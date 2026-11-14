"""Live metrics, analytics, monitoring and the server-sent event stream."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import orjson
import psutil
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import CacheDep, ReadPrincipal, RedisDep, SessionDep
from ..limiter import keys
from ..limiter.cache import RuntimeCache
from ..models import MetricRollup
from ..services import metrics

router = APIRouter()

RangeHours = Annotated[int, Query(ge=1, le=24 * 30)]


def _policy_id(cache: RuntimeCache, name: str | None) -> str | None:
    if name is None:
        return None
    policy = cache.policies.get(name)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy '{name}' does not exist")
    return policy.id


# ---- live -----------------------------------------------------------------


@router.get("/metrics/live", tags=["Metrics"])
async def live(_: ReadPrincipal, redis: RedisDep, cache: CacheDep) -> dict[str, Any]:
    return await metrics.live_snapshot(redis, cache)


@router.get("/metrics/series", tags=["Metrics"])
async def metric_series(
    _: ReadPrincipal,
    redis: RedisDep,
    cache: CacheDep,
    seconds: Annotated[int, Query(ge=10, le=3600)] = 300,
    step: Annotated[int, Query(ge=1, le=60)] = 1,
    policy: str | None = None,
) -> dict[str, Any]:
    points = await metrics.series(redis, _policy_id(cache, policy), seconds=seconds, step=step)
    return {"policy": policy, "step": step, "points": points}


async def _recent_events(redis: Redis, last_id: str, count: int = 100) -> tuple[str, list[dict[str, Any]]]:
    raw = await redis.xread({keys.EVENT_STREAM: last_id}, count=count)
    events: list[dict[str, Any]] = []
    for _stream, entries in raw:
        for entry_id, fields in entries:
            last_id = entry_id
            events.append(
                {
                    "id": entry_id,
                    "ts": int(fields["ts"]) / 1000,
                    "policy": fields["pn"],
                    "subject": fields["sub"],
                    "allowed": fields["ok"] == "1",
                    "reason": fields["rs"],
                    "limit": int(fields["lim"]),
                    "remaining": int(fields["rem"]),
                    "cost": int(fields["cost"]),
                }
            )
    return last_id, events


async def stream_frames(
    redis: Redis, cache: RuntimeCache, disconnected: Callable[[], Awaitable[bool]]
) -> AsyncIterator[bytes]:
    """One metrics frame per second, plus any decisions logged since the previous frame."""
    # Start from the newest event so a new subscriber does not replay history.
    latest = await redis.xrevrange(keys.EVENT_STREAM, count=1)
    last_id = latest[0][0] if latest else "0-0"
    yield b"retry: 2000\n\n"
    while not await disconnected():
        started = time.monotonic()
        snapshot = await metrics.live_snapshot(redis, cache)
        yield b"event: metrics\ndata: " + orjson.dumps(snapshot) + b"\n\n"
        last_id, events = await _recent_events(redis, last_id)
        if events:
            yield b"event: decisions\ndata: " + orjson.dumps(events) + b"\n\n"
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - started)))


@router.get(
    "/stream", tags=["Metrics"], summary="Server-sent events: one metrics frame per second plus new decisions"
)
async def stream(request: Request, _: ReadPrincipal, redis: RedisDep, cache: CacheDep) -> StreamingResponse:
    return StreamingResponse(
        stream_frames(redis, cache, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- analytics ------------------------------------------------------------


def _bucket_seconds(hours: int) -> int:
    if hours <= 3:
        return 60
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 3600
    return 4 * 3600


@router.get("/analytics/overview", tags=["Analytics"])
async def overview(
    _: ReadPrincipal, session: SessionDep, cache: CacheDep, hours: RangeHours = 24, policy: str | None = None
) -> dict[str, Any]:
    policy_id = _policy_id(cache, policy) or ""
    since = datetime.now(UTC) - timedelta(hours=hours)
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(MetricRollup.allowed), 0),
                func.coalesce(func.sum(MetricRollup.blocked), 0),
                func.avg(MetricRollup.avg_health),
            ).where(MetricRollup.policy_id == policy_id, MetricRollup.ts_minute >= since)
        )
    ).one()
    allowed, blocked = int(totals[0]), int(totals[1])

    peak = (
        await session.execute(
            select(MetricRollup.ts_minute, (MetricRollup.allowed + MetricRollup.blocked).label("total"))
            .where(MetricRollup.policy_id == policy_id, MetricRollup.ts_minute >= since)
            .order_by(text("total DESC"))
            .limit(1)
        )
    ).first()

    # Upstream latency is reported per policy; weight each minute by its request count.
    latency_filter = [MetricRollup.ts_minute >= since, MetricRollup.upstream_requests > 0]
    latency_filter.append(MetricRollup.policy_id == policy_id if policy_id else MetricRollup.policy_id != "")
    upstream = (
        await session.execute(
            select(
                func.coalesce(func.sum(MetricRollup.upstream_requests), 0),
                func.coalesce(func.sum(MetricRollup.upstream_errors), 0),
                func.sum(MetricRollup.upstream_latency_avg_ms * MetricRollup.upstream_requests),
                func.max(MetricRollup.upstream_latency_p95_ms),
            ).where(*latency_filter)
        )
    ).one()
    upstream_requests = int(upstream[0])

    return {
        "hours": hours,
        "policy": policy,
        "allowed": allowed,
        "blocked": blocked,
        "total": allowed + blocked,
        "success_rate": round(allowed / (allowed + blocked), 4) if allowed + blocked else None,
        "avg_health": round(float(totals[2]), 1) if totals[2] is not None else None,
        "peak_minute": peak[0].isoformat() if peak and peak[1] else None,
        "peak_requests_per_minute": int(peak[1]) if peak and peak[1] else 0,
        "upstream_requests": upstream_requests,
        "upstream_error_rate": round(int(upstream[1]) / upstream_requests, 4) if upstream_requests else None,
        "avg_latency_ms": round(float(upstream[2]) / upstream_requests, 2)
        if upstream_requests and upstream[2] is not None
        else None,
        "worst_p95_latency_ms": round(float(upstream[3]), 2) if upstream[3] is not None else None,
    }


@router.get("/analytics/timeseries", tags=["Analytics"])
async def timeseries(
    _: ReadPrincipal, session: SessionDep, cache: CacheDep, hours: RangeHours = 24, policy: str | None = None
) -> dict[str, Any]:
    policy_id = _policy_id(cache, policy) or ""
    bucket = _bucket_seconds(hours)
    since = datetime.now(UTC) - timedelta(hours=hours)
    epoch = func.floor(func.extract("epoch", MetricRollup.ts_minute) / bucket) * bucket
    rows = (
        await session.execute(
            select(
                epoch.label("bucket"),
                func.sum(MetricRollup.allowed),
                func.sum(MetricRollup.blocked),
                func.avg(MetricRollup.avg_limit),
                func.avg(MetricRollup.avg_health),
                func.max(MetricRollup.upstream_latency_p95_ms),
                func.avg(MetricRollup.upstream_latency_avg_ms),
            )
            .where(MetricRollup.policy_id == policy_id, MetricRollup.ts_minute >= since)
            .group_by(text("bucket"))
            .order_by(text("bucket"))
        )
    ).all()
    return {
        "policy": policy,
        "bucket_seconds": bucket,
        "points": [
            {
                "ts": int(row[0]),
                "allowed": int(row[1] or 0),
                "blocked": int(row[2] or 0),
                "limit": round(float(row[3]), 2) if row[3] is not None else None,
                "health": round(float(row[4]), 1) if row[4] is not None else None,
                "latency_p95_ms": round(float(row[5]), 2) if row[5] is not None else None,
                "latency_avg_ms": round(float(row[6]), 2) if row[6] is not None else None,
            }
            for row in rows
        ],
    }


@router.get("/analytics/heatmap", tags=["Analytics"], summary="Requests by weekday and hour")
async def heatmap(
    _: ReadPrincipal,
    session: SessionDep,
    cache: CacheDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    tz_offset_minutes: Annotated[int, Query(ge=-840, le=840)] = 0,
    policy: str | None = None,
) -> dict[str, Any]:
    policy_id = _policy_id(cache, policy) or ""
    since = datetime.now(UTC) - timedelta(days=days)
    local = MetricRollup.ts_minute + timedelta(minutes=tz_offset_minutes)
    local_utc = func.timezone("UTC", local)
    rows = (
        await session.execute(
            select(
                func.extract("dow", local_utc).label("dow"),
                func.extract("hour", local_utc).label("hour"),
                func.sum(MetricRollup.allowed + MetricRollup.blocked),
                func.sum(MetricRollup.blocked),
            )
            .where(MetricRollup.policy_id == policy_id, MetricRollup.ts_minute >= since)
            .group_by(text("dow"), text("hour"))
        )
    ).all()
    cells = [
        {
            "weekday": int(row[0]),
            "hour": int(row[1]),
            "requests": int(row[2] or 0),
            "blocked": int(row[3] or 0),
        }
        for row in rows
    ]
    by_hour: dict[int, int] = defaultdict(int)
    for cell in cells:
        by_hour[cell["hour"]] += cell["requests"]
    peak_hours = sorted(by_hour, key=lambda hour: -by_hour[hour])[:3]
    return {"days": days, "cells": cells, "peak_hours": [h for h in peak_hours if by_hour[h] > 0]}


async def _top(redis: Redis, kind: str, hours: int, limit: int) -> list[dict[str, Any]]:
    current_hour = int(time.time()) // 3600
    pipe = redis.pipeline(transaction=False)
    for hour in range(current_hour - hours + 1, current_hour + 1):
        pipe.zrevrange(keys.top_key(kind, hour), 0, 499, withscores=True)
    totals: dict[str, float] = defaultdict(float)
    for entries in await pipe.execute():
        for member, score in entries:
            totals[member] += score
    ranked = sorted(totals.items(), key=lambda item: -item[1])[:limit]
    result: list[dict[str, Any]] = []
    for member, score in ranked:
        policy_name, _, subject = member.partition("|")
        result.append({"policy": policy_name, "subject": subject, "requests": int(score)})
    return result


@router.get("/analytics/top", tags=["Analytics"], summary="Busiest and most-blocked clients")
async def top_clients(
    _: ReadPrincipal,
    redis: RedisDep,
    hours: Annotated[int, Query(ge=1, le=24)] = 24,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict[str, Any]:
    return {
        "hours": hours,
        "allowed": await _top(redis, "a", hours, limit),
        "blocked": await _top(redis, "b", hours, limit),
    }


# ---- monitoring -----------------------------------------------------------


async def _timed(coroutine: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    result = await coroutine
    return result, round((time.perf_counter() - started) * 1000, 2)


async def _redis_status(redis: Redis) -> dict[str, Any]:
    try:
        _, latency = await _timed(redis.ping())
        info = await redis.info()
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    hits = int(info.get("keyspace_hits", 0))
    misses = int(info.get("keyspace_misses", 0))
    return {
        "ok": True,
        "latency_ms": latency,
        "version": info.get("redis_version"),
        "uptime_seconds": info.get("uptime_in_seconds"),
        "connected_clients": info.get("connected_clients"),
        "used_memory_bytes": info.get("used_memory"),
        "max_memory_bytes": info.get("maxmemory") or None,
        "ops_per_second": info.get("instantaneous_ops_per_sec"),
        "keys": await redis.dbsize(),
        "cache_hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
    }


async def _postgres_status(session: AsyncSession) -> dict[str, Any]:
    try:
        _, latency = await _timed(session.execute(text("SELECT 1")))
        row = (
            await session.execute(
                text(
                    "SELECT pg_database_size(current_database()), "
                    "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()), "
                    "current_setting('max_connections')::int, "
                    "current_setting('server_version')"
                )
            )
        ).one()
        tables = (
            await session.execute(
                text(
                    "SELECT relname, n_live_tup FROM pg_stat_user_tables "
                    "WHERE relname IN "
                    "('decision_logs', 'metric_rollups', 'audit_logs', 'policies', 'api_keys')"
                )
            )
        ).all()
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {
        "ok": True,
        "latency_ms": latency,
        "version": row[3],
        "size_bytes": int(row[0]),
        "connections": int(row[1]),
        "max_connections": int(row[2]),
        "rows": {name: int(count) for name, count in tables},
    }


@router.get("/monitoring", tags=["Monitoring"])
async def monitoring(
    _: ReadPrincipal, redis: RedisDep, session: SessionDep, cache: CacheDep
) -> dict[str, Any]:
    now = time.time()
    redis_status, postgres_status = await _redis_status(redis), await _postgres_status(session)

    worker_raw: dict[str, str] = {}
    api_raw: dict[str, str] = {}
    host: dict[str, float] = {}
    if redis_status["ok"]:
        worker_raw = await redis.hgetall(keys.WORKER_HEARTBEAT)
        api_raw = await redis.hgetall(keys.API_HEARTBEATS)
        host = await metrics.host_signals(redis)

    worker_age = now - float(worker_raw["ts"]) if "ts" in worker_raw else None
    worker = {
        "ok": worker_age is not None and worker_age < 10,
        "last_seen_seconds": round(worker_age, 1) if worker_age is not None else None,
        "instance": worker_raw.get("instance"),
        "tick_ms": float(worker_raw["tick_ms"]) if "tick_ms" in worker_raw else None,
        "event_lag": int(worker_raw["event_lag"]) if "event_lag" in worker_raw else None,
        "logs_written": int(worker_raw["logs_written"]) if "logs_written" in worker_raw else None,
    }

    api_processes: list[dict[str, Any]] = []
    stale: list[str] = []
    for instance, raw in api_raw.items():
        beat = orjson.loads(raw)
        age = now - beat["ts"]
        if age > 60:
            stale.append(instance)
            continue
        api_processes.append({"instance": instance, "last_seen_seconds": round(age, 1), **beat})
    if stale:
        await redis.hdel(keys.API_HEARTBEATS, *stale)

    process = psutil.Process(os.getpid())
    checks = [
        {"name": "Redis", "ok": redis_status["ok"], "detail": f"{redis_status.get('latency_ms', '-')} ms"},
        {
            "name": "PostgreSQL",
            "ok": postgres_status["ok"],
            "detail": f"{postgres_status.get('latency_ms', '-')} ms",
        },
        {
            "name": "Adaptive worker",
            "ok": worker["ok"],
            "detail": "no heartbeat" if worker_age is None else f"seen {worker_age:.0f}s ago",
        },
        {
            "name": "Policy cache",
            "ok": now - cache.loaded_at < 120,
            "detail": f"{len(cache.policies)} policies, refreshed {now - cache.loaded_at:.0f}s ago",
        },
    ]
    return {
        "ts": now,
        "status": "ok" if all(check["ok"] for check in checks) else "degraded",
        "checks": checks,
        "host": {
            "cpu_percent": host.get("cpu"),
            "memory_percent": host.get("mem"),
            "load_1m": host.get("load1"),
            "cpu_count": psutil.cpu_count(),
        },
        "redis": redis_status,
        "postgres": postgres_status,
        "worker": worker,
        "api_processes": sorted(api_processes, key=lambda item: item["instance"]),
        "this_process": {
            "pid": process.pid,
            "rss_bytes": process.memory_info().rss,
            "threads": process.num_threads(),
            "cache_reloads": cache.reloads,
        },
    }

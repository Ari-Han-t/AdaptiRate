"""Background worker: adaptive control loop, decision-log sink, rollups, housekeeping.

Run with `python -m app.worker`. Several workers may run for availability; a Redis
lock makes sure only one acts at a time, and another takes over within seconds if
the leader stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import psutil
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adaptive import signals as signal_store
from .adaptive.controller import AdaptiveState, Signals, provider_cap, step
from .config import get_settings
from .db import dispose_engine, session_factory
from .limiter import keys
from .limiter.cache import RuntimeCache
from .limiter.policy import CompiledPolicy
from .models import ApiKey, DecisionLog, MetricRollup, RefreshToken
from .providers.service import ProviderState, state_from_hash
from .redis_client import close_redis, get_redis
from .startup import wait_for_dependencies

logger = logging.getLogger("adaptirate.worker")

TICK_SECONDS = 1.0
LOCK_TTL_MS = 5000
MULTIPLIER_TTL_SECONDS = 30  # limits fall back to their base value if the worker dies
ROLLUP_LAST_KEY = keys.PREFIX + "rollup:last"
ROLLUP_BACKFILL_MINUTES = 55
TOP_SET_MAX_MEMBERS = 5000
SINK_BATCH = 1000


class Worker:
    def __init__(self, redis: Redis, sessions: async_sessionmaker[AsyncSession], cache: RuntimeCache) -> None:
        self.redis = redis
        self.sessions = sessions
        self.cache = cache
        self.instance = f"{socket.gethostname()}:{os.getpid()}"
        self.states: dict[str, AdaptiveState] = {}
        self.logs_written = 0
        self.last_tick_ms = 0.0
        self._was_leader = False
        psutil.cpu_percent(interval=None)  # prime: the first call always returns 0

    # ---- leadership -------------------------------------------------------

    async def is_leader(self) -> bool:
        if await self.redis.set(keys.WORKER_LOCK, self.instance, nx=True, px=LOCK_TTL_MS):
            leader = True
        elif await self.redis.get(keys.WORKER_LOCK) == self.instance:
            await self.redis.pexpire(keys.WORKER_LOCK, LOCK_TTL_MS)
            leader = True
        else:
            leader = False
        if leader != self._was_leader:
            logger.info("%s leadership", "acquired" if leader else "lost")
            self._was_leader = leader
            if leader:
                self.states.clear()  # reload from Redis rather than trust stale memory
        return leader

    # ---- adaptive loop ----------------------------------------------------

    async def _host_signals(self) -> dict[str, float]:
        host = {
            "cpu": psutil.cpu_percent(interval=None),
            "mem": psutil.virtual_memory().percent,
        }
        with contextlib.suppress(OSError, AttributeError):
            host["load1"] = os.getloadavg()[0]
        await self.redis.hset(keys.HOST_SIGNALS, mapping={name: repr(value) for name, value in host.items()})
        await self.redis.expire(keys.HOST_SIGNALS, 30)
        return host

    async def _load_state(self, policy_id: str) -> AdaptiveState:
        raw = await self.redis.hgetall(keys.adaptive_state_key(policy_id))
        if not raw:
            return AdaptiveState()
        return AdaptiveState(
            multiplier=float(raw.get("adaptive_multiplier", 1.0)),
            health=float(raw.get("health", 100.0)),
            last_decrease_at=float(raw.get("last_decrease_at", 0.0)),
        )

    async def _provider_states(self, policies: list[CompiledPolicy]) -> dict[str, ProviderState]:
        names = sorted({policy.provider for policy in policies if policy.provider})
        if not names:
            return {}
        pipe = self.redis.pipeline(transaction=False)
        for name in names:
            pipe.hgetall(keys.provider_state_key(name))
        return {
            name: state_from_hash(name, raw) for name, raw in zip(names, await pipe.execute(), strict=True)
        }

    async def adaptive_tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        config = self.cache.config.adaptive
        host = await self._host_signals()
        policies = [policy for policy in self.cache.policies.values() if policy.enabled]
        providers = await self._provider_states(policies)
        second = int(now)

        pipe = self.redis.pipeline(transaction=False)
        for policy in policies:
            observed = Signals()
            if policy.adaptive and config.enabled:
                state = self.states.get(policy.id) or await self._load_state(policy.id)
                observed = await signal_store.collect(
                    self.redis,
                    policy.id,
                    now=now,
                    window_seconds=config.signal_window_seconds,
                    host=host if config.use_host_signals else None,
                )
                state = step(
                    state,
                    observed,
                    now=now,
                    min_multiplier=policy.min_limit / policy.limit,
                    max_multiplier=policy.max_limit / policy.limit,
                    priority=policy.priority,
                    target_latency_ms=policy.target_latency_ms,
                    config=config,
                )
            else:
                state = AdaptiveState()
            self.states[policy.id] = state

            multiplier = state.multiplier
            cap: float | None = None
            provider = providers.get(policy.provider) if policy.provider else None
            if provider is not None:
                cap = provider_cap(
                    remaining=provider.remaining(policy.provider_dimension),
                    reset_in_seconds=provider.reset_in(policy.provider_dimension, now),
                    window_seconds=policy.window_ms / 1000,
                    base_limit=policy.limit,
                    safety_margin=config.provider_safety_margin,
                )
                if cap is not None:
                    multiplier = min(multiplier, cap)
            effective = max(1, round(policy.limit * multiplier))

            if abs(multiplier - 1.0) < 1e-9:
                pipe.delete(keys.multiplier_key(policy.id))
            else:
                pipe.set(
                    keys.multiplier_key(policy.id), repr(round(multiplier, 6)), ex=MULTIPLIER_TTL_SECONDS
                )

            state_key = keys.adaptive_state_key(policy.id)
            pipe.hset(
                state_key,
                mapping={
                    "ts": repr(now),
                    "health": repr(state.health),
                    "adaptive_multiplier": repr(state.multiplier),
                    "multiplier": repr(multiplier),
                    "effective_limit": str(effective),
                    "action": state.action,
                    "last_decrease_at": repr(state.last_decrease_at),
                    "penalties": orjson.dumps(state.penalties).decode(),
                    "signals": orjson.dumps(
                        {k: v for k, v in asdict(observed).items() if v is not None}
                    ).decode(),
                    "provider_cap": "" if cap is None else repr(round(cap, 6)),
                },
            )
            pipe.expire(state_key, 120)
            counter = keys.counter_key(policy.id, second)
            pipe.hset(counter, mapping={"lim": str(effective), "hs": repr(state.health)})
            pipe.expire(counter, 3700)

        # Forget state for policies that no longer exist or were disabled.
        active = {policy.id for policy in policies}
        for policy_id in [pid for pid in self.states if pid not in active]:
            del self.states[policy_id]
            pipe.delete(keys.multiplier_key(policy_id))
        await pipe.execute()

    # ---- decision log sink ------------------------------------------------

    async def sink_once(self, block_ms: int = 1000) -> int:
        """Move one batch of decision events from the Redis stream into Postgres."""
        cursor = await self.redis.get(keys.EVENT_CURSOR) or "0-0"
        raw = await self.redis.xread({keys.EVENT_STREAM: cursor}, count=SINK_BATCH, block=block_ms or None)
        if not raw:
            return 0
        entries = raw[0][1]
        rows: list[dict[str, Any]] = []
        for _entry_id, fields in entries:
            try:
                rows.append(
                    {
                        "ts": datetime.fromtimestamp(int(fields["ts"]) / 1000, UTC),
                        "policy_id": fields["pid"],
                        "policy_name": fields["pn"],
                        "subject": fields["sub"][:300],
                        "allowed": fields["ok"] == "1",
                        "reason": fields["rs"][:16],
                        "limit": int(fields["lim"]),
                        "remaining": int(fields["rem"]),
                        "cost": int(fields["cost"]),
                        "api_key_id": fields.get("kid") or None,
                    }
                )
            except (KeyError, ValueError):
                logger.warning("skipping malformed event %s", _entry_id)
        if rows:
            async with self.sessions() as session:
                await session.execute(insert(DecisionLog), rows)
                await session.commit()
        # Advance only after the rows are durable; a crash before this re-reads the batch.
        await self.redis.set(keys.EVENT_CURSOR, entries[-1][0])
        self.logs_written += len(rows)
        return len(rows)

    # ---- rollups ----------------------------------------------------------

    async def _rollup_minute(self, minute_start: int) -> None:
        policies = list(self.cache.policies.values())
        targets: list[tuple[str, str | None]] = [("", None)] + [(policy.id, policy.id) for policy in policies]
        pipe = self.redis.pipeline(transaction=False)
        for _row_id, policy_id in targets:
            for second in range(minute_start, minute_start + 60):
                pipe.hgetall(keys.counter_key(policy_id, second))
        for policy in policies:
            for second in range(minute_start, minute_start + 60):
                pipe.hgetall(keys.signal_key(policy.id, second))
        raw = await pipe.execute()

        rows: list[dict[str, Any]] = []
        total_requests = total_errors = 0
        total_latency = 0.0
        total_buckets = [0] * signal_store.BUCKET_COUNT
        policy_healths: list[float] = []
        ts_minute = datetime.fromtimestamp(minute_start, UTC)
        for index, (row_id, policy_id) in enumerate(targets):
            counters = raw[index * 60 : index * 60 + 60]
            allowed = sum(int(c.get("a", 0)) for c in counters)
            blocked = sum(int(c.get("b", 0)) for c in counters)
            limits = [float(c["lim"]) for c in counters if "lim" in c]
            healths = [float(c["hs"]) for c in counters if "hs" in c]
            row: dict[str, Any] = {
                "ts_minute": ts_minute,
                "policy_id": row_id,
                "allowed": allowed,
                "blocked": blocked,
                "avg_limit": sum(limits) / len(limits) if limits else None,
                "avg_health": sum(healths) / len(healths) if healths else None,
                "upstream_requests": 0,
                "upstream_errors": 0,
                "upstream_latency_p95_ms": None,
                "upstream_latency_avg_ms": None,
            }
            if policy_id is not None:
                offset = (len(targets) + index - 1) * 60
                reported = raw[offset : offset + 60]
                requests = sum(int(r.get("n", 0)) for r in reported)
                buckets = [0] * signal_store.BUCKET_COUNT
                for r in reported:
                    for bucket in range(signal_store.BUCKET_COUNT):
                        value = r.get(f"b{bucket}")
                        if value:
                            buckets[bucket] += int(value)
                latency_sum = sum(float(r.get("ls", 0)) for r in reported)
                row["upstream_requests"] = requests
                row["upstream_errors"] = sum(int(r.get("e", 0)) + int(r.get("to", 0)) for r in reported)
                row["upstream_latency_p95_ms"] = signal_store.percentile_from_buckets(buckets, 0.95)
                row["upstream_latency_avg_ms"] = latency_sum / requests if requests and latency_sum else None
                total_requests += requests
                total_errors += row["upstream_errors"]
                total_latency += latency_sum
                total_buckets = [left + right for left, right in zip(total_buckets, buckets, strict=True)]
                if healths:
                    policy_healths.append(sum(healths) / len(healths))
            if allowed or blocked or row["upstream_requests"] or row_id == "":
                rows.append(row)

        # The all-policy row carries the combined upstream picture, so "all policies"
        # views need no special handling. Health is the worst policy's: an average
        # would hide one failing service behind several healthy ones.
        overall = rows[0]
        overall["upstream_requests"] = total_requests
        overall["upstream_errors"] = total_errors
        overall["upstream_latency_p95_ms"] = signal_store.percentile_from_buckets(total_buckets, 0.95)
        overall["upstream_latency_avg_ms"] = (
            total_latency / total_requests if total_requests and total_latency else None
        )
        overall["avg_health"] = min(policy_healths) if policy_healths else None

        statement = pg_insert(MetricRollup).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["ts_minute", "policy_id"],
            set_={
                column: statement.excluded[column]
                for column in rows[0]
                if column not in ("ts_minute", "policy_id")
            },
        )
        async with self.sessions() as session:
            await session.execute(statement)
            await session.commit()

    async def rollup(self, now: float | None = None) -> int:
        """Persist every complete minute not yet rolled up. Returns how many were written."""
        now = time.time() if now is None else now
        last_complete = int(now) // 60 * 60 - 60
        stored = await self.redis.get(ROLLUP_LAST_KEY)
        earliest = last_complete - ROLLUP_BACKFILL_MINUTES * 60
        start = max(int(stored) + 60, earliest) if stored else last_complete
        written = 0
        for minute_start in range(start, last_complete + 60, 60):
            await self._rollup_minute(minute_start)
            await self.redis.set(ROLLUP_LAST_KEY, str(minute_start))
            written += 1
        return written

    # ---- housekeeping -----------------------------------------------------

    async def housekeeping(self) -> None:
        now = datetime.now(UTC)
        logging_config = self.cache.config.logging
        retention = logging_config.retention_days
        last_used = await self.redis.hgetall(keys.key_last_used_key())
        async with self.sessions() as session:
            await session.execute(delete(DecisionLog).where(DecisionLog.ts < now - timedelta(days=retention)))
            # Also bound the log by size, so a busy system cannot fill the disk within
            # the retention period. Ids are sequential, so this keeps the newest rows.
            newest = (await session.execute(select(func.max(DecisionLog.id)))).scalar_one_or_none()
            if newest is not None:
                await session.execute(
                    delete(DecisionLog).where(DecisionLog.id <= newest - logging_config.max_rows)
                )
            await session.execute(
                delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=1))
            )
            for key_id, seen in last_used.items():
                await session.execute(
                    update(ApiKey)
                    .where(ApiKey.id == key_id)
                    .values(last_used_at=datetime.fromtimestamp(int(seen), UTC))
                )
            await session.commit()
        hour = int(time.time()) // 3600
        pipe = self.redis.pipeline(transaction=False)
        for kind in ("a", "b"):
            pipe.zremrangebyrank(keys.top_key(kind, hour), 0, -(TOP_SET_MAX_MEMBERS + 1))
        await pipe.execute()

    # ---- loops ------------------------------------------------------------

    async def _heartbeat(self) -> None:
        await self.redis.hset(
            keys.WORKER_HEARTBEAT,
            mapping={
                "ts": repr(time.time()),
                "instance": self.instance,
                "tick_ms": repr(round(self.last_tick_ms, 2)),
                "logs_written": str(self.logs_written),
            },
        )

    async def _control_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                if await self.is_leader():
                    await self.adaptive_tick()
                    self.last_tick_ms = (time.monotonic() - started) * 1000
                    await self._heartbeat()
            except Exception:
                logger.exception("adaptive tick failed")
            await asyncio.sleep(max(0.05, TICK_SECONDS - (time.monotonic() - started)))

    async def _sink_loop(self) -> None:
        while True:
            try:
                if self._was_leader:
                    await self.sink_once()
                else:
                    await asyncio.sleep(1)
            except Exception:
                logger.exception("log sink failed")
                await asyncio.sleep(1)

    async def _slow_loop(self) -> None:
        last_housekeeping = 0.0
        while True:
            try:
                if self._was_leader:
                    await self.rollup()
                    if time.monotonic() - last_housekeeping > 60:
                        await self.housekeeping()
                        last_housekeeping = time.monotonic()
            except Exception:
                logger.exception("rollup or housekeeping failed")
            await asyncio.sleep(5)

    async def run(self, stop: asyncio.Event) -> None:
        await self.cache.start()
        tasks = [
            asyncio.create_task(self._control_loop(), name="control"),
            asyncio.create_task(self._sink_loop(), name="sink"),
            asyncio.create_task(self._slow_loop(), name="slow"),
        ]
        logger.info("worker %s started", self.instance)
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._was_leader:
            # Release the lock so a standby takes over immediately.
            with contextlib.suppress(Exception):
                if await self.redis.get(keys.WORKER_LOCK) == self.instance:
                    await self.redis.delete(keys.WORKER_LOCK)
        await self.cache.stop()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    redis = get_redis()
    worker = Worker(
        redis, session_factory(), RuntimeCache(redis, session_factory(), settings.cache_refresh_seconds)
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        await wait_for_dependencies(redis, session_factory())
        await worker.run(stop)
    finally:
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())

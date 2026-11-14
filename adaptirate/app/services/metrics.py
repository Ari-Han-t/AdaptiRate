"""Reads live counters and adaptive state out of Redis."""

from __future__ import annotations

import time
from typing import Any

import orjson
from redis.asyncio import Redis

from ..limiter import keys
from ..limiter.cache import RuntimeCache
from ..limiter.policy import CompiledPolicy
from ..providers.service import state_from_hash

MAX_SERIES_SECONDS = 3600


async def series(
    redis: Redis, policy_id: str | None, *, seconds: int, step: int = 1, now: float | None = None
) -> list[dict[str, Any]]:
    """Per-step traffic for the last `seconds`, oldest first. Rates are per second."""
    seconds = max(1, min(seconds, MAX_SERIES_SECONDS))
    step = max(1, step)
    # The current second is still filling; end at the last complete one.
    end = int(time.time() if now is None else now) - 1
    end -= end % step
    start = end - seconds + step
    start -= start % step

    pipe = redis.pipeline(transaction=False)
    for second in range(start, end + step):
        pipe.hgetall(keys.counter_key(policy_id, second))
    rows: list[dict[str, str]] = await pipe.execute()

    points: list[dict[str, Any]] = []
    for offset in range(0, len(rows), step):
        chunk = rows[offset : offset + step]
        allowed = sum(int(row.get("a", 0)) for row in chunk)
        blocked = sum(int(row.get("b", 0)) for row in chunk)
        limits = [float(row["lim"]) for row in chunk if "lim" in row]
        healths = [float(row["hs"]) for row in chunk if "hs" in row]
        points.append(
            {
                "ts": start + offset,
                "allowed": round(allowed / step, 2),
                "blocked": round(blocked / step, 2),
                "limit": round(sum(limits) / len(limits), 2) if limits else None,
                "health": round(sum(healths) / len(healths), 1) if healths else None,
            }
        )
    return points


async def policy_states(redis: Redis, policies: list[CompiledPolicy]) -> list[dict[str, Any]]:
    if not policies:
        return []
    pipe = redis.pipeline(transaction=False)
    for policy in policies:
        pipe.hgetall(keys.adaptive_state_key(policy.id))
        pipe.get(keys.multiplier_key(policy.id))
        pipe.pttl(keys.pause_key(policy.id))
    raw = await pipe.execute()
    states: list[dict[str, Any]] = []
    for index, policy in enumerate(policies):
        state, multiplier_raw, pause_ttl = raw[index * 3 : index * 3 + 3]
        multiplier = float(multiplier_raw) if multiplier_raw is not None else 1.0
        states.append(
            {
                "id": policy.id,
                "name": policy.name,
                "enabled": policy.enabled,
                "algorithm": policy.algorithm.value,
                "adaptive": policy.adaptive,
                "base_limit": policy.limit,
                "window_seconds": policy.window_ms / 1000,
                "effective_limit": max(1, round(policy.limit * multiplier)),
                "multiplier": round(multiplier, 4),
                "health": float(state["health"]) if "health" in state else None,
                "action": state.get("action"),
                "penalties": orjson.loads(state["penalties"]) if "penalties" in state else {},
                "signals": orjson.loads(state["signals"]) if "signals" in state else {},
                "provider": policy.provider,
                "provider_cap": float(state["provider_cap"]) if state.get("provider_cap") else None,
                "paused_ms": max(0, int(pause_ttl)),
            }
        )
    return states


async def host_signals(redis: Redis) -> dict[str, float]:
    raw = await redis.hgetall(keys.HOST_SIGNALS)
    return {name: float(value) for name, value in raw.items()}


async def provider_states(redis: Redis, names: set[str], now: float) -> list[dict[str, Any]]:
    ordered = sorted(names)
    if not ordered:
        return []
    pipe = redis.pipeline(transaction=False)
    for name in ordered:
        pipe.hgetall(keys.provider_state_key(name))
    raw = await pipe.execute()
    return [state_from_hash(name, values).as_dict(now) for name, values in zip(ordered, raw, strict=True)]


async def live_snapshot(redis: Redis, cache: RuntimeCache) -> dict[str, Any]:
    """Everything the dashboard's front page shows, in one call."""
    now = time.time()
    last_minute = await series(redis, None, seconds=60, now=now)
    recent = last_minute[-5:]
    allowed_rate = sum(point["allowed"] for point in recent) / len(recent)
    blocked_rate = sum(point["blocked"] for point in recent) / len(recent)
    allowed_minute = sum(point["allowed"] for point in last_minute)
    blocked_minute = sum(point["blocked"] for point in last_minute)
    total_minute = allowed_minute + blocked_minute

    policies = sorted(cache.policies.values(), key=lambda policy: policy.name)
    states = await policy_states(redis, policies)
    healths = [state["health"] for state in states if state["health"] is not None and state["enabled"]]
    host = await host_signals(redis)

    latencies = [s["signals"].get("latency_p95_ms") for s in states]
    latencies = [value for value in latencies if value is not None]
    queues = [s["signals"].get("queue_depth") for s in states]
    queues = [value for value in queues if value is not None]

    provider_names = {policy.provider for policy in policies if policy.provider} | set(cache.custom_providers)
    return {
        "ts": now,
        "requests_per_second": round(allowed_rate + blocked_rate, 2),
        "allowed_per_second": round(allowed_rate, 2),
        "blocked_per_second": round(blocked_rate, 2),
        "allowed_last_minute": int(allowed_minute),
        "blocked_last_minute": int(blocked_minute),
        "success_rate": round(allowed_minute / total_minute, 4) if total_minute else None,
        # The least healthy policy sets the headline: an average would hide one failing service.
        "health": min(healths) if healths else None,
        "cpu_percent": host.get("cpu"),
        "memory_percent": host.get("mem"),
        "latency_p95_ms": max(latencies) if latencies else None,
        "queue_depth": sum(queues) if queues else None,
        "policies": states,
        "providers": await provider_states(redis, provider_names, now),
    }

"""Stores what protected services report and turns it back into Signals."""

from __future__ import annotations

import time
from typing import Any

import orjson
from redis.asyncio import Redis

from ..limiter import keys
from .controller import Signals

# Upper bounds (ms) of the latency histogram every SDK uses. The last bucket is open-ended.
LATENCY_BOUNDS_MS: tuple[float, ...] = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
BUCKET_COUNT = len(LATENCY_BOUNDS_MS) + 1

SIGNAL_TTL_SECONDS = 120
INSTANCE_TTL_SECONDS = 15


async def record_report(
    redis: Redis,
    policy_id: str,
    *,
    instance_id: str,
    requests: int,
    errors: int,
    throttled: int,
    timeouts: int,
    latency_sum_ms: float,
    latency_buckets: list[int] | None,
    in_flight: int | None,
    queue_depth: int | None,
    cpu_percent: float | None,
    memory_percent: float | None,
) -> None:
    now = time.time()
    key = keys.signal_key(policy_id, int(now))
    pipe = redis.pipeline(transaction=False)
    if requests:
        pipe.hincrby(key, "n", requests)
    if errors:
        pipe.hincrby(key, "e", errors)
    if throttled:
        pipe.hincrby(key, "t", throttled)
    if timeouts:
        pipe.hincrby(key, "to", timeouts)
    if latency_sum_ms:
        pipe.hincrbyfloat(key, "ls", latency_sum_ms)
    if latency_buckets:
        for index, count in enumerate(latency_buckets[:BUCKET_COUNT]):
            if count:
                pipe.hincrby(key, f"b{index}", count)
    pipe.expire(key, SIGNAL_TTL_SECONDS)

    gauges: dict[str, Any] = {"ts": now}
    if in_flight is not None:
        gauges["in_flight"] = in_flight
    if queue_depth is not None:
        gauges["queue"] = queue_depth
    if cpu_percent is not None:
        gauges["cpu"] = cpu_percent
    if memory_percent is not None:
        gauges["mem"] = memory_percent
    instances = keys.instances_key(policy_id)
    pipe.hset(instances, instance_id, orjson.dumps(gauges).decode())
    pipe.expire(instances, 3600)
    await pipe.execute()


def percentile_from_buckets(buckets: list[int], quantile: float) -> float | None:
    """Estimate a percentile from histogram counts by linear interpolation within the bucket."""
    total = sum(buckets)
    if total == 0:
        return None
    rank = quantile * total
    seen = 0
    for index, count in enumerate(buckets):
        if count == 0:
            continue
        if seen + count >= rank:
            lower = LATENCY_BOUNDS_MS[index - 1] if index > 0 else 0.0
            if index >= len(LATENCY_BOUNDS_MS):
                return LATENCY_BOUNDS_MS[-1]  # open-ended bucket: report its lower edge
            upper = LATENCY_BOUNDS_MS[index]
            return lower + (upper - lower) * ((rank - seen) / count)
        seen += count
    return LATENCY_BOUNDS_MS[-1]


async def collect(
    redis: Redis,
    policy_id: str,
    *,
    now: float,
    window_seconds: int,
    host: dict[str, float] | None = None,
) -> Signals:
    """Aggregate the last window of reports for one policy."""
    current = int(now)
    pipe = redis.pipeline(transaction=False)
    # Skip the current second: it is still being written to.
    for second in range(current - window_seconds, current):
        pipe.hgetall(keys.signal_key(policy_id, second))
    pipe.hgetall(keys.instances_key(policy_id))
    results = await pipe.execute()
    per_second: list[dict[str, str]] = results[:-1]
    instances: dict[str, str] = results[-1]

    requests = errors = throttled = timeouts = 0
    latency_sum = 0.0
    buckets = [0] * BUCKET_COUNT
    for row in per_second:
        if not row:
            continue
        requests += int(row.get("n", 0))
        errors += int(row.get("e", 0))
        throttled += int(row.get("t", 0))
        timeouts += int(row.get("to", 0))
        latency_sum += float(row.get("ls", 0))
        for index in range(BUCKET_COUNT):
            value = row.get(f"b{index}")
            if value:
                buckets[index] += int(value)

    in_flight: int | None = None
    queue: int | None = None
    cpu_values: list[float] = []
    memory_values: list[float] = []
    stale: list[str] = []
    for instance_id, raw in instances.items():
        gauges = orjson.loads(raw)
        if now - float(gauges.get("ts", 0)) > INSTANCE_TTL_SECONDS:
            stale.append(instance_id)
            continue
        if "in_flight" in gauges:
            in_flight = (in_flight or 0) + int(gauges["in_flight"])
        if "queue" in gauges:
            queue = (queue or 0) + int(gauges["queue"])
        if "cpu" in gauges:
            cpu_values.append(float(gauges["cpu"]))
        if "mem" in gauges:
            memory_values.append(float(gauges["mem"]))
    if stale:
        await redis.hdel(keys.instances_key(policy_id), *stale)

    # The busiest instance decides: one saturated node is a problem even if the mean is fine.
    cpu = max(cpu_values) if cpu_values else None
    memory = max(memory_values) if memory_values else None
    if host:
        if cpu is None:
            cpu = host.get("cpu")
        if memory is None:
            memory = host.get("mem")

    has_traffic = requests > 0
    return Signals(
        requests=requests,
        error_rate=errors / requests if has_traffic else None,
        throttle_rate=throttled / requests if has_traffic else None,
        timeout_rate=timeouts / requests if has_traffic else None,
        latency_p95_ms=percentile_from_buckets(buckets, 0.95),
        latency_avg_ms=latency_sum / requests if has_traffic and latency_sum else None,
        in_flight=in_flight,
        queue_depth=queue,
        cpu_percent=cpu,
        memory_percent=memory,
    )

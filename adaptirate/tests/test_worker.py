"""The worker's jobs, driven tick by tick against real Redis and Postgres."""

from __future__ import annotations

import time

import httpx
from redis.asyncio import Redis

from app.adaptive.signals import BUCKET_COUNT
from app.db import session_factory
from app.limiter import keys
from app.limiter.cache import RuntimeCache
from app.worker import Worker

API = "/api/v1"


def latency_buckets(milliseconds: float, count: int) -> list[int]:
    """All `count` requests in the histogram bucket containing `milliseconds`."""
    bounds = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
    index = next((i for i, bound in enumerate(bounds) if milliseconds <= bound), len(bounds))
    buckets = [0] * BUCKET_COUNT
    buckets[index] = count
    return buckets


async def report(
    client: httpx.AsyncClient, key: dict[str, str], policy: str, *, latency_ms: float, errors: int = 0
):
    body = {
        "policy": policy,
        "instance_id": "app-1",
        "requests": 50,
        "errors": errors,
        "latency_sum_ms": latency_ms * 50,
        "latency_buckets": latency_buckets(latency_ms, 50),
        "in_flight": 4,
    }
    response = await client.post(f"{API}/report", headers=key, json=body)
    assert response.status_code == 202, response.text


async def effective_limit(client: httpx.AsyncClient, admin: dict[str, str], policy_id: str) -> int:
    return (await client.get(f"{API}/policies/{policy_id}", headers=admin)).json()["effective_limit"]


async def run_ticks(
    worker: Worker, client, key, policy: str, *, ticks: int, latency_ms: float, start: float
) -> float:
    """Feed one report per simulated second and advance the controller."""
    now = start
    for _ in range(ticks):
        await report(client, key, policy, latency_ms=latency_ms)
        now += 1.0
        # Reports are stored under the real clock; evaluate "one second later" each time.
        await worker.adaptive_tick(now=time.time() + 1)
    return now


async def test_limit_falls_under_latency_and_recovers(
    client, admin, service_key, make_policy, worker: Worker, redis: Redis
):
    policy = await make_policy(limit=100, adaptive=True, target_latency_ms=100, algorithm="token_bucket")
    await worker.cache.load()
    await redis.delete(keys.HOST_SIGNALS)
    worker.cache.config.adaptive.use_host_signals = False
    worker.cache.config.adaptive.decrease_cooldown_seconds = 0
    assert await effective_limit(client, admin, policy["id"]) == 100

    await run_ticks(worker, client, service_key, policy["name"], ticks=3, latency_ms=800, start=0)
    degraded = await effective_limit(client, admin, policy["id"])
    assert degraded < 40, "sustained slow responses must cut the limit hard"

    live = (await client.get(f"{API}/metrics/live", headers=admin)).json()
    state = next(p for p in live["policies"] if p["name"] == policy["name"])
    assert state["health"] < 60 and state["penalties"]["latency"] == 1.0
    assert state["signals"]["latency_p95_ms"] > 500

    # The enforced limit really is lower: a burst now admits fewer requests.
    burst = [
        (
            await client.post(
                f"{API}/evaluate", headers=service_key, json={"policy": policy["name"], "user": "u"}
            )
        ).json()
        for _ in range(60)
    ]
    assert sum(r["allowed"] for r in burst) == degraded

    # Old slow reports age out of the signal window, then healthy traffic restores the limit.
    await redis.delete(
        *[key async for key in redis.scan_iter(match=keys.signal_key(policy["id"], 0)[:-1] + "*")]
    )
    await run_ticks(worker, client, service_key, policy["name"], ticks=25, latency_ms=20, start=10)
    assert await effective_limit(client, admin, policy["id"]) == 100


async def test_limit_never_drops_below_min_limit(client, admin, service_key, make_policy, worker: Worker):
    policy = await make_policy(limit=100, min_limit=70, adaptive=True, target_latency_ms=50)
    await worker.cache.load()
    worker.cache.config.adaptive.use_host_signals = False
    worker.cache.config.adaptive.decrease_cooldown_seconds = 0
    await run_ticks(worker, client, service_key, policy["name"], ticks=6, latency_ms=4000, start=0)
    assert await effective_limit(client, admin, policy["id"]) == 70


async def test_non_adaptive_policy_is_left_alone(client, admin, service_key, make_policy, worker: Worker):
    policy = await make_policy(limit=100, adaptive=False)
    await worker.cache.load()
    await run_ticks(worker, client, service_key, policy["name"], ticks=4, latency_ms=4000, start=0)
    assert await effective_limit(client, admin, policy["id"]) == 100


async def test_provider_quota_caps_the_limit_before_a_429(
    client, admin, service_key, make_policy, worker: Worker
):
    """With 30 requests left for the next 60 s, a 100/min policy must slow to about 27/min."""
    provider = "quota-test"
    await client.put(
        f"{API}/providers/{provider}",
        headers=admin,
        json={"remaining_requests": "x-left", "reset_requests": "x-reset", "reset_format": "seconds"},
    )
    policy = await make_policy(limit=100, window_seconds=60, adaptive=False, provider=provider)
    headers = {"x-left": "30", "x-reset": "60"}
    await client.post(
        f"{API}/providers/report",
        headers=service_key,
        json={"provider": provider, "status": 200, "headers": headers},
    )
    await worker.cache.load()
    await worker.adaptive_tick()
    limit = await effective_limit(client, admin, policy["id"])
    assert 24 <= limit <= 28

    # Quota replenished: the cap lifts on the next tick.
    await client.post(
        f"{API}/providers/report",
        headers=service_key,
        json={"provider": provider, "status": 200, "headers": {"x-left": "5000", "x-reset": "60"}},
    )
    await worker.adaptive_tick()
    assert await effective_limit(client, admin, policy["id"]) == 100


async def test_only_one_worker_leads(worker: Worker, redis: Redis):
    await redis.delete(keys.WORKER_LOCK)
    standby = Worker(redis, session_factory(), RuntimeCache(redis, session_factory()))
    standby.instance = "standby:1"
    assert await worker.is_leader()
    assert not await standby.is_leader()
    assert await worker.is_leader(), "the leader keeps its lease"
    await redis.delete(keys.WORKER_LOCK)  # leader gone
    assert await standby.is_leader()
    assert not await worker.is_leader()
    await redis.delete(keys.WORKER_LOCK)


async def test_decisions_reach_the_searchable_log(
    client, admin, service_key, make_policy, worker: Worker, redis: Redis
):
    policy = await make_policy(limit=2)
    name = policy["name"]
    for user in ("alice", "alice", "alice", "=cmd|' /C calc'!A0"):
        await client.post(f"{API}/evaluate", headers=service_key, json={"policy": name, "user": user})
    await client._transport.app.state.events.flush()  # type: ignore[attr-defined]
    while await worker.sink_once(block_ms=50):
        pass

    logs = (await client.get(f"{API}/logs", headers=admin, params={"policy": name})).json()["items"]
    assert len(logs) == 4
    assert [entry["allowed"] for entry in reversed(logs)] == [True, True, False, True]

    blocked = (
        await client.get(f"{API}/logs", headers=admin, params={"policy": name, "allowed": "false"})
    ).json()
    assert [entry["reason"] for entry in blocked["items"]] == ["limit"]
    searched = (await client.get(f"{API}/logs", headers=admin, params={"policy": name, "q": "ALI"})).json()
    assert len(searched["items"]) == 3
    # LIKE wildcards in the search text are literal, not patterns.
    assert (await client.get(f"{API}/logs", headers=admin, params={"policy": name, "q": "%"})).json()[
        "items"
    ] == []

    first_page = (await client.get(f"{API}/logs", headers=admin, params={"policy": name, "limit": 3})).json()
    assert len(first_page["items"]) == 3 and first_page["next_cursor"]
    second_page = (
        await client.get(
            f"{API}/logs",
            headers=admin,
            params={"policy": name, "limit": 3, "cursor": first_page["next_cursor"]},
        )
    ).json()
    assert len(second_page["items"]) == 1 and second_page["next_cursor"] is None

    exported = await client.get(f"{API}/logs/export", headers=admin, params={"policy": name, "format": "csv"})
    lines = exported.text.strip().splitlines()
    assert lines[0].startswith("id,ts,policy,subject,allowed")
    assert len(lines) == 5
    assert "'=cmd" in exported.text, "formula-looking subjects must be neutralised in CSV"
    ndjson = await client.get(
        f"{API}/logs/export", headers=admin, params={"policy": name, "format": "ndjson"}
    )
    assert len(ndjson.text.strip().splitlines()) == 4

    # Re-running the sink does not duplicate rows.
    assert await worker.sink_once(block_ms=50) == 0


async def test_rollup_feeds_analytics(client, admin, service_key, make_policy, worker: Worker, redis: Redis):
    policy = await make_policy(limit=3)
    name = policy["name"]
    for _ in range(5):
        await client.post(f"{API}/evaluate", headers=service_key, json={"policy": name, "user": "a"})
    await report(client, service_key, name, latency_ms=40, errors=5)
    await worker.cache.load()

    await redis.delete("ar:rollup:last")
    # Roll up as if the current minute had just completed.
    written = await worker.rollup(now=(int(time.time()) // 60 + 1) * 60 + 1)
    assert written == 1

    overview = (
        await client.get(f"{API}/analytics/overview", headers=admin, params={"policy": name, "hours": 1})
    ).json()
    assert (overview["allowed"], overview["blocked"]) == (3, 2)
    assert overview["success_rate"] == 0.6
    assert overview["peak_requests_per_minute"] == 5
    assert overview["upstream_requests"] == 50 and overview["upstream_error_rate"] == 0.1
    assert overview["avg_latency_ms"] == 40

    # The all-policies view carries the combined upstream picture too.
    combined = (await client.get(f"{API}/analytics/timeseries", headers=admin, params={"hours": 1})).json()
    latest = combined["points"][-1]
    assert latest["latency_p95_ms"] is not None and latest["latency_avg_ms"] is not None

    series = (
        await client.get(f"{API}/analytics/timeseries", headers=admin, params={"policy": name, "hours": 1})
    ).json()
    assert sum(point["allowed"] for point in series["points"]) == 3
    heatmap = (await client.get(f"{API}/analytics/heatmap", headers=admin, params={"policy": name})).json()
    assert sum(cell["requests"] for cell in heatmap["cells"]) == 5
    assert sum(cell["blocked"] for cell in heatmap["cells"]) == 2
    assert len(heatmap["peak_hours"]) == 1

    # Rolling the same minute again updates in place rather than double counting.
    await redis.delete("ar:rollup:last")
    await worker.rollup(now=(int(time.time()) // 60 + 1) * 60 + 1)
    again = (
        await client.get(f"{API}/analytics/overview", headers=admin, params={"policy": name, "hours": 1})
    ).json()
    assert again["allowed"] == 3


async def test_housekeeping_runs_cleanly(worker: Worker):
    await worker.housekeeping()


async def test_log_is_capped_by_row_count(client, admin, service_key, make_policy, worker: Worker):
    policy = await make_policy(limit=1000)
    for index in range(12):
        await client.post(
            f"{API}/evaluate", headers=service_key, json={"policy": policy["name"], "user": f"u{index}"}
        )
    await client._transport.app.state.events.flush()  # type: ignore[attr-defined]
    while await worker.sink_once(block_ms=50):
        pass
    before = (await client.get(f"{API}/logs", headers=admin, params={"policy": policy["name"]})).json()[
        "items"
    ]
    assert len(before) == 12

    await worker.cache.load()
    worker.cache.config.logging.max_rows = 5  # the model's own floor is 1000; this drives the query
    await worker.housekeeping()
    after = (await client.get(f"{API}/logs", headers=admin, params={"limit": 500})).json()["items"]
    assert len(after) == 5, "only the newest rows survive"
    assert [entry["subject"] for entry in after] == [f"u{index}" for index in (11, 10, 9, 8, 7)]


def test_allowed_decisions_are_sampled_by_default():
    from app.runtime_config import RuntimeConfig

    logging = RuntimeConfig().logging
    assert logging.sample_allowed < 1, "logging every allowed request does not scale"
    assert logging.log_blocked

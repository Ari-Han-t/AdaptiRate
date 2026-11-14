"""Limiter algorithms against a real Redis."""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.limiter import keys
from app.limiter.engine import Algorithm, Check, LimiterEngine

ALL = list(Algorithm)


def make(
    unique: str,
    algorithm: Algorithm,
    *,
    limit: int = 10,
    window_ms: int = 60_000,
    burst: int = 0,
    cost: int = 1,
    subject: str = "client",
) -> Check:
    return Check(
        policy_id=unique,
        policy_name=f"policy-{unique}",
        algorithm=algorithm,
        limit=limit,
        window_ms=window_ms,
        burst=burst,
        subject=subject,
        cost=cost,
    )


def capacity(algorithm: Algorithm, limit: int, burst: int) -> int:
    """How many unit requests an idle bucket admits instantly."""
    if algorithm is Algorithm.TOKEN_BUCKET:
        return limit + burst
    if algorithm is Algorithm.LEAKY_BUCKET:
        return burst + 1
    if algorithm is Algorithm.ADAPTIVE_HYBRID:
        return min(limit, burst if burst > 0 else max(1, -(-limit // 5)))
    return limit


@pytest.mark.parametrize("algorithm", ALL)
async def test_admits_exactly_capacity_then_blocks(engine: LimiterEngine, unique: str, algorithm: Algorithm):
    check = make(unique, algorithm, limit=20, burst=5)
    expected = capacity(algorithm, 20, 5)
    decisions = [await engine.check(check) for _ in range(expected + 10)]
    assert sum(d.allowed for d in decisions) == expected
    blocked = decisions[-1]
    assert not blocked.allowed
    assert blocked.reason == "limit"
    assert blocked.retry_after_ms > 0
    assert blocked.limit == 20


@pytest.mark.parametrize("algorithm", ALL)
async def test_concurrent_requests_never_over_admit(engine: LimiterEngine, unique: str, algorithm: Algorithm):
    check = make(unique, algorithm, limit=50, burst=10)
    expected = capacity(algorithm, 50, 10)
    decisions = await asyncio.gather(*(engine.check(check) for _ in range(400)))
    assert sum(d.allowed for d in decisions) == expected


@pytest.mark.parametrize("algorithm", ALL)
async def test_subjects_are_isolated(engine: LimiterEngine, unique: str, algorithm: Algorithm):
    first = make(unique, algorithm, limit=1, subject="a")
    second = make(unique, algorithm, limit=1, subject="b")
    assert (await engine.check(first)).allowed
    assert not (await engine.check(first)).allowed
    assert (await engine.check(second)).allowed


@pytest.mark.parametrize("algorithm", ALL)
async def test_recovers_after_waiting(engine: LimiterEngine, unique: str, algorithm: Algorithm):
    check = make(unique, algorithm, limit=5, window_ms=300, burst=0)
    while (await engine.check(check)).allowed:
        pass
    # Two full windows clears every algorithm, including the sliding window's previous bucket.
    await asyncio.sleep(0.65)
    assert (await engine.check(check)).allowed


@pytest.mark.parametrize("algorithm", ALL)
async def test_retry_after_is_honest(engine: LimiterEngine, unique: str, algorithm: Algorithm):
    """Waiting the advertised retry_after must be enough to get through."""
    check = make(unique, algorithm, limit=10, window_ms=400)
    while True:
        decision = await engine.check(check)
        if not decision.allowed:
            break
    assert 0 < decision.retry_after_ms <= 800
    await asyncio.sleep(decision.retry_after_ms / 1000 + 0.03)
    assert (await engine.check(check)).allowed


@pytest.mark.parametrize("algorithm", ALL)
async def test_cost_larger_than_capacity_is_rejected_as_impossible(
    engine: LimiterEngine, unique: str, algorithm: Algorithm
):
    decision = await engine.check(make(unique, algorithm, limit=5, burst=0, cost=50))
    assert not decision.allowed
    assert decision.reason == "too_large"
    assert decision.retry_after_ms == -1


async def test_token_bucket_refills_at_configured_rate(engine: LimiterEngine, unique: str):
    check = make(unique, Algorithm.TOKEN_BUCKET, limit=100, window_ms=1000)
    drained = await engine.check_many([check] * 100)
    assert all(d.allowed for d in drained)
    assert not (await engine.check(check)).allowed
    await asyncio.sleep(0.2)  # 100/s for 200 ms is about 20 tokens
    refilled = await engine.check_many([check] * 60)
    assert 15 <= sum(d.allowed for d in refilled) <= 30


async def test_sliding_window_has_no_edge_burst(engine: LimiterEngine, unique: str, redis: Redis):
    window = 400
    check = make(unique, Algorithm.SLIDING_WINDOW, limit=20, window_ms=window)
    # Line up just before a window boundary, fill the window, cross the edge.
    now_ms = int((await redis.time())[0]) * 1000 + int((await redis.time())[1]) // 1000
    await asyncio.sleep((window - now_ms % window + window * 0.8) % window / 1000)
    assert sum(d.allowed for d in await engine.check_many([check] * 20)) == 20
    await asyncio.sleep(window * 0.3 / 1000)
    after_edge = await engine.check_many([check] * 20)
    # A fixed window would admit all 20 again here; the weighted count must not.
    assert sum(d.allowed for d in after_edge) <= 6


async def test_leaky_bucket_spaces_requests(engine: LimiterEngine, unique: str):
    check = make(unique, Algorithm.LEAKY_BUCKET, limit=10, window_ms=1000)  # one per 100 ms
    assert (await engine.check(check)).allowed
    second = await engine.check(check)
    assert not second.allowed
    assert 50 <= second.retry_after_ms <= 100
    await asyncio.sleep(0.11)
    assert (await engine.check(check)).allowed


async def test_multiplier_scales_the_effective_limit(engine: LimiterEngine, unique: str, redis: Redis):
    await redis.set(keys.multiplier_key(unique), "0.5")
    check = make(unique, Algorithm.FIXED_WINDOW, limit=40)
    decisions = await engine.check_many([check] * 40)
    assert sum(d.allowed for d in decisions) == 20
    assert decisions[0].limit == 20


async def test_multiplier_never_drops_limit_below_one(engine: LimiterEngine, unique: str, redis: Redis):
    await redis.set(keys.multiplier_key(unique), "0.0001")
    decision = await engine.check(make(unique, Algorithm.SLIDING_WINDOW, limit=10))
    assert decision.allowed
    assert decision.limit == 1


@pytest.mark.parametrize("algorithm", ALL)
async def test_pause_blocks_everything_and_reports_ttl(
    engine: LimiterEngine, unique: str, redis: Redis, algorithm: Algorithm
):
    await redis.set(keys.pause_key(unique), "1", px=5000)
    decision = await engine.check(make(unique, algorithm, limit=100))
    assert not decision.allowed
    assert decision.reason == "paused"
    assert 4000 < decision.retry_after_ms <= 5000
    await redis.delete(keys.pause_key(unique))
    assert (await engine.check(make(unique, algorithm, limit=100))).allowed


async def test_counters_and_top_clients_are_recorded(engine: LimiterEngine, unique: str, redis: Redis):
    check = make(unique, Algorithm.FIXED_WINDOW, limit=3)
    await engine.check_many([check] * 5)
    allowed = blocked = 0
    async for key in redis.scan_iter(match=keys.counter_prefix(unique) + "*"):
        values = await redis.hgetall(key)
        allowed += int(values.get("a", 0))
        blocked += int(values.get("b", 0))
    assert (allowed, blocked) == (3, 2)
    hour = int((await redis.time())[0]) // 3600
    member = f"policy-{unique}|client"
    assert await redis.zscore(keys.top_key("a", hour), member) == 3
    assert await redis.zscore(keys.top_key("b", hour), member) == 2


async def test_lease_grants_partial_then_nothing(engine: LimiterEngine, unique: str):
    check = make(unique, Algorithm.TOKEN_BUCKET, limit=10, cost=6)
    granted, _ = await engine.lease(check)
    assert granted == 6
    granted, _ = await engine.lease(check)
    assert granted == 4  # only 4 left; a lease takes what remains
    granted, decision = await engine.lease(check)
    assert granted == 0
    assert decision.retry_after_ms > 0


async def test_switching_algorithm_uses_separate_state(engine: LimiterEngine, unique: str):
    bucket = make(unique, Algorithm.TOKEN_BUCKET, limit=2)
    assert sum(d.allowed for d in await engine.check_many([bucket] * 3)) == 2
    window = make(unique, Algorithm.SLIDING_WINDOW, limit=2)
    assert sum(d.allowed for d in await engine.check_many([window] * 3)) == 2


async def test_reset_clears_state(engine: LimiterEngine, unique: str):
    check = make(unique, Algorithm.FIXED_WINDOW, limit=1)
    assert (await engine.check(check)).allowed
    assert not (await engine.check(check)).allowed
    assert await engine.reset(unique) >= 1
    assert (await engine.check(check)).allowed


async def test_long_subject_is_hashed_not_truncated(engine: LimiterEngine, unique: str):
    base = "x" * 400
    first = make(unique, Algorithm.FIXED_WINDOW, limit=1, subject=base + "a")
    second = make(unique, Algorithm.FIXED_WINDOW, limit=1, subject=base + "b")
    assert (await engine.check(first)).allowed
    assert (await engine.check(second)).allowed
    assert not (await engine.check(first)).allowed


@pytest.mark.parametrize(
    ("algorithm", "limit", "burst", "expected"),
    [
        (Algorithm.TOKEN_BUCKET, 20, 0, 20),
        (Algorithm.TOKEN_BUCKET, 20, 5, 25),
        (Algorithm.FIXED_WINDOW, 20, 0, 20),
        (Algorithm.SLIDING_WINDOW, 20, 0, 20),
        (Algorithm.LEAKY_BUCKET, 20, 3, 4),
        (Algorithm.ADAPTIVE_HYBRID, 20, 0, 4),
    ],
)
async def test_lease_larger_than_capacity_is_clamped_not_refused(
    engine: LimiterEngine, unique: str, algorithm: Algorithm, limit: int, burst: int, expected: int
):
    """An SDK asking for 50 tokens from a small policy must get what fits, not nothing."""
    check = make(unique, algorithm, limit=limit, burst=burst, cost=50)
    granted, decision = await engine.lease(check)
    assert granted == expected
    assert decision.allowed
    again, _ = await engine.lease(check)
    assert again == 0, "the policy is now exhausted; a second lease must not over-admit"


async def test_lease_respects_an_adaptively_reduced_limit(engine: LimiterEngine, unique: str, redis: Redis):
    await redis.set(keys.multiplier_key(unique), "0.25")
    granted, decision = await engine.lease(make(unique, Algorithm.TOKEN_BUCKET, limit=40, cost=100))
    assert (granted, decision.limit) == (10, 10)

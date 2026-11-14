"""The adaptive control law, tested without any I/O."""

from __future__ import annotations

import pytest

from app.adaptive.controller import AdaptiveState, Signals, health_score, provider_cap, step
from app.adaptive.signals import LATENCY_BOUNDS_MS, percentile_from_buckets
from app.runtime_config import AdaptiveConfig

CONFIG = AdaptiveConfig()


def advance(state: AdaptiveState, signals: Signals, now: float, **overrides) -> AdaptiveState:
    arguments = {
        "now": now,
        "min_multiplier": 0.1,
        "max_multiplier": 1.0,
        "priority": 50,
        "target_latency_ms": 200.0,
        "config": CONFIG,
    }
    arguments.update(overrides)
    return step(state, signals, **arguments)


def test_health_is_perfect_when_every_signal_is_within_bounds():
    signals = Signals(requests=100, error_rate=0.0, throttle_rate=0.0, latency_p95_ms=150, cpu_percent=40)
    score, penalties = health_score(signals, 200, CONFIG)
    assert score == 100
    assert set(penalties.values()) == {0.0}


def test_health_with_no_signals_is_perfect_and_has_no_penalties():
    assert health_score(Signals(), 200, CONFIG) == (100.0, {})


def test_one_critical_signal_is_enough_to_fall_below_the_decrease_threshold():
    signals = Signals(requests=100, error_rate=0.0, throttle_rate=0.0, latency_p95_ms=100, cpu_percent=99)
    score, penalties = health_score(signals, 200, CONFIG)
    assert penalties["cpu"] == 1.0
    assert score < CONFIG.decrease_threshold


def test_latency_penalty_scales_between_target_and_critical():
    at_target, _ = health_score(Signals(latency_p95_ms=200), 200, CONFIG)
    halfway, _ = health_score(Signals(latency_p95_ms=400), 200, CONFIG)
    critical, _ = health_score(Signals(latency_p95_ms=600), 200, CONFIG)
    assert at_target == 100
    assert halfway == 50
    assert critical == 0


def test_unhealthy_system_cuts_the_limit_multiplicatively():
    state = advance(AdaptiveState(), Signals(requests=50, latency_p95_ms=600), now=100)
    assert state.action == "decrease"
    assert state.multiplier == pytest.approx(0.5)  # health 0 -> the maximum cut


def test_decrease_respects_the_cooldown():
    bad = Signals(requests=50, latency_p95_ms=600)
    first = advance(AdaptiveState(), bad, now=100)
    second = advance(first, bad, now=100.5)
    assert second.multiplier == first.multiplier
    assert second.action == "hold"
    third = advance(second, bad, now=100 + CONFIG.decrease_cooldown_seconds)
    assert third.multiplier < first.multiplier


def test_healthy_system_recovers_additively_up_to_the_ceiling():
    state = AdaptiveState(multiplier=0.5)
    good = Signals(requests=50, latency_p95_ms=50, error_rate=0.0)
    seen = []
    for tick in range(20):
        state = advance(state, good, now=200 + tick)
        seen.append(state.multiplier)
    assert seen[0] == pytest.approx(0.55)
    assert seen[-1] == 1.0
    assert seen == sorted(seen)


def test_dead_band_holds_steady():
    signals = Signals(latency_p95_ms=340)  # health 65: between the thresholds
    score, _ = health_score(signals, 200, CONFIG)
    assert CONFIG.decrease_threshold <= score < CONFIG.increase_threshold
    state = advance(AdaptiveState(multiplier=0.7), signals, now=10)
    assert state.multiplier == 0.7
    assert state.action == "hold"


def test_multiplier_never_leaves_its_bounds():
    state = AdaptiveState()
    bad = Signals(requests=10, error_rate=1.0, latency_p95_ms=5000)
    for tick in range(50):
        state = advance(state, bad, now=tick * 10, min_multiplier=0.25)
    assert state.multiplier == 0.25
    good = Signals(requests=10, error_rate=0.0, latency_p95_ms=10)
    for tick in range(100):
        state = advance(state, good, now=1000 + tick, max_multiplier=1.5)
    assert state.multiplier == 1.5


def test_high_priority_policies_shed_less():
    bad = Signals(requests=50, latency_p95_ms=450)
    low = advance(AdaptiveState(), bad, now=10, priority=0)
    high = advance(AdaptiveState(), bad, now=10, priority=100)
    assert low.multiplier < high.multiplier < 1.0


def test_without_signals_the_limit_drifts_back_to_base():
    lowered = advance(AdaptiveState(multiplier=0.5), Signals(), now=1)
    assert lowered.multiplier == pytest.approx(0.55)
    raised = advance(AdaptiveState(multiplier=1.4), Signals(), now=1, max_multiplier=2.0)
    assert raised.multiplier == pytest.approx(1.35)
    steady = advance(AdaptiveState(), Signals(), now=1, max_multiplier=2.0)
    assert steady.multiplier == 1.0


def test_closed_loop_settles_at_the_capacity_of_the_service():
    """Simulate a service whose latency climbs once load passes its capacity.

    The controller should bring the limit down to roughly that capacity and keep it
    there, without collapsing to the floor or oscillating wildly.
    """
    base_limit, capacity, target = 100.0, 60.0, 200.0
    state = AdaptiveState()
    history: list[float] = []
    for tick in range(300):
        load = base_limit * state.multiplier
        overload = max(0.0, load - capacity) / capacity
        latency = 100.0 + 2000.0 * overload  # healthy at 100 ms, degrades fast when overloaded
        state = advance(
            state,
            Signals(requests=int(load), latency_p95_ms=latency),
            now=float(tick),
            target_latency_ms=target,
        )
        history.append(base_limit * state.multiplier)
    settled = history[100:]
    assert min(settled) > 30, "collapsed far below what the service can take"
    assert max(settled) < 80, "kept overshooting the capacity"
    assert 45 <= sum(settled) / len(settled) <= 70


def test_recovers_fully_once_the_incident_ends():
    state = AdaptiveState()
    for tick in range(20):
        state = advance(state, Signals(requests=10, cpu_percent=99), now=float(tick * 3))
    assert state.multiplier < 0.3
    for tick in range(40):
        state = advance(state, Signals(requests=10, cpu_percent=30), now=100.0 + tick)
    assert state.multiplier == 1.0


def test_provider_cap_paces_remaining_quota_until_reset():
    # 600 requests left, 60 s to reset, 10% held back: 9 per second.
    cap = provider_cap(remaining=600, reset_in_seconds=60, window_seconds=1, base_limit=20, safety_margin=0.1)
    assert cap == pytest.approx(0.45)
    assert (
        provider_cap(remaining=None, reset_in_seconds=60, window_seconds=1, base_limit=20, safety_margin=0.1)
        is None
    )
    assert (
        provider_cap(remaining=0, reset_in_seconds=30, window_seconds=1, base_limit=20, safety_margin=0.1)
        == 0
    )


def test_percentile_interpolates_within_a_bucket():
    buckets = [0] * (len(LATENCY_BOUNDS_MS) + 1)
    assert percentile_from_buckets(buckets, 0.95) is None
    buckets[6] = 100  # everything between 50 and 100 ms
    assert percentile_from_buckets(buckets, 0.5) == pytest.approx(75)
    assert percentile_from_buckets(buckets, 0.95) == pytest.approx(97.5)
    buckets[-1] = 1000  # a flood of very slow requests lands in the open-ended bucket
    assert percentile_from_buckets(buckets, 0.95) == LATENCY_BOUNDS_MS[-1]

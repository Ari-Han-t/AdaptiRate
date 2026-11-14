"""The adaptive control law. Pure functions: signals in, new multiplier out.

Health is a 0-100 score built from whichever signals are present. Below the
decrease threshold the limit is cut multiplicatively, in proportion to how bad
things are; above the increase threshold it grows additively. The gap between
the two thresholds is a dead band that prevents oscillation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..runtime_config import AdaptiveConfig

# Relative importance of each signal in the average part of the score.
WEIGHTS: dict[str, float] = {
    "latency": 3.0,
    "errors": 3.0,
    "throttled": 3.0,
    "timeouts": 2.0,
    "queue": 2.0,
    "cpu": 1.5,
    "memory": 1.0,
}


# How much of the score is decided by the single worst signal.
WORST_SHARE = 0.7


@dataclass(frozen=True, slots=True)
class Signals:
    """Observations over the signal window. None means 'not reported'."""

    requests: int = 0
    error_rate: float | None = None
    throttle_rate: float | None = None
    timeout_rate: float | None = None
    latency_p95_ms: float | None = None
    latency_avg_ms: float | None = None
    in_flight: int | None = None
    queue_depth: int | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None

    @property
    def empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.error_rate,
                self.throttle_rate,
                self.timeout_rate,
                self.latency_p95_ms,
                self.queue_depth,
                self.cpu_percent,
                self.memory_percent,
            )
        )


@dataclass(frozen=True, slots=True)
class AdaptiveState:
    multiplier: float = 1.0
    health: float = 100.0
    last_decrease_at: float = 0.0
    action: str = "hold"
    penalties: dict[str, float] = field(default_factory=dict)


def _penalty(value: float, ok: float, critical: float) -> float:
    """0 at or below `ok`, 1 at or above `critical`, linear between."""
    if value <= ok:
        return 0.0
    if value >= critical:
        return 1.0
    return (value - ok) / (critical - ok)


def health_score(
    signals: Signals, target_latency_ms: float | None, config: AdaptiveConfig
) -> tuple[float, dict[str, float]]:
    penalties: dict[str, float] = {}
    if signals.latency_p95_ms is not None:
        target = target_latency_ms or config.default_target_latency_ms
        penalties["latency"] = _penalty(
            signals.latency_p95_ms, target, target * config.latency_critical_factor
        )
    if signals.error_rate is not None:
        penalties["errors"] = _penalty(signals.error_rate, config.error_rate_ok, config.error_rate_critical)
    if signals.throttle_rate is not None:
        penalties["throttled"] = _penalty(
            signals.throttle_rate, config.throttle_rate_ok, config.throttle_rate_critical
        )
    if signals.timeout_rate is not None:
        penalties["timeouts"] = _penalty(
            signals.timeout_rate, config.error_rate_ok, config.error_rate_critical
        )
    if signals.queue_depth is not None:
        penalties["queue"] = _penalty(float(signals.queue_depth), config.queue_ok, config.queue_critical)
    if signals.cpu_percent is not None:
        penalties["cpu"] = _penalty(signals.cpu_percent, config.cpu_ok, config.cpu_critical)
    if signals.memory_percent is not None:
        penalties["memory"] = _penalty(signals.memory_percent, config.memory_ok, config.memory_critical)

    if not penalties:
        return 100.0, penalties
    total_weight = sum(WEIGHTS[name] for name in penalties)
    average = sum(WEIGHTS[name] * value for name, value in penalties.items()) / total_weight
    worst = max(penalties.values())
    # Mostly the worst signal, partly the weighted average. A single critical signal must
    # be enough to act on even when every other signal looks fine; the average lets
    # several mild problems add up.
    return round(100.0 * (1.0 - (WORST_SHARE * worst + (1.0 - WORST_SHARE) * average)), 2), penalties


def step(
    state: AdaptiveState,
    signals: Signals,
    *,
    now: float,
    min_multiplier: float,
    max_multiplier: float,
    priority: int,
    target_latency_ms: float | None,
    config: AdaptiveConfig,
) -> AdaptiveState:
    """Advance the controller by one tick."""
    health, penalties = health_score(signals, target_latency_ms, config)
    multiplier = state.multiplier
    last_decrease_at = state.last_decrease_at
    action = "hold"

    if signals.empty:
        # Nothing to judge by. Drift back to the configured limit instead of guessing.
        if multiplier < 1.0:
            multiplier = min(1.0, multiplier + config.increase_step)
            action = "recover"
        elif multiplier > 1.0:
            multiplier = max(1.0, multiplier - config.increase_step)
            action = "recover"
    elif health < config.decrease_threshold:
        if now - last_decrease_at >= config.decrease_cooldown_seconds:
            severity = (config.decrease_threshold - health) / config.decrease_threshold
            # Priority 100 sheds half as hard as priority 50; priority 0 sheds 1.5x.
            severity *= 1.5 - priority / 100.0
            cut = min(config.max_decrease, max(0.02, config.max_decrease * severity))
            multiplier *= 1.0 - cut
            last_decrease_at = now
            action = "decrease"
    elif health >= config.increase_threshold and multiplier < max_multiplier:
        multiplier += config.increase_step
        action = "increase"

    multiplier = min(max_multiplier, max(min_multiplier, multiplier))
    if abs(multiplier - state.multiplier) < 1e-9 and action in ("increase", "decrease", "recover"):
        action = "hold"
    return AdaptiveState(
        multiplier=round(multiplier, 6),
        health=health,
        last_decrease_at=last_decrease_at,
        action=action,
        penalties=penalties,
    )


def provider_cap(
    *,
    remaining: float | None,
    reset_in_seconds: float | None,
    window_seconds: float,
    base_limit: int,
    safety_margin: float,
) -> float | None:
    """Multiplier ceiling that spreads a provider's remaining quota over the time
    until it resets. None when the provider has not told us enough to pace."""
    if remaining is None or reset_in_seconds is None:
        return None
    usable = max(0.0, remaining * (1.0 - safety_margin))
    rate_per_second = usable / max(reset_in_seconds, 1.0)
    return max(0.0, rate_per_second * window_seconds / base_limit)

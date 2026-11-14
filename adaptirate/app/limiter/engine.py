"""Executes rate-limit decisions. One Redis round trip per check, or one per batch."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib import resources

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from . import keys


class Algorithm(StrEnum):
    TOKEN_BUCKET = "token_bucket"  # noqa: S105
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW = "fixed_window"
    LEAKY_BUCKET = "leaky_bucket"
    ADAPTIVE_HYBRID = "adaptive_hybrid"


ALGORITHM_INFO: dict[Algorithm, dict[str, str]] = {
    Algorithm.TOKEN_BUCKET: {
        "label": "Token Bucket",
        "summary": "Refills at limit/window and holds up to limit + burst tokens.",
        "best_for": "APIs that should tolerate short bursts above a steady rate.",
        "burst": "Extra tokens the bucket can hold above the limit.",
    },
    Algorithm.SLIDING_WINDOW: {
        "label": "Sliding Window",
        "summary": "Weighted count of the current and previous window; no edge bursts.",
        "best_for": "Strict per-window quotas with constant memory per client.",
        "burst": "Not used.",
    },
    Algorithm.FIXED_WINDOW: {
        "label": "Fixed Window",
        "summary": "One counter per aligned window. Cheapest; up to 2x limit across a window edge.",
        "best_for": "Coarse quotas such as daily or monthly allowances.",
        "burst": "Not used.",
    },
    Algorithm.LEAKY_BUCKET: {
        "label": "Leaky Bucket",
        "summary": "Smooths traffic to one request every window/limit (GCRA).",
        "best_for": "Protecting a downstream that needs evenly spaced calls.",
        "burst": "Requests that may arrive together before spacing is enforced.",
    },
    Algorithm.ADAPTIVE_HYBRID: {
        "label": "Adaptive Hybrid",
        "summary": "Small token bucket for burst shaping plus a sliding-window hard ceiling.",
        "best_for": "Adaptive policies: smooth under pressure, strict on the total.",
        "burst": "Bucket size; defaults to one fifth of the limit.",
    },
}

REASONS = ("ok", "limit", "paused", "too_large")


@dataclass(frozen=True, slots=True)
class Check:
    policy_id: str
    policy_name: str
    algorithm: Algorithm
    limit: int
    window_ms: int
    burst: int
    subject: str
    cost: int = 1
    record: bool = True
    # False while probing for a lease: a refused probe is not a refused request.
    record_blocks: bool = True


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int
    reset_after_ms: int
    reason: str


def _load_script(name: str) -> str:
    package = resources.files("app.limiter") / "lua"
    parts = [(package / f"{part}.lua").read_text(encoding="utf-8") for part in ("_header", name, "_footer")]
    return "\n".join(parts)


class LimiterEngine:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._scripts: dict[Algorithm, AsyncScript] = {
            algorithm: redis.register_script(_load_script(algorithm.value)) for algorithm in Algorithm
        }

    @staticmethod
    def _args(check: Check) -> tuple[list[str], list[str | int]]:
        subject = keys.normalize_subject(check.subject)
        script_keys = [
            keys.state_key(check.policy_id, check.algorithm.value, subject),
            keys.multiplier_key(check.policy_id),
            keys.pause_key(check.policy_id),
        ]
        argv: list[str | int] = [
            check.limit,
            check.window_ms,
            check.burst,
            check.cost,
            keys.counter_prefix(check.policy_id),
            keys.GLOBAL_COUNTER_PREFIX,
            keys.TOP_PREFIX,
            f"{check.policy_name}|{subject}",
            "1" if check.record else "0",
            check.policy_name,
            "1" if check.record_blocks else "0",
        ]
        return script_keys, argv

    @staticmethod
    def _decision(raw: Sequence[int]) -> Decision:
        return Decision(
            allowed=raw[0] == 1,
            limit=int(raw[1]),
            remaining=int(raw[2]),
            retry_after_ms=int(raw[3]),
            reset_after_ms=int(raw[4]),
            reason=REASONS[int(raw[5])],
        )

    async def check(self, check: Check) -> Decision:
        script_keys, argv = self._args(check)
        raw = await self._scripts[check.algorithm](keys=script_keys, args=argv)
        return self._decision(raw)

    async def check_many(self, checks: Sequence[Check]) -> list[Decision]:
        if not checks:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for check in checks:
            script_keys, argv = self._args(check)
            await self._scripts[check.algorithm](keys=script_keys, args=argv, client=pipe)
        results = await pipe.execute()
        return [self._decision(raw) for raw in results]

    @staticmethod
    def _capacity(check: Check, effective_limit: int) -> int:
        """The most one request can ever be granted, mirroring each Lua script."""
        if check.algorithm is Algorithm.TOKEN_BUCKET:
            return effective_limit + check.burst
        if check.algorithm is Algorithm.LEAKY_BUCKET:
            return check.burst + 1
        if check.algorithm is Algorithm.ADAPTIVE_HYBRID:
            bucket = check.burst if check.burst > 0 else max(1, -(-effective_limit // 5))
            return min(effective_limit, bucket)
        return effective_limit

    async def lease(self, check: Check) -> tuple[int, Decision]:
        """Grant up to check.cost units, falling back to whatever fits.

        A request larger than the policy can ever grant is clamped to its capacity
        rather than refused, so an SDK configured with a generous lease size still works
        against a small or adaptively reduced limit. Unused leased units are simply
        lost: a lease can under-admit but never over-admit.
        """
        check = replace(check, record_blocks=False)
        decision = await self.check(check)
        if decision.allowed:
            return check.cost, decision
        if decision.reason == "paused":
            return 0, decision

        if decision.reason == "too_large":
            fit = min(check.cost - 1, self._capacity(check, decision.limit))
        else:
            fit = min(decision.remaining, check.cost)
        if fit <= 0:
            return 0, decision

        second = await self.check(replace(check, cost=fit))
        if second.allowed:
            return fit, second
        # The clamped amount is possible in principle but not right now; take what is left.
        if 0 < second.remaining < fit:
            third = await self.check(replace(check, cost=second.remaining))
            return (second.remaining if third.allowed else 0), third
        return 0, second

    async def reset(self, policy_id: str) -> int:
        """Delete all limiter state for a policy. Returns the number of keys removed."""
        removed = 0
        batch: list[str] = []
        async for key in self._redis.scan_iter(match=keys.state_pattern(policy_id), count=1000):
            batch.append(key)
            if len(batch) >= 500:
                removed += await self._redis.unlink(*batch)
                batch.clear()
        if batch:
            removed += await self._redis.unlink(*batch)
        return removed

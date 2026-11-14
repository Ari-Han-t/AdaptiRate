"""Executes rate-limit decisions. One Redis round trip per check, or one per batch."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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

    async def lease(self, check: Check) -> tuple[int, Decision]:
        """Grant up to check.cost units. Falls back to whatever is left when the full
        amount does not fit. Unused leased units are simply lost, so a lease can
        under-admit but never over-admit."""
        decision = await self.check(check)
        if decision.allowed:
            return check.cost, decision
        if decision.reason == "paused" or decision.remaining <= 0:
            return 0, decision
        partial = Check(
            policy_id=check.policy_id,
            policy_name=check.policy_name,
            algorithm=check.algorithm,
            limit=check.limit,
            window_ms=check.window_ms,
            burst=check.burst,
            subject=check.subject,
            cost=min(decision.remaining, check.cost),
            record=check.record,
        )
        second = await self.check(partial)
        return (partial.cost if second.allowed else 0), second

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

"""Shared view of each upstream provider's quota, kept in Redis."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from ..limiter import keys
from ..limiter.policy import CompiledPolicy
from .parsers import BUILTIN, HeaderMap, Observation, parse

MAX_PAUSE_SECONDS = 3600.0
STATE_TTL_SECONDS = 86400


@dataclass(frozen=True, slots=True)
class ProviderState:
    name: str
    remaining_requests: float | None = None
    limit_requests: float | None = None
    reset_requests_at: float | None = None
    remaining_tokens: float | None = None
    limit_tokens: float | None = None
    reset_tokens_at: float | None = None
    paused_until: float | None = None
    consecutive_throttles: int = 0
    responses: int = 0
    throttled: int = 0
    updated_at: float | None = None

    def remaining(self, dimension: str) -> float | None:
        return self.remaining_tokens if dimension == "tokens" else self.remaining_requests

    def limit(self, dimension: str) -> float | None:
        return self.limit_tokens if dimension == "tokens" else self.limit_requests

    def reset_in(self, dimension: str, now: float) -> float | None:
        """Seconds until this quota resets, or None when that is unknown.

        Once the reset time has passed, the remaining count we hold is stale: the
        provider has refilled. Reporting 0 here would keep pacing against the old
        (possibly zero) remaining figure and hold the limit near zero indefinitely.
        """
        reset_at = self.reset_tokens_at if dimension == "tokens" else self.reset_requests_at
        if reset_at is None or reset_at <= now:
            return None
        return reset_at - now

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "builtin": self.name in BUILTIN,
            "remaining_requests": self.remaining_requests,
            "limit_requests": self.limit_requests,
            "reset_requests_in": self.reset_in("requests", now),
            "remaining_tokens": self.remaining_tokens,
            "limit_tokens": self.limit_tokens,
            "reset_tokens_in": self.reset_in("tokens", now),
            "paused_ms": int(max(0.0, (self.paused_until or 0) - now) * 1000),
            "consecutive_throttles": self.consecutive_throttles,
            "responses": self.responses,
            "throttled": self.throttled,
            "updated_at": self.updated_at,
        }


def _float(raw: dict[str, str], name: str) -> float | None:
    value = raw.get(name)
    return float(value) if value not in (None, "") else None


def state_from_hash(name: str, raw: dict[str, str]) -> ProviderState:
    return ProviderState(
        name=name,
        remaining_requests=_float(raw, "remaining_requests"),
        limit_requests=_float(raw, "limit_requests"),
        reset_requests_at=_float(raw, "reset_requests_at"),
        remaining_tokens=_float(raw, "remaining_tokens"),
        limit_tokens=_float(raw, "limit_tokens"),
        reset_tokens_at=_float(raw, "reset_tokens_at"),
        paused_until=_float(raw, "paused_until"),
        consecutive_throttles=int(raw.get("consecutive_throttles", 0)),
        responses=int(raw.get("responses", 0)),
        throttled=int(raw.get("throttled", 0)),
        updated_at=_float(raw, "updated_at"),
    )


async def get_state(redis: Redis, name: str) -> ProviderState:
    return state_from_hash(name, await redis.hgetall(keys.provider_state_key(name)))


def pause_seconds(observation: Observation, consecutive: int) -> float:
    """How long to stop sending after a throttle. Prefers what the provider asked for;
    otherwise exponential backoff, since retrying immediately only earns another 429.

    The backoff keeps doubling up to an hour. Some refusals carry no Retry-After and last
    a long time (Anthropic's monthly spend cap clears only on the 1st), and a 60-second
    ceiling would keep probing such a provider every minute indefinitely. One 429 still
    costs only a 2-second pause; it takes about a dozen in a row to reach the hour.
    """
    if observation.retry_after_seconds is not None:
        return min(MAX_PAUSE_SECONDS, max(0.1, observation.retry_after_seconds))
    for remaining, reset_in in (
        (observation.remaining_requests, observation.reset_requests_in),
        (observation.remaining_tokens, observation.reset_tokens_in),
    ):
        if remaining == 0 and reset_in is not None:
            return min(MAX_PAUSE_SECONDS, max(0.1, reset_in))
    return min(MAX_PAUSE_SECONDS, float(2 ** min(max(consecutive, 1), 12)))


async def record_response(
    redis: Redis,
    provider: str,
    status: int,
    headers: dict[str, str],
    *,
    policies: list[CompiledPolicy],
    custom: dict[str, str] | None = None,
    now: float | None = None,
) -> tuple[Observation, float]:
    """Fold one upstream response into the provider's state.

    Returns the parsed observation and the pause (seconds) applied to every policy
    bound to this provider; 0 when the response was not a throttle.
    """
    now = time.time() if now is None else now
    header_map = HeaderMap(**custom) if custom else None
    observation = parse(provider, status, headers, now=now, custom=header_map)
    key = keys.provider_state_key(provider)

    fields: dict[str, str] = {"updated_at": repr(now)}
    for name, value in (
        ("remaining_requests", observation.remaining_requests),
        ("limit_requests", observation.limit_requests),
        ("remaining_tokens", observation.remaining_tokens),
        ("limit_tokens", observation.limit_tokens),
    ):
        if value is not None:
            fields[name] = repr(value)
    if observation.reset_requests_in is not None:
        fields["reset_requests_at"] = repr(now + observation.reset_requests_in)
    if observation.reset_tokens_in is not None:
        fields["reset_tokens_at"] = repr(now + observation.reset_tokens_in)

    pause = 0.0
    pipe = redis.pipeline(transaction=False)
    pipe.hincrby(key, "responses", 1)
    if observation.throttled:
        consecutive = int(await redis.hincrby(key, "consecutive_throttles", 1))
        pause = pause_seconds(observation, consecutive)
        fields["paused_until"] = repr(now + pause)
        pipe.hincrby(key, "throttled", 1)
        for policy in policies:
            if policy.provider == provider:
                # Extend, never shorten, an existing pause.
                pipe.set(keys.pause_key(policy.id), "1", px=int(pause * 1000), nx=True)
                pipe.pexpire(keys.pause_key(policy.id), int(pause * 1000), gt=True)
    elif status < 400:
        fields["consecutive_throttles"] = "0"
    pipe.hset(key, mapping=fields)
    pipe.expire(key, STATE_TTL_SECONDS)
    await pipe.execute()
    return observation, pause

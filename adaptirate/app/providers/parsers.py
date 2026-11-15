"""Reads quota information out of upstream provider response headers. Pure; no I/O.

Header names follow each provider's public documentation. Providers change these
from time to time; a custom header map (see CustomProviderIn) overrides the built-ins
without a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}


@dataclass(frozen=True, slots=True)
class HeaderMap:
    remaining_requests: str | None = None
    limit_requests: str | None = None
    reset_requests: str | None = None
    remaining_tokens: str | None = None
    limit_tokens: str | None = None
    reset_tokens: str | None = None
    reset_format: str = "seconds"  # seconds | epoch | duration | rfc3339
    # "continuous": the quota refills steadily and the reset header says when it will be
    # full. "window": nothing comes back until the reset time.
    refill: str = "continuous"


@dataclass(frozen=True, slots=True)
class Observation:
    throttled: bool
    retry_after_seconds: float | None = None
    remaining_requests: float | None = None
    limit_requests: float | None = None
    reset_requests_in: float | None = None
    remaining_tokens: float | None = None
    limit_tokens: float | None = None
    reset_tokens_in: float | None = None


_OPENAI_STYLE = HeaderMap(
    remaining_requests="x-ratelimit-remaining-requests",
    limit_requests="x-ratelimit-limit-requests",
    reset_requests="x-ratelimit-reset-requests",
    remaining_tokens="x-ratelimit-remaining-tokens",
    limit_tokens="x-ratelimit-limit-tokens",
    reset_tokens="x-ratelimit-reset-tokens",
    reset_format="duration",
)

BUILTIN: dict[str, HeaderMap] = {
    # Refill model, as documented (checked 2026-11):
    # - Anthropic states its limits are token buckets, continuously replenished.
    # - OpenAI documents the headers but not whether quotas refill continuously or in
    #   fixed windows. Continuous is assumed here, matching observed behaviour. Override
    #   with PUT /providers/openai {"refill": "window", ...} if that proves wrong.
    # - Groq's request headers describe a daily quota and its token headers a per-minute
    #   one. The pacing formula handles either, because it divides by the reported time
    #   to reset rather than assuming a window length.
    # - GitHub's quota is a fixed hourly window (refill="window" below).
    "openai": _OPENAI_STYLE,
    "groq": _OPENAI_STYLE,
    "anthropic": HeaderMap(
        remaining_requests="anthropic-ratelimit-requests-remaining",
        limit_requests="anthropic-ratelimit-requests-limit",
        reset_requests="anthropic-ratelimit-requests-reset",
        remaining_tokens="anthropic-ratelimit-tokens-remaining",
        limit_tokens="anthropic-ratelimit-tokens-limit",
        reset_tokens="anthropic-ratelimit-tokens-reset",
        reset_format="rfc3339",
    ),
    "github": HeaderMap(
        remaining_requests="x-ratelimit-remaining",
        limit_requests="x-ratelimit-limit",
        reset_requests="x-ratelimit-reset",
        reset_format="epoch",
        refill="window",
    ),
    # Gemini does not publish quota headers; only 429 and Retry-After are usable.
    "gemini": HeaderMap(),
}


def refills_continuously(provider: str, custom: dict[str, str] | None) -> bool:
    """Whether a provider's quota refills steadily rather than all at once."""
    if custom is not None:
        return custom.get("refill", "continuous") == "continuous"
    known = BUILTIN.get(provider)
    return known is None or known.refill == "continuous"


def parse_duration(text: str) -> float | None:
    """'6m0s', '20ms', '2m59.56s', or a bare number of seconds."""
    text = text.strip().lower()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    total = 0.0
    position = 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        position = match.end()
    if position != len(text) or position == 0:
        return None
    return total


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _reset_in(text: str | None, reset_format: str, now: float) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if reset_format == "duration":
        return parse_duration(text)
    if reset_format == "seconds":
        return _number(text)
    if reset_format == "epoch":
        value = _number(text)
        if value is None:
            return None
        if value > 1e11:  # milliseconds
            value /= 1000.0
        return max(0.0, value - now)
    if reset_format == "rfc3339":
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max(0.0, moment.timestamp() - now)
    return None


def parse_retry_after(headers: dict[str, str], now: float) -> float | None:
    """Retry-After as seconds or an HTTP date; retry-after-ms when a provider sends it."""
    millis = _number(headers.get("retry-after-ms"))
    if millis is not None:
        return millis / 1000.0
    raw = headers.get("retry-after")
    if raw is None:
        return None
    seconds = _number(raw)
    if seconds is not None:
        return seconds
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, moment.timestamp() - now)


def parse(
    provider: str,
    status: int,
    headers: dict[str, str],
    *,
    now: float,
    custom: HeaderMap | None = None,
) -> Observation:
    lowered = {name.lower(): value for name, value in headers.items()}
    header_map = custom or BUILTIN.get(provider, HeaderMap())

    def get(name: str | None) -> str | None:
        return lowered.get(name.lower()) if name else None

    remaining_requests = _number(get(header_map.remaining_requests))
    retry_after = parse_retry_after(lowered, now)
    throttled = status == 429
    # GitHub signals primary-limit exhaustion with 403 and remaining 0, and secondary
    # limits with 403 plus Retry-After.
    if provider == "github" and status == 403 and (remaining_requests == 0 or retry_after is not None):
        throttled = True

    return Observation(
        throttled=throttled,
        retry_after_seconds=retry_after,
        remaining_requests=remaining_requests,
        limit_requests=_number(get(header_map.limit_requests)),
        reset_requests_in=_reset_in(get(header_map.reset_requests), header_map.reset_format, now),
        remaining_tokens=_number(get(header_map.remaining_tokens)),
        limit_tokens=_number(get(header_map.limit_tokens)),
        reset_tokens_in=_reset_in(get(header_map.reset_tokens), header_map.reset_format, now),
    )

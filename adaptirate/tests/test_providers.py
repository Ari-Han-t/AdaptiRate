"""Provider header parsing. Header sets mirror each provider's documented format."""

from __future__ import annotations

import pytest

from app.providers.parsers import HeaderMap, Observation, parse, parse_duration
from app.providers.service import pause_seconds

NOW = 1_700_000_000.0


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1s", 1),
        ("20ms", 0.02),
        ("6m0s", 360),
        ("2m59.56s", 179.56),
        ("1h2m3s", 3723),
        ("7", 7),
        ("0.5", 0.5),
    ],
)
def test_parse_duration(text: str, seconds: float):
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "soon", "5 minutes", "1s extra", "s"])
def test_parse_duration_rejects_garbage(text: str):
    assert parse_duration(text) is None


def test_openai_headers():
    observation = parse(
        "openai",
        200,
        {
            "x-ratelimit-limit-requests": "10000",
            "x-ratelimit-remaining-requests": "9987",
            "x-ratelimit-reset-requests": "6ms",
            "x-ratelimit-limit-tokens": "2000000",
            "x-ratelimit-remaining-tokens": "1999000",
            "x-ratelimit-reset-tokens": "1m30s",
        },
        now=NOW,
    )
    assert not observation.throttled
    assert observation.remaining_requests == 9987
    assert observation.limit_requests == 10000
    assert observation.reset_requests_in == pytest.approx(0.006)
    assert observation.remaining_tokens == 1_999_000
    assert observation.reset_tokens_in == 90


def test_groq_uses_the_same_header_family():
    observation = parse(
        "groq",
        200,
        {"X-RateLimit-Remaining-Requests": "14370", "X-RateLimit-Reset-Requests": "2m59.56s"},
        now=NOW,
    )
    assert observation.remaining_requests == 14370
    assert observation.reset_requests_in == pytest.approx(179.56)


def test_anthropic_headers_use_rfc3339_reset_times():
    observation = parse(
        "anthropic",
        429,
        {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "0",
            "anthropic-ratelimit-requests-reset": "2023-11-14T22:13:50Z",  # NOW + 30 s
            "anthropic-ratelimit-tokens-remaining": "12000",
            "retry-after": "30",
        },
        now=NOW,
    )
    assert observation.throttled
    assert observation.retry_after_seconds == 30
    assert observation.remaining_requests == 0
    assert observation.reset_requests_in == pytest.approx(30)
    assert observation.remaining_tokens == 12000


def test_github_primary_limit_is_a_403_with_zero_remaining():
    headers = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(int(NOW) + 900),
    }
    observation = parse("github", 403, headers, now=NOW)
    assert observation.throttled
    assert observation.reset_requests_in == 900
    # A plain permission error is not a throttle.
    assert not parse("github", 403, {"x-ratelimit-remaining": "4999"}, now=NOW).throttled


def test_github_secondary_limit_is_a_403_with_retry_after():
    assert parse("github", 403, {"retry-after": "60", "x-ratelimit-remaining": "4000"}, now=NOW).throttled


def test_gemini_has_no_quota_headers_only_the_status():
    observation = parse("gemini", 429, {"content-type": "application/json"}, now=NOW)
    assert observation.throttled
    assert observation.remaining_requests is None
    assert observation.retry_after_seconds is None


def test_custom_header_map():
    custom = HeaderMap(
        remaining_requests="X-Quota-Left", reset_requests="X-Quota-Reset", reset_format="epoch"
    )
    observation = parse(
        "internal", 200, {"x-quota-left": "12", "x-quota-reset": str(int(NOW) + 45)}, now=NOW, custom=custom
    )
    assert observation.remaining_requests == 12
    assert observation.reset_requests_in == 45


def test_unknown_provider_still_understands_429_and_retry_after():
    observation = parse("something-else", 429, {"Retry-After": "12"}, now=NOW)
    assert observation.throttled
    assert observation.retry_after_seconds == 12


def test_retry_after_as_http_date_and_milliseconds():
    dated = parse("x", 429, {"retry-after": "Tue, 14 Nov 2023 22:13:40 GMT"}, now=NOW)
    assert dated.retry_after_seconds == pytest.approx(20)
    millis = parse("x", 429, {"retry-after-ms": "1500", "retry-after": "2"}, now=NOW)
    assert millis.retry_after_seconds == 1.5


def test_malformed_values_are_ignored_rather_than_raised():
    observation = parse(
        "openai",
        200,
        {
            "x-ratelimit-remaining-requests": "lots",
            "x-ratelimit-reset-requests": "whenever",
            "retry-after": "-5",
        },
        now=NOW,
    )
    assert observation.remaining_requests is None
    assert observation.reset_requests_in is None
    assert observation.retry_after_seconds is None


def test_pause_prefers_retry_after_then_reset_then_backoff():
    assert pause_seconds(Observation(throttled=True, retry_after_seconds=7), 1) == 7
    exhausted = Observation(throttled=True, remaining_requests=0, reset_requests_in=42)
    assert pause_seconds(exhausted, 1) == 42
    blind = Observation(throttled=True)
    assert [pause_seconds(blind, n) for n in (1, 2, 3, 6)] == [2, 4, 8, 64]
    # A hostile or broken Retry-After cannot park traffic forever.
    assert pause_seconds(Observation(throttled=True, retry_after_seconds=10**9), 1) == 3600

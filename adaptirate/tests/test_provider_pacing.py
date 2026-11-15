"""How a provider's quota headers turn into a limit, for both kinds of quota."""

from __future__ import annotations

import pytest

from app.adaptive.controller import provider_cap
from app.providers.parsers import Observation, parse, refills_continuously
from app.providers.service import MAX_PAUSE_SECONDS, pause_seconds
from app.worker import Worker

API = "/api/v1"
COMMON = {"window_seconds": 60, "base_limit": 600, "safety_margin": 0.1}


def test_continuous_refill_providers_are_paced_by_their_whole_quota():
    """An OpenAI-style provider reporting 0 remaining and "full again in 60 s" still refills
    at limit/60 per second. Pacing it by remaining alone stopped every call for a minute."""
    exhausted = {"remaining": 0, "reset_in_seconds": 60}
    assert provider_cap(**exhausted, **COMMON, quota_limit=60, continuous=True) == pytest.approx(0.09)
    assert provider_cap(**exhausted, **COMMON, quota_limit=60, continuous=False) == 0
    # Without a limit header there is nothing to infer the refill rate from.
    assert provider_cap(**exhausted, **COMMON, quota_limit=None, continuous=True) == 0


def test_a_full_continuous_quota_does_not_cap_the_limit():
    # Full bucket: the provider reports it will be "full again" almost immediately.
    cap = provider_cap(remaining=60, reset_in_seconds=0.01, quota_limit=60, continuous=True, **COMMON)
    assert cap is not None and cap >= 1


def test_which_providers_refill_continuously():
    assert refills_continuously("openai", None)
    assert refills_continuously("anthropic", None)
    assert refills_continuously("groq", None)
    assert not refills_continuously("github", None)
    assert refills_continuously("something-unknown", None)
    assert refills_continuously("custom", {"remaining_requests": "x"})
    assert not refills_continuously("custom", {"remaining_requests": "x", "refill": "window"})


async def test_exhausted_continuous_provider_keeps_trickling_calls(
    client, admin, service_key, make_policy, worker: Worker
):
    provider = "trickle-test"
    header_map = {
        "remaining_requests": "x-left",
        "limit_requests": "x-limit",
        "reset_requests": "x-reset",
        "refill": "continuous",
    }
    await client.put(f"{API}/providers/{provider}", headers=admin, json=header_map)
    policy = await make_policy(limit=600, window_seconds=60, adaptive=False, provider=provider)
    headers = {"x-left": "0", "x-limit": "60", "x-reset": "60"}
    await client.post(
        f"{API}/providers/report",
        headers=service_key,
        json={"provider": provider, "status": 200, "headers": headers},
    )
    await worker.cache.load()
    await worker.adaptive_tick()
    limit = (await client.get(f"{API}/policies/{policy['id']}", headers=admin)).json()["effective_limit"]
    assert limit == 54, "60 per minute less the 10% safety margin, not zero"
    await client.delete(f"{API}/providers/{provider}", headers=admin)


async def test_deleting_provider_state_lifts_the_quota_cap_at_once(
    client, admin, service_key, make_policy, worker: Worker
):
    provider = "forget-test"
    await client.put(
        f"{API}/providers/{provider}",
        headers=admin,
        json={"remaining_requests": "x-left", "reset_requests": "x-reset", "refill": "window"},
    )
    policy = await make_policy(limit=100, window_seconds=60, adaptive=False, provider=provider)
    await client.post(
        f"{API}/providers/report",
        headers=service_key,
        json={"provider": provider, "status": 200, "headers": {"x-left": "0", "x-reset": "600"}},
    )
    await worker.cache.load()
    await worker.adaptive_tick()

    async def effective_limit() -> int:
        return (await client.get(f"{API}/policies/{policy['id']}", headers=admin)).json()["effective_limit"]

    assert await effective_limit() == 1
    assert (await client.delete(f"{API}/providers/{provider}", headers=admin)).status_code == 204
    # No worker tick in between: the cap must already be gone.
    assert await effective_limit() == 100


# ---- refusals without Retry-After ------------------------------------------


def test_repeated_blind_429s_back_off_to_an_hour_not_a_minute():
    """Anthropic's monthly spend cap answers 429 with no Retry-After until the 1st.
    Backing off only to 60 s would probe it every minute for weeks."""
    blind = Observation(throttled=True)
    pauses = [pause_seconds(blind, consecutive) for consecutive in range(1, 15)]
    assert pauses[0] == 2, "a single 429 still costs only a short pause"
    assert pauses == sorted(pauses)
    assert pauses[11] == MAX_PAUSE_SECONDS, "about a dozen in a row reach the hour"
    assert max(pauses) == MAX_PAUSE_SECONDS


# ---- Groq: daily request quota, per-minute token quota ---------------------


def test_groq_daily_request_quota_is_paced_at_its_refill_rate():
    """Groq's x-ratelimit-*-requests headers describe requests per day. An empty daily
    bucket that is full again in 24 h refills at 14,400 / 86,400 per second, i.e. 10 per
    minute; less the 10% margin, 9 per minute against a 600-per-minute policy."""
    observation = parse(
        "groq",
        200,
        {
            "x-ratelimit-limit-requests": "14400",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "24h0m0s",
        },
        now=0,
    )
    assert observation.reset_requests_in == 86_400
    cap = provider_cap(
        remaining=observation.remaining_requests,
        reset_in_seconds=observation.reset_requests_in,
        quota_limit=observation.limit_requests,
        continuous=refills_continuously("groq", None),
        **COMMON,
    )
    assert cap is not None
    assert cap * COMMON["base_limit"] == pytest.approx(9)


def test_groq_partly_used_daily_quota_never_paces_above_what_is_available():
    """Over the time to full, a continuous bucket yields what remains plus what refills,
    which is the whole limit. Pacing at limit / reset therefore spends exactly that."""
    remaining, limit, reset_in = 14_370.0, 14_400.0, 179.56
    cap = provider_cap(
        remaining=remaining, reset_in_seconds=reset_in, quota_limit=limit, continuous=True, **COMMON
    )
    assert cap is not None
    allowed_until_full = cap * COMMON["base_limit"] / COMMON["window_seconds"] * reset_in
    assert allowed_until_full <= limit

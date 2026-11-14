"""How a provider's quota headers turn into a limit, for both kinds of quota."""

from __future__ import annotations

import pytest

from app.adaptive.controller import provider_cap
from app.providers.parsers import refills_continuously
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

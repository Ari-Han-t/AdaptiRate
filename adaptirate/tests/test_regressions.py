"""Regression tests for defects found in review. Each test fails on the code as it was."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from redis.asyncio import Redis

from app.adaptive.controller import provider_cap
from app.api.observe import stream_frames
from app.limiter import keys
from app.limiter.engine import Algorithm, Check, LimiterEngine
from app.limiter.policy import RuleAction, canonical_ip, compile_policy, resolve
from app.providers.service import ProviderState
from app.worker import Worker

API = "/api/v1"
NUL = chr(0)


def check(unique: str, algorithm: Algorithm, **overrides) -> Check:
    values = {
        "policy_id": unique,
        "policy_name": f"policy-{unique}",
        "algorithm": algorithm,
        "limit": 10,
        "window_ms": 60_000,
        "burst": 0,
        "subject": "client",
    }
    values.update(overrides)
    return Check(**values)


async def counters(redis: Redis, policy_id: str) -> tuple[int, int]:
    allowed = blocked = 0
    async for key in redis.scan_iter(match=keys.counter_prefix(policy_id) + "*"):
        values = await redis.hgetall(key)
        allowed += int(values.get("a", 0))
        blocked += int(values.get("b", 0))
    return allowed, blocked


# ---- limiter ---------------------------------------------------------------


async def test_fixed_window_subject_cannot_write_into_another_subjects_window(
    engine: LimiterEngine, unique: str, redis: Redis
):
    """The window index used to be appended to the key, so the subject "victim:<index>"
    addressed the victim's counter."""
    window_ms = 60_000
    seconds, micros = await redis.time()
    index = (int(seconds) * 1000 + int(micros) // 1000) // window_ms
    for offset in (0, 1):  # cover a window boundary falling between the two calls
        attacker = check(unique, Algorithm.FIXED_WINDOW, subject=f"victim:{index + offset}", cost=10)
        await engine.check(attacker)
    victim = await engine.check(check(unique, Algorithm.FIXED_WINDOW, subject="victim"))
    assert victim.allowed
    assert victim.remaining == 9


async def test_leaky_bucket_enforces_rates_above_ten_thousand_per_second(
    engine: LimiterEngine, unique: str, redis: Redis
):
    """At 20,000/s the spacing is 0.05 ms. Stored with 0.1 ms precision it rounded away
    and every request was admitted."""
    probe = check(unique, Algorithm.LEAKY_BUCKET, limit=20_000, window_ms=1000)
    before = time.perf_counter()
    decisions = await engine.check_many([probe] * 3000)
    elapsed_ms = (time.perf_counter() - before) * 1000
    allowed = sum(d.allowed for d in decisions)
    assert allowed < 3000, "the limiter admitted everything"
    assert allowed <= 20 * elapsed_ms + 5, f"{allowed} admitted in {elapsed_ms:.0f} ms at 20 per ms"


async def test_lease_probes_are_not_counted_as_blocked_traffic(
    engine: LimiterEngine, unique: str, redis: Redis
):
    granted, _ = await engine.lease(check(unique, Algorithm.TOKEN_BUCKET, limit=20, cost=50))
    assert granted == 20
    assert await counters(redis, unique) == (20, 0), "the refused 50-token probe is not refused traffic"
    # A plain over-limit check is still counted.
    await engine.check(check(unique, Algorithm.TOKEN_BUCKET, limit=20, cost=5))
    assert await counters(redis, unique) == (20, 5)


# ---- rules -----------------------------------------------------------------


def _policy(**overrides):
    row = {
        "id": "p1", "name": "api", "enabled": True, "algorithm": "token_bucket", "limit": 100,
        "window_seconds": 60, "burst": 0, "priority": 50, "scope": "ip", "rules": [], "adaptive": True,
        "min_limit": None, "max_limit": None, "target_latency_ms": None, "provider": None,
        "provider_dimension": "requests",
    }  # fmt: skip
    row.update(overrides)
    return compile_policy(SimpleNamespace(**row))


def test_ip_rules_cannot_be_dodged_by_respelling_the_address():
    policy = _policy(
        rules=[
            {"attribute": "ip", "values": ["10.0.0.0/8"], "action": "deny", "priority": 2},
            {"attribute": "ip", "values": ["2001:db8::1"], "action": "deny", "priority": 1},
        ]
    )
    for spelling in ("10.1.2.3", "::ffff:10.1.2.3", "::FFFF:10.1.2.3", "2001:0DB8:0:0:0:0:0:1"):
        assert resolve(policy, key=None, attrs={"ip": spelling}).verdict is RuleAction.DENY, spelling
    assert resolve(policy, key=None, attrs={"ip": "192.168.1.1"}).verdict is RuleAction.LIMIT


def test_one_address_has_one_bucket_however_it_is_written():
    policy = _policy()
    subjects = {
        resolve(policy, key=None, attrs={"ip": spelling}).subject
        for spelling in ("2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001", "2001:DB8::1")
    }
    assert subjects == {"2001:db8::1"}
    assert canonical_ip("::ffff:192.0.2.7") == "192.0.2.7"
    assert canonical_ip("not-an-ip") == "not-an-ip"


# ---- providers -------------------------------------------------------------


def test_a_quota_whose_reset_time_has_passed_no_longer_caps_the_limit():
    """remaining=0 with a reset one second ago used to yield a cap of 0 for up to a day."""
    now = 1000.0
    stale = ProviderState(name="p", remaining_requests=0, reset_requests_at=now - 1)
    assert stale.reset_in("requests", now) is None
    cap = provider_cap(
        remaining=stale.remaining("requests"),
        reset_in_seconds=stale.reset_in("requests", now),
        window_seconds=60,
        base_limit=100,
        safety_margin=0.1,
    )
    assert cap is None
    live = ProviderState(name="p", remaining_requests=0, reset_requests_at=now + 30)
    assert live.reset_in("requests", now) == 30


async def test_limit_recovers_once_an_exhausted_provider_quota_resets(
    client, admin, service_key, make_policy, worker: Worker
):
    provider = "reset-test"
    await client.put(
        f"{API}/providers/{provider}",
        headers=admin,
        json={"remaining_requests": "x-left", "reset_requests": "x-reset", "reset_format": "seconds"},
    )
    policy = await make_policy(limit=100, window_seconds=60, adaptive=False, provider=provider)
    await client.post(
        f"{API}/providers/report",
        headers=service_key,
        json={"provider": provider, "status": 200, "headers": {"x-left": "0", "x-reset": "1"}},
    )
    await worker.cache.load()
    await worker.adaptive_tick()
    capped = (await client.get(f"{API}/policies/{policy['id']}", headers=admin)).json()["effective_limit"]
    assert capped == 1
    await asyncio.sleep(1.2)
    await worker.adaptive_tick()
    recovered = (await client.get(f"{API}/policies/{policy['id']}", headers=admin)).json()["effective_limit"]
    assert recovered == 100


# ---- sessions --------------------------------------------------------------


async def test_two_tabs_refreshing_at_once_both_stay_signed_in(client: httpx.AsyncClient, admin):
    body = {"email": "tabs@test.local", "password": "tabs-password-123", "role": "viewer"}
    assert (await client.post(f"{API}/users", headers=admin, json=body)).status_code == 201
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as browser:
        await browser.post(f"{API}/auth/login", json={"email": body["email"], "password": body["password"]})
        cookie = browser.cookies.get("ar_refresh")
    tabs = []
    for _ in range(2):
        tab = httpx.AsyncClient(
            transport=client._transport, base_url="http://test", cookies={"ar_refresh": cookie}
        )
        tabs.append(tab)
    try:
        first, second = [await tab.post(f"{API}/auth/refresh") for tab in tabs]
        assert (first.status_code, second.status_code) == (200, 200)
        # Each tab's new session keeps working.
        for tab in tabs:
            assert (await tab.post(f"{API}/auth/refresh")).status_code == 200
    finally:
        for tab in tabs:
            await tab.aclose()


# ---- API keys --------------------------------------------------------------


async def test_policy_restricted_key_cannot_widen_its_own_reach(client, admin, make_policy):
    mine, other = await make_policy(), await make_policy()
    created = await client.post(
        f"{API}/keys",
        headers=admin,
        json={
            "name": "tenant-a",
            "scopes": ["evaluate", "report", "manage", "read"],
            "policy_ids": [mine["id"]],
        },
    )
    key = {"X-API-Key": created.json()["secret"]}

    unrestricted = await client.post(f"{API}/keys", headers=key, json={"name": "x", "scopes": ["evaluate"]})
    assert unrestricted.status_code == 403
    widened = await client.post(
        f"{API}/keys",
        headers=key,
        json={"name": "x", "scopes": ["evaluate"], "policy_ids": [mine["id"], other["id"]]},
    )
    assert widened.status_code == 403
    same = await client.post(
        f"{API}/keys", headers=key, json={"name": "x", "scopes": ["evaluate"], "policy_ids": [mine["id"]]}
    )
    assert same.status_code == 201

    assert (
        await client.patch(f"{API}/policies/{mine['id']}", headers=key, json={"limit": 9})
    ).status_code == 200
    assert (
        await client.patch(f"{API}/policies/{other['id']}", headers=key, json={"limit": 9})
    ).status_code == 403
    assert (await client.post(f"{API}/policies/{other['id']}/reset", headers=key)).status_code == 403
    assert (await client.delete(f"{API}/policies/{other['id']}", headers=key)).status_code == 403
    assert (
        await client.post(f"{API}/policies", headers=key, json={"name": "new-one", "limit": 1})
    ).status_code == 403


async def test_provider_report_only_pauses_policies_the_key_may_use(client, admin, service_key, make_policy):
    provider = "shared-provider"
    mine = await make_policy(limit=100, provider=provider)
    theirs = await make_policy(limit=100, provider=provider)
    created = await client.post(
        f"{API}/keys",
        headers=admin,
        json={"name": "tenant-b", "scopes": ["evaluate", "report"], "policy_ids": [mine["id"]]},
    )
    key = {"X-API-Key": created.json()["secret"]}
    report = {"provider": provider, "status": 429, "headers": {"retry-after": "3600"}}
    assert (await client.post(f"{API}/providers/report", headers=key, json=report)).status_code == 200

    own = await client.post(f"{API}/evaluate", headers=key, json={"policy": mine["name"], "user": "a"})
    assert own.json()["reason"] == "paused"
    others = await client.post(
        f"{API}/evaluate", headers=service_key, json={"policy": theirs["name"], "user": "a"}
    )
    assert others.json()["allowed"], "another tenant's policy must not be paused by this key"
    await client.delete(f"{API}/providers/{provider}", headers=admin)


# ---- decision log ----------------------------------------------------------


async def test_a_subject_the_database_cannot_store_does_not_stall_the_log(
    client, admin, service_key, make_policy, worker: Worker
):
    policy = await make_policy(limit=5)
    name = policy["name"]
    for user in (f"bad{NUL}subject", "good-subject"):
        response = await client.post(
            f"{API}/evaluate", headers=service_key, json={"policy": name, "user": user}
        )
        assert response.status_code == 200
    await client._transport.app.state.events.flush()  # type: ignore[attr-defined]
    while await worker.sink_once(block_ms=50):
        pass
    subjects = {
        entry["subject"]
        for entry in (await client.get(f"{API}/logs", headers=admin, params={"policy": name})).json()["items"]
    }
    assert subjects == {"badsubject", "good-subject"}


async def test_one_rejected_row_does_not_lose_the_rest_of_the_batch(worker: Worker, database):
    def row(subject: str) -> dict:
        return {
            "ts": datetime.now(UTC), "policy_id": "p", "policy_name": "batch-fallback", "subject": subject,
            "allowed": True, "reason": "ok", "limit": 1, "remaining": 0, "cost": 1, "api_key_id": None,
        }  # fmt: skip

    written = await worker._insert_logs([row("first"), row(f"un{NUL}storable"), row("third")])
    assert written == 2


# ---- API surface -----------------------------------------------------------


async def test_responses_carry_the_window_so_sdks_need_not_guess_it(client, service_key, make_policy):
    policy = await make_policy(
        limit=10,
        window_seconds=30,
        rules=[{"attribute": "role", "values": ["batch"], "limit": 2, "window_seconds": 5}],
    )
    name = policy["name"]
    plain = (
        await client.post(f"{API}/evaluate", headers=service_key, json={"policy": name, "user": "a"})
    ).json()
    assert plain["window_ms"] == 30_000
    ruled = await client.post(
        f"{API}/evaluate", headers=service_key, json={"policy": name, "user": "b", "role": "batch"}
    )
    assert ruled.json()["window_ms"] == 5_000
    lease = await client.post(
        f"{API}/lease", headers=service_key, json={"policy": name, "user": "c", "tokens": 3}
    )
    assert lease.json()["window_ms"] == 30_000
    batch = await client.post(
        f"{API}/evaluate/batch", headers=service_key, json={"checks": [{"policy": name, "user": "d"}]}
    )
    assert batch.json()["results"][0]["window_ms"] == 30_000


async def test_oversized_adaptive_bounds_are_rejected_not_a_server_error(client, admin):
    response = await client.post(
        f"{API}/policies", headers=admin, json={"name": "huge-bounds", "limit": 10, "max_limit": 10**12}
    )
    assert response.status_code == 422


async def test_stream_ends_when_the_session_that_opened_it_expires(client, redis: Redis):
    app = client._transport.app  # type: ignore[attr-defined]

    async def connected() -> bool:
        return False

    expired = [frame async for frame in stream_frames(redis, app.state.cache, connected, time.time() - 1)]
    assert expired == [b"retry: 2000\n\n"], "no data after the token's expiry"

    frames = stream_frames(redis, app.state.cache, connected, time.time() + 1.5)
    received = [frame async for frame in frames]
    assert 2 <= len(received) <= 4, "runs until the deadline, then stops on its own"


async def test_refresh_reuse_after_the_grace_window_still_revokes(client, admin, monkeypatch):
    monkeypatch.setattr("app.api.auth.REFRESH_REUSE_GRACE", timedelta(0))
    body = {"email": "late-reuse@test.local", "password": "late-reuse-password", "role": "viewer"}
    await client.post(f"{API}/users", headers=admin, json=body)
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as browser:
        await browser.post(f"{API}/auth/login", json={"email": body["email"], "password": body["password"]})
        original = browser.cookies.get("ar_refresh")
        assert (await browser.post(f"{API}/auth/refresh")).status_code == 200
        async with httpx.AsyncClient(
            transport=client._transport, base_url="http://test", cookies={"ar_refresh": original}
        ) as thief:
            assert (await thief.post(f"{API}/auth/refresh")).status_code == 401
        assert (await browser.post(f"{API}/auth/refresh")).status_code == 401, "every session is ended"

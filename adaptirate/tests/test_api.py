"""End-to-end API behaviour against real Postgres and Redis."""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
import pytest

from app.config import get_settings
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

API = "/api/v1"


async def evaluate(client: httpx.AsyncClient, headers: dict[str, str], **body) -> httpx.Response:
    return await client.post(f"{API}/evaluate", headers=headers, json=body)


# ---- health and authentication --------------------------------------------


async def test_health_and_readiness(client: httpx.AsyncClient):
    assert (await client.get("/health")).json() == {"status": "ok"}
    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "redis": "ok", "postgres": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/policies"),
        ("POST", "/evaluate"),
        ("GET", "/keys"),
        ("GET", "/metrics/live"),
        ("GET", "/logs"),
        ("GET", "/users"),
        ("GET", "/config"),
        ("GET", "/stream"),
        ("POST", "/report"),
    ],
)
async def test_every_endpoint_requires_credentials(client: httpx.AsyncClient, method: str, path: str):
    response = await client.request(method, API + path)
    assert response.status_code == 401


async def test_forged_and_malformed_tokens_are_rejected(client: httpx.AsyncClient):
    payload = {
        "sub": "x",
        "role": "admin",
        "iss": "adaptirate",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "typ": "access",
    }
    wrong_secret = jwt.encode(payload, "another-secret-another-secret-another", algorithm="HS256")
    unsigned = jwt.encode(payload, "", algorithm="none")
    secret = get_settings().jwt_secret.get_secret_value()
    expired = jwt.encode({**payload, "exp": int(time.time()) - 5}, secret, algorithm="HS256")
    refresh_typed = jwt.encode({**payload, "typ": "refresh"}, secret, algorithm="HS256")
    for token in (wrong_secret, unsigned, expired, refresh_typed, "garbage", "ar_not-a-real-key"):
        response = await client.get(f"{API}/policies", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, token


async def test_login_rejects_bad_credentials_without_revealing_which_part_was_wrong(
    client: httpx.AsyncClient,
):
    wrong_password = await client.post(
        f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "nope-nope-nope"}
    )
    unknown_user = await client.post(
        f"{API}/auth/login", json={"email": "ghost@test.local", "password": "x" * 12}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


async def test_login_is_rate_limited_per_account(client: httpx.AsyncClient):
    statuses = []
    for _ in range(10):
        response = await client.post(
            f"{API}/auth/login", json={"email": "victim@test.local", "password": "guess-guess-1"}
        )
        statuses.append(response.status_code)
    assert statuses[:8] == [401] * 8
    assert statuses[8:] == [429, 429]


async def test_refresh_rotates_and_reuse_revokes_the_session(
    client: httpx.AsyncClient, admin: dict[str, str]
):
    created = await client.post(
        f"{API}/users",
        headers=admin,
        json={"email": "rotate@test.local", "password": "rotate-password-1", "role": "viewer"},
    )
    assert created.status_code == 201
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as browser:
        login = await browser.post(
            f"{API}/auth/login", json={"email": "rotate@test.local", "password": "rotate-password-1"}
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
        original = browser.cookies.get("ar_refresh")

        refreshed = await browser.post(f"{API}/auth/refresh")
        assert refreshed.status_code == 200
        assert browser.cookies.get("ar_refresh") != original
        me = await browser.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}
        )
        assert me.json()["role"] == "viewer"

        # Replaying the rotated-out token is treated as theft: every session ends.
        replay = await client.post(f"{API}/auth/refresh", cookies={"ar_refresh": original})
        assert replay.status_code == 401
        client.cookies.clear()
        assert (await browser.post(f"{API}/auth/refresh")).status_code == 401


async def test_logout_ends_the_session(client: httpx.AsyncClient, admin: dict[str, str]):
    await client.post(
        f"{API}/users",
        headers=admin,
        json={"email": "leaver@test.local", "password": "leaver-password-1", "role": "viewer"},
    )
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as browser:
        await browser.post(
            f"{API}/auth/login", json={"email": "leaver@test.local", "password": "leaver-password-1"}
        )
        token = browser.cookies.get("ar_refresh")
        assert (await browser.post(f"{API}/auth/logout")).status_code == 204
        assert (await client.post(f"{API}/auth/refresh", cookies={"ar_refresh": token})).status_code == 401
        client.cookies.clear()


# ---- roles ----------------------------------------------------------------


async def _login_as(client: httpx.AsyncClient, admin: dict[str, str], role: str) -> dict[str, str]:
    email, password = f"{role}@test.local", f"{role}-password-123"
    await client.post(
        f"{API}/users", headers=admin, json={"email": email, "password": password, "role": role}
    )
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_role_permissions(client: httpx.AsyncClient, admin: dict[str, str]):
    viewer = await _login_as(client, admin, "viewer")
    developer = await _login_as(client, admin, "developer")
    policy = {"name": "rbac-policy", "limit": 10}

    assert (await client.get(f"{API}/policies", headers=viewer)).status_code == 200
    assert (await client.post(f"{API}/policies", headers=viewer, json=policy)).status_code == 403
    assert (await client.post(f"{API}/keys", headers=viewer, json={"name": "k"})).status_code == 403
    assert (await evaluate(client, viewer, policy="rbac-policy", key="x")).status_code == 403

    assert (await client.post(f"{API}/policies", headers=developer, json=policy)).status_code == 201
    assert (await evaluate(client, developer, policy="rbac-policy", key="x")).status_code == 200
    for response in (
        await client.get(f"{API}/users", headers=developer),
        await client.get(f"{API}/audit", headers=developer),
        await client.put(f"{API}/config", headers=developer, json={}),
    ):
        assert response.status_code == 403

    assert (await client.get(f"{API}/users", headers=admin)).status_code == 200


async def test_the_last_admin_cannot_be_removed(client: httpx.AsyncClient, admin: dict[str, str]):
    users = (await client.get(f"{API}/users", headers=admin)).json()
    me = next(user for user in users if user["email"] == ADMIN_EMAIL)
    assert (
        await client.patch(f"{API}/users/{me['id']}", headers=admin, json={"role": "viewer"})
    ).status_code == 409
    assert (
        await client.patch(f"{API}/users/{me['id']}", headers=admin, json={"is_active": False})
    ).status_code == 409
    assert (await client.delete(f"{API}/users/{me['id']}", headers=admin)).status_code == 409


async def test_user_validation(client: httpx.AsyncClient, admin: dict[str, str]):
    short = await client.post(
        f"{API}/users", headers=admin, json={"email": "a@test.local", "password": "short"}
    )
    assert short.status_code == 422
    body = {"email": "dup@test.local", "password": "long-enough-password"}
    assert (await client.post(f"{API}/users", headers=admin, json=body)).status_code == 201
    assert (await client.post(f"{API}/users", headers=admin, json=body)).status_code == 409


# ---- policies -------------------------------------------------------------


async def test_policy_lifecycle(client: httpx.AsyncClient, admin: dict[str, str], make_policy):
    policy = await make_policy(limit=100, description="first")
    assert policy["effective_limit"] == 100 and policy["multiplier"] == 1.0

    duplicate = await client.post(f"{API}/policies", headers=admin, json={"name": policy["name"], "limit": 1})
    assert duplicate.status_code == 409

    updated = await client.patch(
        f"{API}/policies/{policy['id']}", headers=admin, json={"limit": 250, "burst": 10}
    )
    assert updated.status_code == 200
    assert (updated.json()["limit"], updated.json()["burst"], updated.json()["description"]) == (
        250,
        10,
        "first",
    )

    listed = (await client.get(f"{API}/policies", headers=admin)).json()
    assert any(item["id"] == policy["id"] for item in listed)

    assert (await client.delete(f"{API}/policies/{policy['id']}", headers=admin)).status_code == 204
    assert (await client.get(f"{API}/policies/{policy['id']}", headers=admin)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad name with spaces", "limit": 1},
        {"name": "ok", "limit": 0},
        {"name": "ok", "limit": 10, "min_limit": 20},
        {"name": "ok", "limit": 10, "max_limit": 5},
        {"name": "ok", "limit": 10, "algorithm": "roulette"},
        {"name": "ok", "limit": 10, "window_seconds": 0},
        {"name": "ok", "limit": 10, "unknown_field": True},
        {"name": "ok", "limit": 10, "rules": [{"attribute": "ip", "values": ["not-an-ip"], "limit": 5}]},
        {"name": "ok", "limit": 10, "rules": [{"attribute": "role", "values": ["x"]}]},
    ],
)
async def test_policy_validation(client: httpx.AsyncClient, admin: dict[str, str], body: dict):
    assert (await client.post(f"{API}/policies", headers=admin, json=body)).status_code == 422


async def test_update_cannot_break_adaptive_bounds(
    client: httpx.AsyncClient, admin: dict[str, str], make_policy
):
    policy = await make_policy(limit=100, min_limit=50)
    response = await client.patch(f"{API}/policies/{policy['id']}", headers=admin, json={"limit": 20})
    assert response.status_code == 422


# ---- evaluate -------------------------------------------------------------


async def test_evaluate_enforces_the_limit_per_subject(client, service_key, make_policy):
    policy = await make_policy(limit=3)
    results = [
        (await evaluate(client, service_key, policy=policy["name"], user="alice")).json() for _ in range(5)
    ]
    assert [r["allowed"] for r in results] == [True, True, True, False, False]
    assert results[0]["remaining"] == 2 and results[0]["limit"] == 3
    assert results[3]["reason"] == "limit" and results[3]["retry_after_ms"] > 0
    assert (await evaluate(client, service_key, policy=policy["name"], user="bob")).json()["allowed"]


async def test_evaluate_rejects_requests_it_cannot_attribute(client, service_key, make_policy):
    policy = await make_policy(scope="user")
    missing = await evaluate(client, service_key, policy=policy["name"], ip="10.0.0.1")
    assert missing.status_code == 422
    assert "scoped by 'user'" in missing.json()["detail"]
    assert (await evaluate(client, service_key, policy="does-not-exist", user="a")).status_code == 404
    assert (await evaluate(client, service_key, policy=policy["name"], user="a", cost=0)).status_code == 422


async def test_concurrent_evaluations_do_not_over_admit(client, service_key, make_policy):
    policy = await make_policy(limit=25, algorithm="token_bucket")
    responses = await asyncio.gather(
        *(evaluate(client, service_key, policy=policy["name"], user="burst") for _ in range(120))
    )
    assert sum(r.json()["allowed"] for r in responses) == 25


async def test_rules_allow_deny_and_override(client, service_key, make_policy):
    policy = await make_policy(
        limit=1,
        rules=[
            {"attribute": "user", "values": ["banned"], "action": "deny", "priority": 100},
            {"attribute": "ip", "values": ["10.0.0.0/8"], "action": "allow", "priority": 50},
            {"attribute": "role", "values": ["premium"], "limit": 4},
        ],
    )
    name = policy["name"]
    denied = (await evaluate(client, service_key, policy=name, user="banned", ip="10.1.1.1")).json()
    assert (denied["allowed"], denied["reason"]) == (False, "rule_deny")
    for _ in range(5):
        allowed = (await evaluate(client, service_key, policy=name, user="internal", ip="10.9.9.9")).json()
        assert (allowed["allowed"], allowed["reason"]) == (True, "rule_allow")
    premium = [
        (await evaluate(client, service_key, policy=name, user="vip", role="premium")).json()
        for _ in range(5)
    ]
    assert sum(r["allowed"] for r in premium) == 4
    basic = [
        (await evaluate(client, service_key, policy=name, user="free", role="free")).json() for _ in range(3)
    ]
    assert sum(r["allowed"] for r in basic) == 1


async def test_disabled_policy_lets_everything_through(client, admin, service_key, make_policy):
    policy = await make_policy(limit=1)
    await client.patch(f"{API}/policies/{policy['id']}", headers=admin, json={"enabled": False})
    results = [
        (await evaluate(client, service_key, policy=policy["name"], user="a")).json() for _ in range(3)
    ]
    assert all(r["allowed"] and r["reason"] == "disabled" for r in results)


async def test_algorithm_can_be_switched_live(client, admin, service_key, make_policy):
    policy = await make_policy(limit=2, algorithm="fixed_window")
    name = policy["name"]
    before = [(await evaluate(client, service_key, policy=name, user="a")).json() for _ in range(4)]
    assert sum(r["allowed"] for r in before) == 2
    switched = await client.patch(
        f"{API}/policies/{policy['id']}", headers=admin, json={"algorithm": "leaky_bucket"}
    )
    assert switched.json()["algorithm"] == "leaky_bucket"
    # Takes effect on the very next request, with fresh state for the new algorithm.
    after = [(await evaluate(client, service_key, policy=name, user="a")).json() for _ in range(3)]
    assert [r["allowed"] for r in after] == [True, False, False]
    catalogue = (await client.get(f"{API}/algorithms", headers=admin)).json()
    assert {item["id"] for item in catalogue} == {
        "token_bucket",
        "sliding_window",
        "fixed_window",
        "leaky_bucket",
        "adaptive_hybrid",
    }
    assert name in next(item for item in catalogue if item["id"] == "leaky_bucket")["policy_names"]


async def test_reset_clears_limiter_state(client, admin, service_key, make_policy):
    policy = await make_policy(limit=1)
    await evaluate(client, service_key, policy=policy["name"], user="a")
    assert not (await evaluate(client, service_key, policy=policy["name"], user="a")).json()["allowed"]
    reset = await client.post(f"{API}/policies/{policy['id']}/reset", headers=admin)
    assert reset.json()["keys_removed"] >= 1
    assert (await evaluate(client, service_key, policy=policy["name"], user="a")).json()["allowed"]


async def test_batch_evaluate(client, service_key, make_policy):
    first = await make_policy(limit=2)
    second = await make_policy(limit=1, rules=[{"attribute": "user", "values": ["x"], "action": "deny"}])
    checks = [
        {"policy": first["name"], "user": "a"},
        {"policy": first["name"], "user": "a"},
        {"policy": first["name"], "user": "a"},
        {"policy": second["name"], "user": "x"},
        {"policy": second["name"], "user": "b"},
    ]
    response = await client.post(f"{API}/evaluate/batch", headers=service_key, json={"checks": checks})
    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["allowed"] for r in results] == [True, True, False, False, True]
    assert [r["policy"] for r in results] == [c["policy"] for c in checks]
    too_many = {"checks": [{"policy": first["name"], "user": "a"}] * 501}
    assert (await client.post(f"{API}/evaluate/batch", headers=service_key, json=too_many)).status_code == 422


async def test_lease_grants_what_is_available(client, service_key, make_policy):
    policy = await make_policy(limit=10, algorithm="token_bucket")
    body = {"policy": policy["name"], "user": "worker", "tokens": 7}
    first = (await client.post(f"{API}/lease", headers=service_key, json=body)).json()
    second = (await client.post(f"{API}/lease", headers=service_key, json=body)).json()
    third = (await client.post(f"{API}/lease", headers=service_key, json=body)).json()
    assert (first["granted"], second["granted"], third["granted"]) == (7, 3, 0)
    assert 100 <= first["ttl_ms"] <= 2000
    assert third["retry_after_ms"] > 0


# ---- API keys -------------------------------------------------------------


async def test_api_key_lifecycle(client, admin, make_policy):
    policy = await make_policy(limit=100)
    created = await client.post(f"{API}/keys", headers=admin, json={"name": "svc", "scopes": ["evaluate"]})
    assert created.status_code == 201
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("ar_") and body["prefix"] == secret[:11]
    key = {"X-API-Key": secret}

    listed = (await client.get(f"{API}/keys", headers=admin)).json()
    assert all("secret" not in item and "key_hash" not in item for item in listed)

    assert (await evaluate(client, key, policy=policy["name"], user="a")).status_code == 200
    bearer = {"Authorization": f"Bearer {secret}"}
    assert (await evaluate(client, bearer, policy=policy["name"], user="a")).status_code == 200
    # Scopes are enforced: this key may evaluate, nothing else.
    assert (await client.get(f"{API}/policies", headers=key)).status_code == 403
    assert (
        await client.post(f"{API}/policies", headers=key, json={"name": "x", "limit": 1})
    ).status_code == 403
    report = {"policy": policy["name"], "instance_id": "i", "requests": 1}
    assert (await client.post(f"{API}/report", headers=key, json=report)).status_code == 403

    revoked = await client.post(f"{API}/keys/{body['id']}/revoke", headers=admin)
    assert revoked.json()["revoked_at"] is not None
    assert (await evaluate(client, key, policy=policy["name"], user="a")).status_code == 401


async def test_api_key_can_be_restricted_to_policies(client, admin, make_policy):
    allowed, other = await make_policy(), await make_policy()
    created = await client.post(
        f"{API}/keys",
        headers=admin,
        json={"name": "scoped", "scopes": ["evaluate"], "policy_ids": [allowed["id"]]},
    )
    key = {"X-API-Key": created.json()["secret"]}
    assert (await evaluate(client, key, policy=allowed["name"], user="a")).status_code == 200
    assert (await evaluate(client, key, policy=other["name"], user="a")).status_code == 403
    unknown = await client.post(f"{API}/keys", headers=admin, json={"name": "x", "policy_ids": ["nope"]})
    assert unknown.status_code == 422


async def test_api_key_expiry_and_scope_escalation(client, admin):
    past = await client.post(
        f"{API}/keys", headers=admin, json={"name": "old", "expires_at": "2020-01-01T00:00:00Z"}
    )
    assert past.status_code == 422
    naive = await client.post(
        f"{API}/keys", headers=admin, json={"name": "old", "expires_at": "2099-01-01T00:00:00"}
    )
    assert naive.status_code == 422

    manager = await client.post(
        f"{API}/keys", headers=admin, json={"name": "mgr", "scopes": ["manage", "read"]}
    )
    manager_key = {"X-API-Key": manager.json()["secret"]}
    # A key holding only manage+read cannot mint a key with powers it lacks.
    escalated = await client.post(
        f"{API}/keys", headers=manager_key, json={"name": "x", "scopes": ["evaluate"]}
    )
    assert escalated.status_code == 403
    same = await client.post(f"{API}/keys", headers=manager_key, json={"name": "x", "scopes": ["read"]})
    assert same.status_code == 201


async def test_api_key_usage_is_counted(client, admin, redis, make_policy):
    policy = await make_policy(limit=100)
    created = (
        await client.post(f"{API}/keys", headers=admin, json={"name": "usage", "scopes": ["evaluate"]})
    ).json()
    key = {"X-API-Key": created["secret"]}
    for _ in range(4):
        await evaluate(client, key, policy=policy["name"], user="a")
    await client._transport.app.state.events.flush()  # type: ignore[attr-defined]
    usage = (await client.get(f"{API}/keys/{created['id']}/usage", headers=admin)).json()
    assert usage["total"] == 4 and usage["daily"][-1]["requests"] == 4
    listed = next(
        k for k in (await client.get(f"{API}/keys", headers=admin)).json() if k["id"] == created["id"]
    )
    assert listed["requests_today"] == 4 and listed["last_used_at"] is not None


# ---- providers ------------------------------------------------------------


async def test_provider_throttle_pauses_bound_policies(client, admin, service_key, make_policy):
    bound = await make_policy(limit=100, provider="openai")
    unbound = await make_policy(limit=100)
    assert (await evaluate(client, service_key, policy=bound["name"], user="a")).json()["allowed"]

    report = {"provider": "openai", "status": 429, "headers": {"Retry-After": "20"}}
    response = await client.post(f"{API}/providers/report", headers=service_key, json=report)
    assert response.json() == {"provider": "openai", "throttled": True, "paused_ms": 20000}

    paused = (await evaluate(client, service_key, policy=bound["name"], user="a")).json()
    assert (paused["allowed"], paused["reason"]) == (False, "paused")
    assert 15000 < paused["retry_after_ms"] <= 20000
    assert (await evaluate(client, service_key, policy=unbound["name"], user="a")).json()["allowed"]

    providers = {p["name"]: p for p in (await client.get(f"{API}/providers", headers=admin)).json()}
    assert providers["openai"]["throttled"] >= 1 and providers["openai"]["paused_ms"] > 0
    assert bound["name"] in providers["openai"]["policies"]
    assert {"openai", "anthropic", "groq", "github", "gemini"} <= set(providers)

    # Deleting the provider's state lifts the pause.
    assert (await client.delete(f"{API}/providers/openai", headers=admin)).status_code == 204
    assert (await evaluate(client, service_key, policy=bound["name"], user="a")).json()["allowed"]


async def test_custom_provider_header_map(client, admin, service_key):
    mapping = {
        "remaining_requests": "X-Quota-Left",
        "reset_requests": "X-Quota-Reset",
        "reset_format": "seconds",
    }
    assert (await client.put(f"{API}/providers/billing-api", headers=admin, json=mapping)).status_code == 200
    assert (await client.put(f"{API}/providers/Bad Name", headers=admin, json=mapping)).status_code == 422
    report = {
        "provider": "billing-api",
        "status": 200,
        "headers": {"x-quota-left": "37", "x-quota-reset": "120"},
    }
    await client.post(f"{API}/providers/report", headers=service_key, json=report)
    providers = {p["name"]: p for p in (await client.get(f"{API}/providers", headers=admin)).json()}
    assert providers["billing-api"]["remaining_requests"] == 37
    assert 100 < providers["billing-api"]["reset_requests_in"] <= 120


# ---- configuration and audit ----------------------------------------------


async def test_runtime_config_round_trip_and_validation(client, admin):
    current = (await client.get(f"{API}/config", headers=admin)).json()
    changed = {**current, "adaptive": {**current["adaptive"], "increase_step": 0.1}}
    assert (await client.put(f"{API}/config", headers=admin, json=changed)).json()["adaptive"][
        "increase_step"
    ] == 0.1
    assert (await client.get(f"{API}/config", headers=admin)).json()["adaptive"]["increase_step"] == 0.1

    inverted = {
        **current,
        "adaptive": {**current["adaptive"], "decrease_threshold": 90, "increase_threshold": 50},
    }
    assert (await client.put(f"{API}/config", headers=admin, json=inverted)).status_code == 422
    assert (await client.put(f"{API}/config", headers=admin, json={"surprise": 1})).status_code == 422
    await client.put(f"{API}/config", headers=admin, json=current)


async def test_management_actions_are_audited(client, admin, make_policy):
    policy = await make_policy()
    await client.delete(f"{API}/policies/{policy['id']}", headers=admin)
    entries = (await client.get(f"{API}/audit", headers=admin, params={"action": "policy."})).json()["items"]
    mine = [entry for entry in entries if entry["resource_id"] == policy["id"]]
    assert {entry["action"] for entry in mine} == {"policy.create", "policy.delete"}
    assert all(entry["actor"] == ADMIN_EMAIL and entry["actor_type"] == "user" for entry in mine)
    failed = (await client.get(f"{API}/audit", headers=admin, params={"action": "auth.login_failed"})).json()[
        "items"
    ]
    assert failed, "failed sign-ins must leave a trail"
    assert ADMIN_PASSWORD not in str(entries) + str(failed)


# ---- metrics --------------------------------------------------------------


async def test_live_metrics_series_and_prometheus(client, admin, service_key, make_policy):
    policy = await make_policy(limit=2)
    for _ in range(5):
        await evaluate(client, service_key, policy=policy["name"], user="a")
    await asyncio.sleep(1.1)  # the series ends at the last complete second

    series = (
        await client.get(
            f"{API}/metrics/series", headers=admin, params={"policy": policy["name"], "seconds": 30}
        )
    ).json()
    assert sum(p["allowed"] for p in series["points"]) == 2
    assert sum(p["blocked"] for p in series["points"]) == 3
    assert (
        await client.get(f"{API}/metrics/series", headers=admin, params={"policy": "nope"})
    ).status_code == 404

    live = (await client.get(f"{API}/metrics/live", headers=admin)).json()
    state = next(p for p in live["policies"] if p["name"] == policy["name"])
    assert state["effective_limit"] == 2 and state["algorithm"] == "fixed_window"
    assert live["allowed_last_minute"] >= 2 and live["blocked_last_minute"] >= 3

    text = (await client.get("/metrics")).text
    assert f'adaptirate_decisions_total{{policy="{policy["name"]}",outcome="allowed"}} 2' in text
    assert f'adaptirate_decisions_total{{policy="{policy["name"]}",outcome="blocked"}} 3' in text
    assert f'adaptirate_effective_limit{{policy="{policy["name"]}"}} 2.0' in text

    top = (await client.get(f"{API}/analytics/top", headers=admin, params={"hours": 1, "limit": 100})).json()
    assert {"policy": policy["name"], "subject": "a", "requests": 3} in top["blocked"]


async def test_monitoring_reports_dependencies(client, admin):
    body = (await client.get(f"{API}/monitoring", headers=admin)).json()
    assert body["redis"]["ok"] and body["redis"]["latency_ms"] >= 0
    assert body["postgres"]["ok"] and body["postgres"]["size_bytes"] > 0
    names = {check["name"] for check in body["checks"]}
    assert names == {"Redis", "PostgreSQL", "Adaptive worker", "Policy cache"}


async def test_stream_sends_metrics_and_new_decisions(client, admin, service_key, make_policy, redis):
    # The in-process test transport buffers whole responses, so drive the generator directly.
    from app.api.observe import stream_frames

    app = client._transport.app  # type: ignore[attr-defined]
    policy = await make_policy(limit=1)

    async def never_disconnected() -> bool:
        return False

    frames = stream_frames(redis, app.state.cache, never_disconnected)
    assert await anext(frames) == b"retry: 2000\n\n"
    first = await anext(frames)
    assert first.startswith(b"event: metrics\ndata: {") and first.endswith(b"\n\n")
    assert b'"requests_per_second"' in first

    for _ in range(2):
        await evaluate(client, service_key, policy=policy["name"], user="streamed")
    await app.state.events.flush()
    received = b""
    for _ in range(3):
        received += await anext(frames)
        if b"event: decisions" in received:
            break
    await frames.aclose()
    assert b"event: decisions" in received
    assert received.count(b'"subject":"streamed"') == 2
    assert b'"allowed":false' in received and b'"reason":"limit"' in received


async def test_security_headers_on_every_response(client, admin):
    for response in (
        await client.get("/health"),
        await client.get(f"{API}/policies"),
        await client.get(f"{API}/policies", headers=admin),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert "cache-control" not in (await client.get("/health")).headers
    login = await client.post(
        f"{API}/auth/login", json={"email": "nobody@test.local", "password": "wrong-password-1"}
    )
    assert login.headers["cache-control"] == "no-store", "auth responses must not be cached"

"""SDK against a running AdaptiRate server.

Skipped unless ADAPTIRATE_TEST_URL, ADAPTIRATE_TEST_ADMIN_EMAIL and
ADAPTIRATE_TEST_ADMIN_PASSWORD are set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from adaptirate import AdaptiRateError, AdaptiveLimiter, AsyncAdaptiveLimiter

URL = os.environ.get("ADAPTIRATE_TEST_URL")
EMAIL = os.environ.get("ADAPTIRATE_TEST_ADMIN_EMAIL")
PASSWORD = os.environ.get("ADAPTIRATE_TEST_ADMIN_PASSWORD")

pytestmark = pytest.mark.skipif(
    not (URL and EMAIL and PASSWORD), reason="no live AdaptiRate server configured"
)


class Admin:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=f"{URL}/api/v1", timeout=10)
        login = self.http.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        login.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        self.created: list[str] = []
        key = self.http.post(
            "/keys", json={"name": f"sdk-tests-{uuid.uuid4().hex[:6]}", "scopes": ["evaluate", "report"]}
        )
        key.raise_for_status()
        self.key_id = key.json()["id"]
        self.secret: str = key.json()["secret"]

    def policy(self, **overrides: Any) -> str:
        body = {
            "name": f"sdk-{uuid.uuid4().hex[:10]}",
            "limit": 5,
            "window_seconds": 60,
            "scope": "user",
            "algorithm": "fixed_window",
            "adaptive": False,
        }
        body.update(overrides)
        response = self.http.post("/policies", json=body)
        response.raise_for_status()
        self.created.append(response.json()["id"])
        return str(body["name"])

    def cleanup(self) -> None:
        for policy_id in self.created:
            self.http.delete(f"/policies/{policy_id}")
        self.http.post(f"/keys/{self.key_id}/revoke")
        self.http.close()


@pytest.fixture(scope="module")
def admin() -> Iterator[Admin]:
    instance = Admin()
    yield instance
    instance.cleanup()


def test_sync_client_enforces_a_real_limit(admin: Admin):
    policy = admin.policy(limit=3)
    with AdaptiveLimiter(admin.secret, URL, report_interval=0) as limiter:
        results = [limiter.check(policy, user="alice") for _ in range(5)]
        assert [r.allowed for r in results] == [True, True, True, False, False]
        assert results[3].reason == "limit" and results[3].retry_after_ms > 0
        assert limiter.check(policy, user="bob").allowed
        with pytest.raises(AdaptiRateError) as missing:
            limiter.check("no-such-policy", user="alice")
        assert missing.value.status == 404
        with pytest.raises(AdaptiRateError) as unattributed:
            limiter.check(policy, ip="10.0.0.1")
        assert unattributed.value.status == 422


def test_bad_api_key_is_a_loud_error_not_fail_open(admin: Admin):
    policy = admin.policy()
    with (
        AdaptiveLimiter("ar_wrong-key", URL, report_interval=0) as limiter,
        pytest.raises(AdaptiRateError) as caught,
    ):
        limiter.check(policy, user="alice")
    assert caught.value.status == 401


def test_leasing_never_admits_more_than_the_limit(admin: Admin):
    policy = admin.policy(limit=100, algorithm="token_bucket")
    with AdaptiveLimiter(admin.secret, URL, lease_size=30, report_interval=0) as limiter:
        results = [limiter.check(policy, user="worker") for _ in range(150)]
    assert sum(r.allowed for r in results) == 100
    assert sum(r.source == "lease" for r in results) > 90, "most decisions should be made locally"


async def test_async_client_under_concurrency(admin: Admin):
    policy = admin.policy(limit=40, algorithm="sliding_window")
    async with AsyncAdaptiveLimiter(admin.secret, URL, report_interval=0) as limiter:
        results = await asyncio.gather(*(limiter.check(policy, user="burst") for _ in range(200)))
    assert sum(r.allowed for r in results) == 40


async def test_two_sdk_instances_share_one_limit(admin: Admin):
    """The point of a distributed limiter: separate processes draw from the same budget."""
    policy = admin.policy(limit=20, algorithm="token_bucket")
    async with (
        AsyncAdaptiveLimiter(admin.secret, URL, report_interval=0) as first,
        AsyncAdaptiveLimiter(admin.secret, URL, report_interval=0) as second,
    ):
        results = await asyncio.gather(
            *(first.check(policy, user="shared") for _ in range(30)),
            *(second.check(policy, user="shared") for _ in range(30)),
        )
    assert sum(r.allowed for r in results) == 20


def test_reports_and_provider_feedback_reach_the_server(admin: Admin):
    provider = f"sdk-{uuid.uuid4().hex[:8]}"
    policy = admin.policy(limit=50, provider=provider)
    with AdaptiveLimiter(admin.secret, URL, report_interval=0) as limiter:
        for _ in range(3):
            with limiter.track(policy):
                pass
        limiter.flush()
        assert limiter.check(policy, user="a").allowed
        limiter.report_provider(provider, 429, {"Retry-After": "30"})
        paused = limiter.check(policy, user="a")
        assert (paused.allowed, paused.reason) == (False, "paused")
        assert 25_000 < paused.retry_after_ms <= 30_000
    admin.http.delete(f"/providers/{provider}")

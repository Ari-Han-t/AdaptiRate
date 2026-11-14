"""SDK behaviour against a fake server (httpx.MockTransport). No network."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from adaptirate import AdaptiRateError, AdaptiveLimiter, AsyncAdaptiveLimiter, RateLimitExceeded
from adaptirate._core import LATENCY_BOUNDS_MS, LeasePool, LocalFallback, Reporter, decision_from


def decision(allowed: bool = True, **overrides: Any) -> dict[str, Any]:
    body = {
        "allowed": allowed,
        "limit": 10,
        "remaining": 9 if allowed else 0,
        "retry_after_ms": 0 if allowed else 500,
        "reset_after_ms": 60000,
        "reason": "ok" if allowed else "limit",
        "policy": "api",
    }
    body.update(overrides)
    return body


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], httpx.Headers]] = []
        self.responses: dict[str, Any] = {}
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append((request.url.path, payload, request.headers))
        if self.fail:
            raise httpx.ConnectError("down", request=request)
        response = self.responses.get(request.url.path, decision())
        if callable(response):
            response = response(payload)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def sync_limiter(server: FakeServer, **options: Any) -> AdaptiveLimiter:
    options.setdefault("report_interval", 0)
    return AdaptiveLimiter("ar_test", "http://ar.test", transport=httpx.MockTransport(server), **options)


def test_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("ADAPTIRATE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        AdaptiveLimiter()
    monkeypatch.setenv("ADAPTIRATE_API_KEY", "ar_env")
    monkeypatch.setenv("ADAPTIRATE_URL", "http://example.test/api/v1/")
    limiter = AdaptiveLimiter()
    assert limiter.options.api_key == "ar_env"
    assert limiter.options.base_url == "http://example.test"
    limiter.close()


def test_check_sends_subject_and_key_to_the_right_endpoint(server: FakeServer):
    with sync_limiter(server) as limiter:
        result = limiter.check("api", user="u1", role="premium", cost=3)
    path, payload, headers = server.requests[0]
    assert path == "/api/v1/evaluate"
    assert payload == {"policy": "api", "user": "u1", "role": "premium", "cost": 3}
    assert headers["x-api-key"] == "ar_test"
    assert result.allowed and result.remaining == 9 and result.source == "server"


def test_denied_decision_exposes_retry_headers(server: FakeServer):
    server.responses["/api/v1/evaluate"] = decision(False, retry_after_ms=1500, reset_after_ms=2500)
    with sync_limiter(server) as limiter:
        result = limiter.check("api", user="u1")
        assert not result.allowed and result.retry_after_seconds == 1.5
        assert result.headers() == {
            "X-RateLimit-Limit": "10",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "3",
            "Retry-After": "2",
        }
        with pytest.raises(RateLimitExceeded) as caught:
            limiter.acquire("api", user="u1")
        assert caught.value.retry_after_seconds == 1.5


def test_configuration_errors_are_raised_not_swallowed(server: FakeServer):
    server.responses["/api/v1/evaluate"] = httpx.Response(
        404, json={"detail": "Policy 'nope' does not exist"}
    )
    with sync_limiter(server) as limiter, pytest.raises(AdaptiRateError) as caught:
        limiter.check("nope", user="u1")
    assert caught.value.status == 404 and "does not exist" in caught.value.detail


@pytest.mark.parametrize(
    ("mode", "allowed", "reason"), [("open", True, "fail_open"), ("closed", False, "fail_closed")]
)
def test_fail_mode_when_unreachable(server: FakeServer, mode: str, allowed: bool, reason: str):
    server.fail = True
    with sync_limiter(server, fail_mode=mode) as limiter:
        result = limiter.check("api", user="u1")
    assert (result.allowed, result.reason, result.source) == (allowed, reason, "fallback")


def test_server_errors_use_the_fallback_too(server: FakeServer):
    server.responses["/api/v1/evaluate"] = httpx.Response(503, json={"detail": "down"})
    with sync_limiter(server, fail_mode="closed") as limiter:
        assert not limiter.check("api", user="u1").allowed


def test_breaker_stops_hammering_a_dead_server(server: FakeServer):
    server.fail = True
    with sync_limiter(server, cooldown=60) as limiter:
        for _ in range(20):
            limiter.check("api", user="u1")
    assert len(server.requests) == 1, "after one failure, calls short-circuit until the cooldown passes"


def test_local_fail_mode_enforces_the_last_known_limit(server: FakeServer):
    server.responses["/api/v1/evaluate"] = decision(limit=5, reset_after_ms=60000)
    with sync_limiter(server, fail_mode="local", cooldown=60) as limiter:
        assert limiter.check("api", user="u1").allowed  # learns limit 5 per 60 s
        server.fail = True
        results = [limiter.check("api", user="u1") for _ in range(8)]
        assert [r.allowed for r in results] == [True] * 5 + [False] * 3
        assert results[-1].reason == "fail_local" and results[-1].retry_after_ms > 0
        # Buckets stay per subject during the outage.
        assert limiter.check("api", user="u2").allowed
        # A policy never seen before has no known limit, so it fails open.
        assert limiter.check("other", user="u1").reason == "fail_open"


def test_leasing_spends_locally_and_refills_when_empty(server: FakeServer):
    server.responses["/api/v1/lease"] = lambda payload: {
        **decision(),
        "granted": payload["tokens"],
        "ttl_ms": 2000,
    }
    with sync_limiter(server, lease_size=50) as limiter:
        results = [limiter.check("api", user="u1") for _ in range(120)]
    assert all(r.allowed for r in results)
    assert server.paths() == ["/api/v1/lease"] * 3, "120 checks at 50 per lease is three round trips"
    assert server.requests[0][1] == {"policy": "api", "user": "u1", "tokens": 50}
    assert {r.source for r in results} == {"server", "lease"}


def test_leasing_keeps_separate_pools_per_subject(server: FakeServer):
    server.responses["/api/v1/lease"] = lambda payload: {
        **decision(),
        "granted": payload["tokens"],
        "ttl_ms": 2000,
    }
    with sync_limiter(server, lease_size=10) as limiter:
        limiter.check("api", user="a")
        limiter.check("api", user="b")
        limiter.check("api", user="a")
    assert [payload.get("user") for _, payload, _ in server.requests] == ["a", "b"]


def test_partial_and_empty_leases(server: FakeServer):
    grants = iter([4, 0])
    server.responses["/api/v1/lease"] = lambda payload: {
        **decision(False, retry_after_ms=800),
        "granted": next(grants),
        "ttl_ms": 2000,
    }
    with sync_limiter(server, lease_size=10) as limiter:
        results = [limiter.check("api", user="u1") for _ in range(8)]
    assert [r.allowed for r in results] == [True] * 4 + [False] * 4
    # After the empty grant the SDK denies locally instead of asking again on every request.
    assert len(server.requests) == 2
    assert results[-1].retry_after_ms > 0


def test_expired_lease_tokens_are_discarded():
    now = [0.0]
    pool = LeasePool(clock=lambda: now[0])
    bucket = ("api", "", "u1", "", "", "")
    granted = decision_from({**decision(), "granted": 10, "ttl_ms": 1000})
    assert pool.store(bucket, 10, 1, 1000, granted).allowed
    assert pool.try_spend(bucket, 1, "api") is not None
    now[0] = 1.5  # past the TTL: the remaining 8 tokens must not be spent
    assert pool.try_spend(bucket, 1, "api") is None


def test_check_many_uses_the_batch_endpoint(server: FakeServer):
    server.responses["/api/v1/evaluate/batch"] = {"results": [decision(), decision(False)]}
    with sync_limiter(server) as limiter:
        results = limiter.check_many(
            [{"policy": "api", "user": "a"}, {"policy": "api", "user": "b", "cost": 2}]
        )
    assert [r.allowed for r in results] == [True, False]
    assert server.requests[0][1] == {
        "checks": [{"policy": "api", "user": "a", "cost": 1}, {"policy": "api", "user": "b", "cost": 2}]
    }


def test_wait_sleeps_for_retry_after_then_succeeds(server: FakeServer, monkeypatch):
    answers = iter([decision(False, retry_after_ms=250), decision(False, retry_after_ms=100), decision()])
    server.responses["/api/v1/evaluate"] = lambda payload: next(answers)
    slept: list[float] = []
    monkeypatch.setattr("adaptirate.client.time.sleep", slept.append)
    with sync_limiter(server) as limiter:
        assert limiter.wait("api", user="u1").allowed
    assert slept == [0.25, 0.1]


def test_wait_gives_up_when_it_would_exceed_max_wait(server: FakeServer):
    server.responses["/api/v1/evaluate"] = decision(False, retry_after_ms=60000)
    with sync_limiter(server) as limiter, pytest.raises(RateLimitExceeded):
        limiter.wait("api", user="u1", max_wait=1)


def test_decorator_limits_and_reports(server: FakeServer):
    with sync_limiter(server) as limiter:

        @limiter.limit("emails", subject=lambda user_id: {"user": user_id})
        def send(user_id: str) -> str:
            if user_id == "boom":
                raise RuntimeError("smtp down")
            return f"sent to {user_id}"

        assert send("u1") == "sent to u1"
        with pytest.raises(RuntimeError):
            send("boom")
        limiter.flush()
    reports = [payload for path, payload, _ in server.requests if path == "/api/v1/report"]
    assert len(reports) == 1
    assert (reports[0]["policy"], reports[0]["requests"], reports[0]["errors"]) == ("emails", 2, 1)
    assert sum(reports[0]["latency_buckets"]) == 2 and reports[0]["in_flight"] == 0


def test_reporter_histogram_and_in_flight_gauge():
    reporter = Reporter("instance-1")
    reporter.started("api")
    reporter.started("api")
    reporter.finished("api", 3.0)
    [payload] = reporter.drain()
    assert payload["in_flight"] == 1, "one request is still running"
    assert payload["latency_buckets"][2] == 1  # 3 ms falls in the (2, 5] bucket
    assert len(payload["latency_buckets"]) == len(LATENCY_BOUNDS_MS) + 1
    # Nothing finished since, but work is still in flight: keep reporting it.
    [stalled] = reporter.drain()
    assert (stalled["requests"], stalled["in_flight"]) == (0, 1)
    reporter.finished("api", 99999.0, error=True)
    [last] = reporter.drain()
    assert last["latency_buckets"][-1] == 1 and last["errors"] == 1
    assert reporter.drain() == []


def test_record_classifies_status_codes(server: FakeServer):
    with sync_limiter(server) as limiter:
        limiter.record("api", 10, status=200)
        limiter.record("api", 10, status=503)
        limiter.record("api", 10, status=429)  # our own 429: deliberate, not ill health
        limiter.record("api", 10, status=404)
        limiter.record("api", 10, status=502, throttled=True)  # failed because upstream said 429
        limiter.flush()
    report = next(payload for path, payload, _ in server.requests if path == "/api/v1/report")
    assert (report["requests"], report["errors"], report["throttled"]) == (5, 2, 1)


def test_provider_reports_and_httpx_hook(server: FakeServer):
    with sync_limiter(server) as limiter:
        limiter.report_provider("openai", 429, {"Retry-After": "7"})
        hook = limiter.httpx_hook("anthropic")
        hook(httpx.Response(200, headers={"anthropic-ratelimit-requests-remaining": "40"}))
    first, second = (payload for _, payload, _ in server.requests)
    assert first == {"provider": "openai", "status": 429, "headers": {"Retry-After": "7"}}
    assert second["provider"] == "anthropic" and second["status"] == 200
    assert second["headers"]["anthropic-ratelimit-requests-remaining"] == "40"


def test_local_fallback_refills_over_time():
    now = [0.0]
    fallback = LocalFallback("local", clock=lambda: now[0])
    fallback.learn(decision_from(decision(limit=2, reset_after_ms=1000)))
    bucket = ("api", "", "u", "", "", "")
    assert [fallback.decide("api", bucket, 1).allowed for _ in range(3)] == [True, True, False]
    now[0] = 0.5  # 2 per second: one token back after half a second
    assert [fallback.decide("api", bucket, 1).allowed for _ in range(2)] == [True, False]


# ---- async client ---------------------------------------------------------


def async_limiter(server: FakeServer, **options: Any) -> AsyncAdaptiveLimiter:
    options.setdefault("report_interval", 0)
    return AsyncAdaptiveLimiter("ar_test", "http://ar.test", transport=httpx.MockTransport(server), **options)


async def test_async_check_and_acquire(server: FakeServer):
    async with async_limiter(server) as limiter:
        assert (await limiter.check("api", ip="10.0.0.1")).allowed
        server.responses["/api/v1/evaluate"] = decision(False)
        with pytest.raises(RateLimitExceeded):
            await limiter.acquire("api", ip="10.0.0.1")
    assert server.requests[0][1] == {"policy": "api", "ip": "10.0.0.1", "cost": 1}


async def test_async_concurrent_checks_share_one_lease_request(server: FakeServer):
    import asyncio

    server.responses["/api/v1/lease"] = lambda payload: {
        **decision(),
        "granted": payload["tokens"],
        "ttl_ms": 2000,
    }
    async with async_limiter(server, lease_size=100) as limiter:
        results = await asyncio.gather(*(limiter.check("api", user="u1") for _ in range(100)))
    assert all(r.allowed for r in results)
    assert len(server.requests) == 1, "a burst must not send one lease request per task"


async def test_async_track_reports_errors_and_timeouts(server: FakeServer):
    async with async_limiter(server) as limiter:
        async with limiter.track("jobs"):
            pass
        with pytest.raises(TimeoutError):
            async with limiter.track("jobs"):
                raise TimeoutError
        with pytest.raises(KeyError):
            async with limiter.track("jobs"):
                raise KeyError("x")
        await limiter.flush()
    report = next(payload for path, payload, _ in server.requests if path == "/api/v1/report")
    assert (report["requests"], report["errors"], report["timeouts"]) == (3, 1, 1)


async def test_async_fail_closed(server: FakeServer):
    server.fail = True
    async with async_limiter(server, fail_mode="closed") as limiter:
        assert not (await limiter.check("api", user="u")).allowed


# ---- middleware -----------------------------------------------------------


async def run_asgi(app: Any, path: str = "/") -> tuple[int, dict[str, str], bytes]:
    scope = {"type": "http", "path": path, "method": "GET", "client": ("203.0.113.9", 1234), "headers": []}
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


async def test_asgi_middleware_allows_blocks_and_reports(server: FakeServer):
    from adaptirate.asgi import AdaptiRateMiddleware

    async def inner(scope, receive, send):
        status = 500 if scope["path"] == "/broken" else 200
        await send({"type": "http.response.start", "status": status, "headers": [(b"x-app", b"1")]})
        await send({"type": "http.response.body", "body": b"hello"})

    async with async_limiter(server) as limiter:
        app = AdaptiRateMiddleware(inner, limiter=limiter, policy="api", exclude=("/health",))

        status, headers, body = await run_asgi(app)
        assert (status, body) == (200, b"hello")
        assert headers["x-app"] == "1" and headers["x-ratelimit-limit"] == "10"
        assert server.requests[0][1] == {"policy": "api", "ip": "203.0.113.9", "cost": 1}

        await run_asgi(app, "/broken")
        await run_asgi(app, "/health")
        assert len([p for p in server.paths() if p.endswith("/evaluate")]) == 2, "/health is excluded"

        server.responses["/api/v1/evaluate"] = decision(False, retry_after_ms=2000)
        status, headers, body = await run_asgi(app)
        assert status == 429 and headers["retry-after"] == "2"
        assert json.loads(body)["reason"] == "limit"

        await limiter.flush()
    report = next(payload for path, payload, _ in server.requests if path == "/api/v1/report")
    assert (report["requests"], report["errors"]) == (2, 1), (
        "blocked and excluded requests are not app traffic"
    )


def test_wsgi_middleware_allows_blocks_and_reports(server: FakeServer):
    from adaptirate.wsgi import AdaptiRateWSGIMiddleware

    def inner(environ, start_response):
        start_response("200 OK", [("X-App", "1")])
        return [b"hello"]

    def call(app, path="/"):
        captured: dict[str, Any] = {}

        def start_response(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, dict(headers)

        body = b"".join(app({"PATH_INFO": path, "REMOTE_ADDR": "198.51.100.4"}, start_response))
        return captured["status"], captured["headers"], body

    with sync_limiter(server) as limiter:
        app = AdaptiRateWSGIMiddleware(inner, limiter=limiter, policy="api")
        status, headers, body = call(app)
        assert (status, body) == ("200 OK", b"hello")
        assert headers["X-App"] == "1" and headers["X-RateLimit-Remaining"] == "9"
        assert server.requests[0][1] == {"policy": "api", "ip": "198.51.100.4", "cost": 1}

        server.responses["/api/v1/evaluate"] = decision(False, retry_after_ms=900)
        status, headers, body = call(app)
        assert status.startswith("429") and headers["Retry-After"] == "1"
        limiter.flush()
    report = next(payload for path, payload, _ in server.requests if path == "/api/v1/report")
    assert report["requests"] == 1

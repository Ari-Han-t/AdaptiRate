"""Asyncio client. Same API as AdaptiveLimiter, with awaitable methods."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import httpx

from ._core import (
    API_PREFIX,
    AdaptiRateError,
    Breaker,
    Decision,
    FailMode,
    KeyedLocks,
    LeasePool,
    LocalFallback,
    Options,
    RateLimitExceeded,
    Reporter,
    bucket_id,
    decision_from,
    error_detail,
    host_gauges,
    is_server_error,
    subject_payload,
)

logger = logging.getLogger("adaptirate")
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


class AsyncAdaptiveLimiter:
    """Talks to an AdaptiRate server from asyncio code.

    limiter = AsyncAdaptiveLimiter(api_key="ar_...")
    decision = await limiter.check("api", user=user_id)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 1.0,
        fail_mode: FailMode = "open",
        lease_size: int = 0,
        report_interval: float = 1.0,
        cooldown: float = 1.0,
        max_connections: int = 100,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.options = Options.resolve(
            api_key,
            base_url,
            timeout=timeout,
            fail_mode=fail_mode,
            lease_size=lease_size,
            report_interval=report_interval,
            cooldown=cooldown,
        )
        self._http = httpx.AsyncClient(
            base_url=self.options.base_url + API_PREFIX,
            headers=self.options.headers,
            timeout=self.options.timeout,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            transport=transport,
        )
        self._leases = LeasePool()
        self._fallback = LocalFallback(self.options.fail_mode)
        self._breaker = Breaker(self.options.cooldown)
        self._reporter = Reporter(self.options.instance_id)
        self._flusher: asyncio.Task[None] | None = None
        # One lease request per bucket at a time, so a burst does not stampede the server.
        self._lease_locks: KeyedLocks[asyncio.Lock] = KeyedLocks(asyncio.Lock)
        self._closed = False

    # ---- decisions --------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self._breaker.is_open:
            return None
        try:
            response = await self._http.post(path, json=payload)
        except httpx.HTTPError as exc:
            self._breaker.failure()
            logger.warning("AdaptiRate unreachable (%s); using fail_mode=%s", exc, self.options.fail_mode)
            return None
        if response.status_code >= 500:
            self._breaker.failure()
            logger.warning(
                "AdaptiRate returned %s; using fail_mode=%s", response.status_code, self.options.fail_mode
            )
            return None
        self._breaker.success()
        # A gateway can answer with an HTML page; never let that escape as a JSON error.
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            if body is None:
                raise AdaptiRateError(response.status_code, "unreadable response body")
            raise error_detail(response.status_code, body)
        if not isinstance(body, dict):
            self._breaker.failure()
            logger.warning(
                "AdaptiRate returned an unreadable body; using fail_mode=%s", self.options.fail_mode
            )
            return None
        return body

    async def check(
        self,
        policy: str,
        *,
        key: str | None = None,
        user: str | None = None,
        ip: str | None = None,
        api_key: str | None = None,
        role: str | None = None,
        cost: int = 1,
    ) -> Decision:
        """Ask whether one request may proceed. Never raises for a denied request."""
        payload = subject_payload(policy, key=key, user=user, ip=ip, api_key=api_key, role=role)
        bucket = bucket_id(payload)

        if self.options.lease_size > 0:
            local = self._leases.try_spend(bucket, cost, policy)
            if local is not None:
                return local
            lock = self._lease_locks.get(bucket)
            async with lock:
                # Another task may have refilled the lease while this one waited.
                local = self._leases.try_spend(bucket, cost, policy)
                if local is not None:
                    return local
                body = await self._post("/lease", {**payload, "tokens": max(self.options.lease_size, cost)})
                if body is None:
                    return self._fallback.decide(policy, bucket, cost)
                decision = decision_from(body)
                self._fallback.learn(decision)
                return self._leases.store(bucket, int(body["granted"]), cost, int(body["ttl_ms"]), decision)

        body = await self._post("/evaluate", {**payload, "cost": cost})
        if body is None:
            return self._fallback.decide(policy, bucket, cost)
        decision = decision_from(body)
        self._fallback.learn(decision)
        return decision

    async def check_many(self, checks: list[dict[str, Any]]) -> list[Decision]:
        """Decide several requests in one round trip."""
        payloads = [
            {
                **subject_payload(
                    item["policy"],
                    key=item.get("key"),
                    user=item.get("user"),
                    ip=item.get("ip"),
                    api_key=item.get("api_key"),
                    role=item.get("role"),
                ),
                "cost": int(item.get("cost", 1)),
            }
            for item in checks
        ]
        body = await self._post("/evaluate/batch", {"checks": payloads})
        if body is None:
            return [self._fallback.decide(p["policy"], bucket_id(p), p["cost"]) for p in payloads]
        decisions = [decision_from(item) for item in body["results"]]
        for decision in decisions:
            self._fallback.learn(decision)
        return decisions

    async def acquire(self, policy: str, **subject: Any) -> Decision:
        """Like check(), but raises RateLimitExceeded when denied."""
        decision = await self.check(policy, **subject)
        if not decision.allowed:
            raise RateLimitExceeded(decision)
        return decision

    async def wait(self, policy: str, *, max_wait: float = 30.0, **subject: Any) -> Decision:
        """Wait until allowed, sleeping for the server's retry_after between attempts."""
        deadline = time.monotonic() + max_wait
        while True:
            decision = await self.check(policy, **subject)
            if decision.allowed:
                return decision
            delay = decision.retry_after_seconds
            if decision.retry_after_ms < 0 or time.monotonic() + delay > deadline:
                raise RateLimitExceeded(decision)
            await asyncio.sleep(max(delay, 0.001))

    def limit(
        self, policy: str, *, subject: Callable[..., Mapping[str, Any]] | None = None
    ) -> Callable[[F], F]:
        """Decorator for coroutine functions: rate-limit and report latency and failures."""

        def decorate(function: F) -> F:
            @functools.wraps(function)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                chosen = dict(subject(*args, **kwargs)) if subject else {"key": function.__name__}
                await self.acquire(policy, **chosen)
                async with self.track(policy):
                    return await function(*args, **kwargs)

            return wrapper  # type: ignore[return-value]

        return decorate

    # ---- signals ----------------------------------------------------------

    @asynccontextmanager
    async def track(self, policy: str) -> AsyncIterator[None]:
        """Time a unit of work and report it to the adaptive engine."""
        self._ensure_flusher()
        self._reporter.started(policy)
        started = time.perf_counter()
        failed = timed_out = False
        try:
            yield
        except (TimeoutError, asyncio.TimeoutError):
            failed = timed_out = True
            raise
        except Exception:
            failed = True
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._reporter.finished(policy, elapsed_ms, error=failed and not timed_out, timeout=timed_out)

    def started(self, policy: str) -> None:
        """Mark a request as in flight. Pair with finished()."""
        self._ensure_flusher()
        self._reporter.started(policy)

    def finished(
        self,
        policy: str,
        latency_ms: float,
        *,
        status: int | None = None,
        error: bool = False,
        throttled: bool = False,
    ) -> None:
        """Complete a request marked with started().

        `status` is the response this service produced; only 5xx counts as an error.
        Pass throttled=True when the work failed because an upstream rejected it with 429.
        """
        failed = error or (status is not None and is_server_error(status))
        self._reporter.finished(policy, latency_ms, error=failed, throttled=throttled)

    def set_queue_depth(self, policy: str, depth: int) -> None:
        self._ensure_flusher()
        self._reporter.set_queue_depth(policy, depth)

    async def report_provider(self, provider: str, status: int, headers: Mapping[str, str]) -> None:
        """Tell AdaptiRate what an upstream provider answered."""
        payload = {
            "provider": provider,
            "status": status,
            "headers": {str(k)[:128]: str(v)[:1024] for k, v in list(headers.items())[:100]},
        }
        try:
            await self._post("/providers/report", payload)
        except Exception:
            logger.warning("failed to report provider response", exc_info=True)

    def httpx_hook(self, provider: str) -> Callable[[httpx.Response], Awaitable[None]]:
        """An httpx.AsyncClient response hook that reports every provider response."""

        async def hook(response: httpx.Response) -> None:
            await self.report_provider(provider, response.status_code, response.headers)

        return hook

    async def flush(self) -> None:
        gauges = host_gauges()
        for payload in self._reporter.drain():
            try:
                await self._post("/report", {**payload, **gauges})
            except Exception:
                logger.warning("failed to send report for %s", payload["policy"], exc_info=True)

    def _ensure_flusher(self) -> None:
        if self._flusher is None and self.options.report_interval > 0 and not self._closed:
            self._flusher = asyncio.get_running_loop().create_task(
                self._flush_loop(), name="adaptirate-report"
            )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.options.report_interval)
            await self.flush()

    # ---- lifecycle --------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
        await self.flush()
        await self._http.aclose()

    async def __aenter__(self) -> AsyncAdaptiveLimiter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

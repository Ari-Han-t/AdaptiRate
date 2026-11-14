"""Synchronous client."""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

import httpx

from ._core import (
    API_PREFIX,
    Breaker,
    Decision,
    FailMode,
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
F = TypeVar("F", bound=Callable[..., Any])


class AdaptiveLimiter:
    """Talks to an AdaptiRate server. Safe to share across threads.

    limiter = AdaptiveLimiter(api_key="ar_...")
    if limiter.check("api", user=user_id).allowed:
        ...
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
        transport: httpx.BaseTransport | None = None,
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
        self._http = httpx.Client(
            base_url=self.options.base_url + API_PREFIX,
            headers=self.options.headers,
            timeout=self.options.timeout,
            transport=transport,
        )
        self._leases = LeasePool()
        self._fallback = LocalFallback(self.options.fail_mode)
        self._breaker = Breaker(self.options.cooldown)
        self._reporter = Reporter(self.options.instance_id)
        self._closed = threading.Event()
        self._flusher: threading.Thread | None = None
        self._flusher_lock = threading.Lock()

    # ---- decisions --------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The JSON body, or None when the server could not be reached or failed."""
        if self._breaker.is_open:
            return None
        try:
            response = self._http.post(path, json=payload)
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
        body = response.json()
        if response.status_code >= 400:
            raise error_detail(response.status_code, body)
        return body  # type: ignore[no-any-return]

    def check(
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
            body = self._post("/lease", {**payload, "tokens": max(self.options.lease_size, cost)})
            if body is None:
                return self._fallback.decide(policy, bucket, cost)
            decision = decision_from(body)
            self._fallback.learn(decision)
            return self._leases.store(bucket, int(body["granted"]), cost, int(body["ttl_ms"]), decision)

        body = self._post("/evaluate", {**payload, "cost": cost})
        if body is None:
            return self._fallback.decide(policy, bucket, cost)
        decision = decision_from(body)
        self._fallback.learn(decision)
        return decision

    def check_many(self, checks: list[dict[str, Any]]) -> list[Decision]:
        """Decide several requests in one round trip. Each item takes the same
        arguments as check(), with 'policy' required."""
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
        body = self._post("/evaluate/batch", {"checks": payloads})
        if body is None:
            return [self._fallback.decide(p["policy"], bucket_id(p), p["cost"]) for p in payloads]
        decisions = [decision_from(item) for item in body["results"]]
        for decision in decisions:
            self._fallback.learn(decision)
        return decisions

    def acquire(self, policy: str, **subject: Any) -> Decision:
        """Like check(), but raises RateLimitExceeded when denied."""
        decision = self.check(policy, **subject)
        if not decision.allowed:
            raise RateLimitExceeded(decision)
        return decision

    def wait(self, policy: str, *, max_wait: float = 30.0, **subject: Any) -> Decision:
        """Block until allowed, sleeping for the server's retry_after between attempts.
        Raises RateLimitExceeded if that would take longer than max_wait."""
        deadline = time.monotonic() + max_wait
        while True:
            decision = self.check(policy, **subject)
            if decision.allowed:
                return decision
            delay = decision.retry_after_seconds
            if decision.retry_after_ms < 0 or time.monotonic() + delay > deadline:
                raise RateLimitExceeded(decision)
            time.sleep(max(delay, 0.001))

    def limit(
        self, policy: str, *, subject: Callable[..., Mapping[str, Any]] | None = None
    ) -> Callable[[F], F]:
        """Decorator: rate-limit a function and report its latency and failures.

        @limiter.limit("emails", subject=lambda user_id, *a, **k: {"user": user_id})
        def send_email(user_id, ...): ...
        """

        def decorate(function: F) -> F:
            @functools.wraps(function)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.acquire(
                    policy, **(dict(subject(*args, **kwargs)) if subject else {"key": function.__name__})
                )
                with self.track(policy):
                    return function(*args, **kwargs)

            return wrapper  # type: ignore[return-value]

        return decorate

    # ---- signals ----------------------------------------------------------

    @contextmanager
    def track(self, policy: str) -> Iterator[None]:
        """Time a unit of work and report it, so the adaptive engine can see how the
        protected service is doing. Exceptions count as errors."""
        self._ensure_flusher()
        self._reporter.started(policy)
        started = time.perf_counter()
        failed = timed_out = False
        try:
            yield
        except TimeoutError:
            failed = timed_out = True
            raise
        except Exception:
            failed = True
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._reporter.finished(policy, elapsed_ms, error=failed and not timed_out, timeout=timed_out)

    def record(
        self,
        policy: str,
        latency_ms: float,
        *,
        status: int | None = None,
        error: bool = False,
        throttled: bool = False,
    ) -> None:
        """Report one completed request when track() does not fit (for example in middleware).

        `status` is the response this service produced; only 5xx counts as an error.
        Pass throttled=True when the work failed because an upstream rejected it with 429.
        """
        self._ensure_flusher()
        failed = error or (status is not None and is_server_error(status))
        self._reporter.started(policy)
        self._reporter.finished(policy, latency_ms, error=failed, throttled=throttled)

    def set_queue_depth(self, policy: str, depth: int) -> None:
        self._ensure_flusher()
        self._reporter.set_queue_depth(policy, depth)

    def report_provider(self, provider: str, status: int, headers: Mapping[str, str]) -> None:
        """Tell AdaptiRate what an upstream provider answered, so it can pace every
        worker against the provider's remaining quota and pause on a 429."""
        payload = {
            "provider": provider,
            "status": status,
            "headers": {str(k)[:128]: str(v)[:1024] for k, v in list(headers.items())[:100]},
        }
        try:
            self._post("/providers/report", payload)
        except Exception:
            logger.warning("failed to report provider response", exc_info=True)

    def httpx_hook(self, provider: str) -> Callable[[httpx.Response], None]:
        """An httpx response hook that reports every response from a provider:

        httpx.Client(event_hooks={"response": [limiter.httpx_hook("openai")]})
        """

        def hook(response: httpx.Response) -> None:
            self.report_provider(provider, response.status_code, response.headers)

        return hook

    def flush(self) -> None:
        """Send pending reports now."""
        gauges = host_gauges()
        for payload in self._reporter.drain():
            try:
                self._post("/report", {**payload, **gauges})
            except Exception:
                logger.warning("failed to send report for %s", payload["policy"], exc_info=True)

    def _ensure_flusher(self) -> None:
        if self._flusher is not None or self.options.report_interval <= 0:
            return
        with self._flusher_lock:
            if self._flusher is None:
                self._flusher = threading.Thread(
                    target=self._flush_loop, name="adaptirate-report", daemon=True
                )
                self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._closed.wait(self.options.report_interval):
            self.flush()

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._flusher is not None:
            self._flusher.join(timeout=self.options.timeout + 1)
        self.flush()
        self._http.close()

    def __enter__(self) -> AdaptiveLimiter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

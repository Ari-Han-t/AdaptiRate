"""WSGI middleware for Flask, Django and any other WSGI framework.

app.wsgi_app = AdaptiRateWSGIMiddleware(app.wsgi_app, limiter=limiter, policy="api")   # Flask
application = AdaptiRateWSGIMiddleware(get_wsgi_application(), limiter=limiter, policy="api")  # Django
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from .client import AdaptiveLimiter

Environ = dict[str, Any]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[Environ, StartResponse], Iterable[bytes]]
Identify = Callable[[Environ], Mapping[str, Any]]


def client_address(environ: Environ) -> Mapping[str, Any]:
    """Default identity: REMOTE_ADDR. Behind a proxy, have your server set REMOTE_ADDR
    from the headers it trusts (for example werkzeug's ProxyFix), or pass `identify`."""
    return {"ip": environ.get("REMOTE_ADDR") or "unknown"}


class _TrackedBody:
    """The app's response iterable, passed through lazily.

    PEP 3333 requires close() to reach the app's iterable (Django fires request_finished from
    it), and buffering the body would break streaming. The request is recorded exactly once:
    when the body is exhausted or fails, or in close() if the server stops early.
    """

    def __init__(self, body: Iterable[bytes], finish: Callable[[bool], None]) -> None:
        self._body = body
        self._iterator: Iterator[bytes] | None = None
        self._finish = finish
        self._finished = False

    def __iter__(self) -> _TrackedBody:
        return self

    def __next__(self) -> bytes:
        if self._iterator is None:
            self._iterator = iter(self._body)
        try:
            return next(self._iterator)
        except StopIteration:
            self._record(error=False)
            raise
        except BaseException:
            self._record(error=True)
            raise

    def close(self) -> None:
        try:
            close = getattr(self._body, "close", None)
            if close is not None:
                close()
        finally:
            self._record(error=False)

    def _record(self, *, error: bool) -> None:
        if not self._finished:
            self._finished = True
            self._finish(error)


class AdaptiRateWSGIMiddleware:
    def __init__(
        self,
        app: WSGIApp,
        *,
        limiter: AdaptiveLimiter,
        policy: str,
        identify: Identify = client_address,
        exclude: tuple[str, ...] = (),
        cost: int = 1,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.policy = policy
        self.identify = identify
        self.exclude = exclude
        self.cost = cost

    def __call__(self, environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "")
        if any(path.startswith(prefix) for prefix in self.exclude):
            return self.app(environ, start_response)

        decision = self.limiter.check(self.policy, cost=self.cost, **dict(self.identify(environ)))
        if not decision.allowed:
            body = json.dumps(
                {
                    "detail": "Rate limit exceeded",
                    "policy": decision.policy,
                    "reason": decision.reason,
                    "retry_after_ms": decision.retry_after_ms,
                }
            ).encode()
            headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
            start_response("429 Too Many Requests", headers + list(decision.headers().items()))
            return [body]

        started = time.perf_counter()
        status_code = 500
        extra = list(decision.headers().items())

        def start_with_headers(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            nonlocal status_code
            status_code = int(status.split(" ", 1)[0])
            return start_response(status, [*headers, *extra], exc_info)

        def finish(error: bool) -> None:
            self.limiter.record(
                self.policy, (time.perf_counter() - started) * 1000, status=status_code, error=error
            )

        try:
            app_body = self.app(environ, start_with_headers)
        except BaseException:
            finish(True)
            raise
        # Latency covers streaming the body, recorded when the server finishes with it.
        return _TrackedBody(app_body, finish)

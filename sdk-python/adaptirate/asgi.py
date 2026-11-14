"""ASGI middleware for FastAPI, Starlette and any other ASGI framework.

from adaptirate import AsyncAdaptiveLimiter
from adaptirate.asgi import AdaptiRateMiddleware

limiter = AsyncAdaptiveLimiter()
app.add_middleware(AdaptiRateMiddleware, limiter=limiter, policy="api")
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from ._core import Decision
from .aio import AsyncAdaptiveLimiter

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Identify = Callable[[Scope], Mapping[str, Any]]


def client_address(scope: Scope) -> Mapping[str, Any]:
    """Default identity: the connecting address.

    Behind a reverse proxy this is the proxy's address. Configure your server to
    rewrite the client address from the proxy headers it trusts (for uvicorn,
    --proxy-headers with --forwarded-allow-ips), or pass your own `identify`.
    Reading X-Forwarded-For here would let any caller pick its own identity.
    """
    client = scope.get("client")
    return {"ip": client[0] if client else "unknown"}


class AdaptiRateMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: AsyncAdaptiveLimiter,
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or any(scope["path"].startswith(prefix) for prefix in self.exclude):
            await self.app(scope, receive, send)
            return

        decision = await self.limiter.check(self.policy, cost=self.cost, **dict(self.identify(scope)))
        if not decision.allowed:
            await self._reject(send, decision)
            return

        self.limiter.started(self.policy)
        started = time.perf_counter()
        status = 500
        extra = [(name.lower().encode(), value.encode()) for name, value in decision.headers().items()]

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            self.limiter.finished(self.policy, (time.perf_counter() - started) * 1000, status=status)

    @staticmethod
    async def _reject(send: Send, decision: Decision) -> None:
        body = json.dumps(
            {
                "detail": "Rate limit exceeded",
                "policy": decision.policy,
                "reason": decision.reason,
                "retry_after_ms": decision.retry_after_ms,
            }
        ).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers += [(name.lower().encode(), value.encode()) for name, value in decision.headers().items()]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})

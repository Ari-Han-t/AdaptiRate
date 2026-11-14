"""AdaptiRate API server."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import orjson
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import admin, auth, evaluate, logs, observe, policies, users
from .api import keys as keys_api
from .config import Settings, get_settings
from .db import dispose_engine, session_factory
from .limiter import keys
from .limiter.cache import RuntimeCache
from .limiter.engine import LimiterEngine
from .models import User
from .redis_client import close_redis, get_redis
from .security import constant_time_equals, hash_password
from .services import metrics
from .services.events import EventBuffer
from .startup import wait_for_dependencies

logger = logging.getLogger("adaptirate")

API_PREFIX = "/api/v1"
HEARTBEAT_SECONDS = 5


async def bootstrap_admin(settings: Settings) -> None:
    """Create the first admin from the environment when the user table is empty."""
    if not settings.bootstrap_admin_email or settings.bootstrap_admin_password is None:
        return
    async with session_factory()() as session:
        if (await session.execute(select(func.count()).select_from(User))).scalar_one() > 0:
            return
        password = settings.bootstrap_admin_password.get_secret_value()
        if len(password) < 12:
            logger.error("AR_BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters; no admin created")
            return
        session.add(
            User(
                email=settings.bootstrap_admin_email.lower(),
                password_hash=hash_password(password),
                role="admin",
            )
        )
        try:
            await session.commit()
            logger.info("created bootstrap admin %s", settings.bootstrap_admin_email)
        except IntegrityError:
            await session.rollback()  # another API process won the race


async def _heartbeat(redis: Redis, events: EventBuffer, cache: RuntimeCache) -> None:
    instance = f"{socket.gethostname()}:{os.getpid()}"
    started = time.time()
    while True:
        try:
            beat = {
                "ts": time.time(),
                "started": started,
                "events_dropped": events.dropped,
                "policies": len(cache.policies),
                "api_keys": len(cache.api_keys),
            }
            await redis.hset(keys.API_HEARTBEATS, instance, orjson.dumps(beat).decode())
        except Exception:
            logger.warning("heartbeat failed", exc_info=True)
        await asyncio.sleep(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redis = get_redis()
    cache = RuntimeCache(redis, session_factory(), settings.cache_refresh_seconds)
    events = EventBuffer(
        redis, per_second=settings.events_per_second, stream_maxlen=settings.event_stream_maxlen
    )
    app.state.redis = redis
    app.state.cache = cache
    app.state.engine = LimiterEngine(redis)
    app.state.events = events
    app.state.trusted_networks = [
        ipaddress.ip_network(item, strict=False) for item in settings.trusted_proxies
    ]

    await wait_for_dependencies(redis, session_factory())
    await bootstrap_admin(settings)
    await cache.start()
    events.start()
    heartbeat = asyncio.create_task(_heartbeat(redis, events, cache), name="api-heartbeat")
    try:
        yield
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await events.stop()
        await cache.stop()
        await close_redis()
        await dispose_engine()


def _prometheus(snapshot: dict[str, Any], totals: dict[str, str]) -> str:
    def label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    lines = [
        "# HELP adaptirate_decisions_total Rate-limit decisions by policy and outcome.",
        "# TYPE adaptirate_decisions_total counter",
    ]
    for field, value in sorted(totals.items()):
        outcome, _, policy = field.partition(":")
        name = "allowed" if outcome == "a" else "blocked"
        lines.append(f'adaptirate_decisions_total{{policy="{label(policy)}",outcome="{name}"}} {int(value)}')
    gauges = (
        ("adaptirate_effective_limit", "Current limit after adaptation.", "effective_limit"),
        ("adaptirate_multiplier", "Adaptive multiplier applied to the base limit.", "multiplier"),
        ("adaptirate_health_score", "Health score 0-100 from the adaptive engine.", "health"),
        ("adaptirate_paused_seconds", "Remaining provider backoff pause.", "paused_ms"),
    )
    states: list[dict[str, Any]] = snapshot["policies"]
    for metric, help_text, gauge_field in gauges:
        lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"]
        for state in states:
            reading = state.get(gauge_field)
            if reading is None:
                continue
            number = float(reading) / 1000 if gauge_field == "paused_ms" else float(reading)
            lines.append(f'{metric}{{policy="{label(str(state["name"]))}"}} {number}')
    return "\n".join(lines) + "\n"


class SecurityHeadersMiddleware:
    """Adds security headers to every response.

    Written as plain ASGI rather than with @app.middleware: that decorator wraps each
    request in a task and a stream, which costs a noticeable share of throughput on the
    evaluate path.
    """

    _ALWAYS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    )
    _AUTH_PREFIX = f"{API_PREFIX}/auth"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        no_store = scope["path"].startswith(self._AUTH_PREFIX)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [pair for pair in self._ALWAYS if pair[0] not in present]
                if no_store and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AdaptiRate",
        version="0.1.0",
        description="Adaptive distributed rate limiting.",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    if settings.cors_origins:
        # Explicit origins only. A wildcard with credentials would let any site act as the user.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        )

    app.add_middleware(SecurityHeadersMiddleware)

    api = APIRouter(prefix=API_PREFIX)
    for module in (auth, users, policies, keys_api, evaluate, observe, logs, admin):
        api.include_router(module.router)
    app.include_router(api)

    @app.get("/health", tags=["Health"], summary="Liveness: the process is up")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["Health"], summary="Readiness: Redis and Postgres are reachable")
    async def ready(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            await request.app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unreachable"
        try:
            async with session_factory()() as session:
                await session.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "unreachable"
        ok = all(value == "ok" for value in checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", **checks},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/metrics", tags=["Health"], summary="Prometheus metrics", response_class=PlainTextResponse)
    async def prometheus(request: Request) -> PlainTextResponse:
        token = settings.metrics_token
        if token is not None:
            header = request.headers.get("authorization", "")
            if not constant_time_equals(header, f"Bearer {token.get_secret_value()}"):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid metrics token")
        redis: Redis = request.app.state.redis
        snapshot = await metrics.live_snapshot(redis, request.app.state.cache)
        totals = await redis.hgetall(keys.TOTALS)
        return PlainTextResponse(_prometheus(snapshot, totals), media_type="text/plain; version=0.0.4")

    return app


app = create_app()

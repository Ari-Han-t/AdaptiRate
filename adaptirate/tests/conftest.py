from __future__ import annotations

import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

os.environ.setdefault("AR_BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("AR_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery")
os.environ.setdefault("AR_CACHE_REFRESH_SECONDS", "3600")

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import Base, dispose_engine, get_engine, session_factory
from app.limiter.cache import RuntimeCache
from app.limiter.engine import LimiterEngine
from app.worker import Worker

ADMIN_EMAIL = os.environ["AR_BOOTSTRAP_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["AR_BOOTSTRAP_ADMIN_PASSWORD"]

_counter = itertools.count()


@pytest.fixture(scope="session")
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(get_settings().redis_url, decode_responses=True, max_connections=300)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def engine(redis: Redis) -> LimiterEngine:
    return LimiterEngine(redis)


@pytest.fixture
def unique() -> str:
    """A fresh identifier so tests never share limiter state."""
    return f"t{next(_counter)}"


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[None]:
    """A dedicated, freshly created schema in the test database."""
    url = make_url(get_settings().database_url)
    assert url.database and url.database.endswith("_test"), (
        "refusing to run tests against a non-test database"
    )
    admin = await asyncpg.connect(
        host=url.host, port=url.port or 5432, user=url.username, password=url.password, database="postgres"
    )
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await admin.close()
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture(scope="session")
async def client(database: None, redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture(scope="session")
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    # Production samples allowed decisions; tests assert on exact log contents.
    config = (await client.get("/api/v1/config", headers=headers)).json()
    config["logging"]["sample_allowed"] = 1.0
    saved = await client.put("/api/v1/config", headers=headers, json=config)
    assert saved.status_code == 200, saved.text
    return headers


@pytest.fixture(scope="session")
async def service_key(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    """An SDK-style API key allowed to evaluate and report."""
    response = await client.post(
        "/api/v1/keys", headers=admin, json={"name": "tests", "scopes": ["evaluate", "report"]}
    )
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["secret"]}


PolicyFactory = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def make_policy(client: httpx.AsyncClient, admin: dict[str, str]) -> PolicyFactory:
    async def create(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": f"policy-{next(_counter)}",
            "limit": 5,
            "window_seconds": 60,
            "algorithm": "fixed_window",
            "scope": "user",
            "adaptive": False,
        }
        body.update(overrides)
        response = await client.post("/api/v1/policies", headers=admin, json=body)
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    return create


@pytest.fixture
async def worker(database: None, redis: Redis, client: httpx.AsyncClient) -> Worker:
    """A worker sharing the test Redis and database, driven by hand rather than by its loops."""
    cache = RuntimeCache(redis, session_factory(), refresh_seconds=3600)
    await cache.load()
    return Worker(redis, session_factory(), cache)

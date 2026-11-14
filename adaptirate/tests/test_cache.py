"""Cross-process cache invalidation."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from redis.asyncio import Redis

from app.config import get_settings
from app.db import session_factory
from app.limiter.cache import RuntimeCache

API = "/api/v1"


async def test_other_processes_pick_up_changes_through_pubsub(client: httpx.AsyncClient, admin, redis: Redis):
    # A second cache stands in for another API process or the worker.
    other = RuntimeCache(redis, session_factory(), refresh_seconds=3600)
    await other.start()
    try:
        await asyncio.sleep(0.2)  # let the subscription settle
        created = await client.post(
            f"{API}/policies", headers=admin, json={"name": "pubsub-policy", "limit": 7}
        )
        assert created.status_code == 201
        for _ in range(40):
            if "pubsub-policy" in other.policies:
                break
            await asyncio.sleep(0.05)
        assert other.policies["pubsub-policy"].limit == 7

        await client.patch(f"{API}/policies/{created.json()['id']}", headers=admin, json={"limit": 9})
        for _ in range(40):
            if other.policies["pubsub-policy"].limit == 9:
                break
            await asyncio.sleep(0.05)
        assert other.policies["pubsub-policy"].limit == 9
    finally:
        await other.stop()


async def test_listener_survives_a_quiet_channel(redis: Redis, database, caplog):
    """An idle subscription must not error out and reconnect (it once did, every 5 s)."""
    quiet = Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=0.5)
    cache = RuntimeCache(quiet, session_factory(), refresh_seconds=3600)
    await cache.start()
    try:
        with caplog.at_level(logging.ERROR, logger="app.limiter.cache"):
            await asyncio.sleep(1.6)  # three socket timeouts' worth of silence
        assert not caplog.records, [record.getMessage() for record in caplog.records]
        reloads = cache.reloads
        await redis.publish("ar:invalidate", "reload")
        for _ in range(40):
            if cache.reloads > reloads:
                break
            await asyncio.sleep(0.05)
        assert cache.reloads > reloads, "still subscribed after the quiet period"
    finally:
        await cache.stop()
        await quiet.aclose()


async def test_startup_waits_for_dependencies_instead_of_crashing(redis: Redis, database):
    from app.startup import wait_for_dependencies

    await wait_for_dependencies(redis, session_factory(), max_wait=5)

    calls = 0

    class FlakyRedis:
        async def ping(self) -> bool:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("not yet")
            return True

    await wait_for_dependencies(FlakyRedis(), session_factory(), max_wait=5, interval=0.01)  # type: ignore[arg-type]
    assert calls == 3

    class DeadRedis:
        async def ping(self) -> bool:
            raise ConnectionError("gone")

    with pytest.raises(RuntimeError, match="not reachable"):
        await wait_for_dependencies(DeadRedis(), session_factory(), max_wait=0.05, interval=0.01)  # type: ignore[arg-type]

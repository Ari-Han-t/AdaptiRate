"""Wait for Redis and Postgres before serving.

Dependencies can be briefly unavailable at boot (Postgres replaying its log after an
unclean shutdown, for example). Waiting here is quicker and quieter than exiting and
relying on the container's restart backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("adaptirate.startup")


async def wait_for_dependencies(
    redis: Redis,
    sessions: async_sessionmaker[AsyncSession],
    *,
    max_wait: float = 60.0,
    interval: float = 1.0,
) -> None:
    deadline = time.monotonic() + max_wait
    attempt = 0
    while True:
        attempt += 1
        try:
            await redis.ping()
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("dependencies ready after %d attempts", attempt)
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Redis or Postgres not reachable after {max_wait:.0f}s") from exc
            logger.warning("waiting for Redis and Postgres (%s: %s)", type(exc).__name__, exc)
            await asyncio.sleep(interval)

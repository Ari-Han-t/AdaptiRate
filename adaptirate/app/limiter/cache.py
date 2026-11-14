"""Process-local copy of policies, API keys and runtime config.

The evaluate path reads only from here, so it never touches Postgres. Writers call
publish_invalidation(); every process reloads on that message and also on a timer,
so a missed pub/sub message heals within cache_refresh_seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import ApiKey, Policy, ProviderConfig, Setting
from ..runtime_config import RUNTIME_SETTING_KEY, RuntimeConfig
from . import keys
from .policy import CompiledPolicy, compile_policy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedKey:
    id: str
    name: str
    scopes: frozenset[str]
    policy_ids: frozenset[str]
    expires_at: float | None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class RuntimeCache:
    def __init__(
        self,
        redis: Redis,
        sessions: async_sessionmaker[AsyncSession],
        refresh_seconds: float = 30.0,
    ) -> None:
        self._redis = redis
        self._sessions = sessions
        self._refresh_seconds = refresh_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._reload_lock = asyncio.Lock()

        self.policies: dict[str, CompiledPolicy] = {}
        self.policies_by_id: dict[str, CompiledPolicy] = {}
        self.api_keys: dict[str, CachedKey] = {}
        self.custom_providers: dict[str, dict[str, str]] = {}
        self.config = RuntimeConfig()
        self.loaded_at = 0.0
        self.reloads = 0

    async def load(self) -> None:
        async with self._reload_lock, self._sessions() as session:
            policy_rows = (await session.execute(select(Policy))).scalars().all()
            key_rows = (
                (await session.execute(select(ApiKey).where(ApiKey.revoked_at.is_(None)))).scalars().all()
            )
            setting = await session.get(Setting, RUNTIME_SETTING_KEY)
            provider_rows = (await session.execute(select(ProviderConfig))).scalars().all()

        policies: dict[str, CompiledPolicy] = {}
        for row in policy_rows:
            try:
                policies[row.name] = compile_policy(row)
            except (ValueError, KeyError, TypeError):
                logger.exception("skipping invalid policy %s", row.name)

        self.policies = policies
        self.policies_by_id = {policy.id: policy for policy in policies.values()}
        self.api_keys = {
            row.key_hash: CachedKey(
                id=row.id,
                name=row.name,
                scopes=frozenset(row.scopes or []),
                policy_ids=frozenset(row.policy_ids or []),
                expires_at=row.expires_at.timestamp() if row.expires_at else None,
            )
            for row in key_rows
        }
        self.custom_providers = {row.name: dict(row.header_map or {}) for row in provider_rows}
        try:
            self.config = RuntimeConfig.model_validate(setting.value) if setting else RuntimeConfig()
        except ValueError:
            logger.exception("stored runtime config is invalid; using defaults")
            self.config = RuntimeConfig()
        self.loaded_at = time.time()
        self.reloads += 1

    async def start(self) -> None:
        await self.load()
        self._tasks = [
            asyncio.create_task(self._listen(), name="cache-listen"),
            asyncio.create_task(self._refresh(), name="cache-refresh"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.load()
            except Exception:
                logger.exception("cache refresh failed; keeping previous snapshot")

    async def _listen(self) -> None:
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(keys.INVALIDATE_CHANNEL)
                while True:
                    # Poll with a timeout shorter than the socket timeout: a bare listen()
                    # raises TimeoutError whenever the channel is quiet for that long.
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is not None:
                        await self.load()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("invalidation listener failed; reconnecting")
                await asyncio.sleep(1)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]


async def publish_invalidation(redis: Redis) -> None:
    await redis.publish(keys.INVALIDATE_CHANNEL, "reload")

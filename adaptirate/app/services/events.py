"""Decision event log, buffered in memory and flushed to a Redis stream.

Logging must never slow or break a decision, so the buffer is bounded per second
and overflow is dropped and counted rather than queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections import Counter
from datetime import UTC, datetime

from redis.asyncio import Redis

from ..limiter import keys

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 0.25
# Postgres text columns cannot hold this character; one such subject would otherwise
# make every batch insert containing it fail.
NUL = chr(0)


class EventBuffer:
    def __init__(self, redis: Redis, *, per_second: int, stream_maxlen: int) -> None:
        self._redis = redis
        self._per_second = per_second
        self._stream_maxlen = stream_maxlen
        self._events: list[dict[str, str]] = []
        self._key_usage: Counter[str] = Counter()
        self._second = 0
        self._count_this_second = 0
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def record(
        self,
        *,
        policy_id: str,
        policy_name: str,
        subject: str,
        allowed: bool,
        reason: str,
        limit: int,
        remaining: int,
        cost: int,
        api_key_id: str | None,
        log_blocked: bool,
        sample_allowed: float,
    ) -> None:
        if api_key_id is not None:
            self._key_usage[api_key_id] += 1

        if allowed:
            if sample_allowed <= 0 or (sample_allowed < 1 and random.random() >= sample_allowed):  # noqa: S311
                return
        elif not log_blocked:
            return

        now = time.time()
        second = int(now)
        if second != self._second:
            self._second = second
            self._count_this_second = 0
        if self._count_this_second >= self._per_second:
            self.dropped += 1
            return
        self._count_this_second += 1
        self._events.append(
            {
                "ts": str(int(now * 1000)),
                "pid": policy_id,
                "pn": policy_name,
                "sub": subject.replace(NUL, "")[:300],
                "ok": "1" if allowed else "0",
                "rs": reason,
                "lim": str(limit),
                "rem": str(remaining),
                "cost": str(cost),
                "kid": api_key_id or "",
            }
        )

    async def flush(self) -> None:
        if not self._events and not self._key_usage:
            return
        events, self._events = self._events, []
        usage, self._key_usage = self._key_usage, Counter()
        pipe = self._redis.pipeline(transaction=False)
        for event in events:
            pipe.xadd(keys.EVENT_STREAM, event, maxlen=self._stream_maxlen, approximate=True)  # type: ignore[arg-type]
        if usage:
            now = datetime.now(UTC)
            usage_key = keys.key_usage_key(now.strftime("%Y%m%d"))
            for key_id, count in usage.items():
                pipe.hincrby(usage_key, key_id, count)
            pipe.expire(usage_key, 40 * 86400)
            pipe.hset(
                keys.key_last_used_key(), mapping={key_id: str(int(now.timestamp())) for key_id in usage}
            )
        try:
            await pipe.execute()
        except Exception:
            # Losing a batch of log lines is acceptable; blocking decisions is not.
            self.dropped += len(events)
            logger.warning("event flush failed; dropped %d events", len(events), exc_info=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await self.flush()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-flush")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

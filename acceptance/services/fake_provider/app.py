"""A tiny imitation of an LLM provider with a token-bucket request quota.

Every response carries x-ratelimit-limit-requests, x-ratelimit-remaining-requests and
x-ratelimit-reset-requests (seconds until the bucket is full). When the quota is
exhausted it answers 429 with Retry-After.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

QUOTA_PER_MINUTE = int(os.environ.get("QUOTA_PER_MINUTE", "60"))
SERVICE_LATENCY_SECONDS = 0.02

app = FastAPI(title="Acceptance fake LLM provider")


class Quota:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.tokens = float(per_minute)
        self.stamp = time.monotonic()
        self.blocked_until = 0.0
        self.served = 0
        self.rejected = 0

    def refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(float(self.per_minute), self.tokens + (now - self.stamp) * self.per_minute / 60)
        self.stamp = now

    def reset(self) -> None:
        self.tokens = float(self.per_minute)
        self.stamp = time.monotonic()
        self.blocked_until = 0.0
        self.served = 0
        self.rejected = 0

    def headers(self) -> dict[str, str]:
        return {
            "x-ratelimit-limit-requests": str(self.per_minute),
            "x-ratelimit-remaining-requests": str(int(self.tokens)),
            "x-ratelimit-reset-requests": f"{(self.per_minute - self.tokens) * 60 / self.per_minute:.1f}",
        }

    def reject(self, retry_after: int) -> JSONResponse:
        self.rejected += 1
        headers = {**self.headers(), "x-ratelimit-remaining-requests": "0", "retry-after": str(retry_after)}
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers=headers)


quota = Quota(QUOTA_PER_MINUTE)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/complete")
async def complete() -> JSONResponse:
    await asyncio.sleep(SERVICE_LATENCY_SECONDS)
    quota.refill()
    now = time.monotonic()
    if now < quota.blocked_until:
        return quota.reject(max(1, int(quota.blocked_until - now + 0.999)))
    if quota.tokens < 1:
        return quota.reject(max(1, int((1 - quota.tokens) * 60 / quota.per_minute + 0.999)))
    quota.tokens -= 1
    quota.served += 1
    return JSONResponse({"text": "ok"}, headers=quota.headers())


class Drain(BaseModel):
    penalty_seconds: float = Field(default=0.0, ge=0, le=300)


@app.post("/_drain")
async def drain(request: Drain) -> dict[str, Any]:
    """Exhaust the quota. For penalty_seconds the provider answers 429 even as tokens refill."""
    quota.refill()
    quota.tokens = 0.0
    quota.blocked_until = time.monotonic() + request.penalty_seconds
    return {"remaining": 0, "penalty_seconds": request.penalty_seconds}


@app.post("/_reset")
async def reset() -> dict[str, Any]:
    quota.reset()
    return {"remaining": quota.per_minute, "served": 0, "rejected": 0}


@app.get("/_stats")
async def stats() -> dict[str, int]:
    return {"served": quota.served, "rejected": quota.rejected}

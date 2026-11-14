"""A small store API protected by AdaptiRate.

What it shows:
  * One line of middleware limiting every request per user. The higher limit for
    premium users and the block on a banned client are rules on the server, not code.
  * The middleware reporting latency and errors, so the adaptive engine lowers the
    limit when this service slows down and restores it when it recovers.
  * A call to an upstream "LLM" provider, paced against that provider's quota.

Run with Docker:   docker compose --profile demo up --build
Run locally:       ADAPTIRATE_API_KEY=ar_... ADAPTIRATE_URL=http://localhost:8080 \
                   uvicorn app:app --port 8090
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from adaptirate import AsyncAdaptiveLimiter, RateLimitExceeded
from adaptirate.asgi import AdaptiRateMiddleware

API_POLICY = "demo-api"
LLM_POLICY = "demo-llm"
PROVIDER = "demo-llm-provider"

PRODUCTS = [
    {"id": 1, "name": "Mechanical keyboard", "price": 129.0},
    {"id": 2, "name": "USB-C dock", "price": 89.0},
    {"id": 3, "name": "27-inch monitor", "price": 349.0},
    {"id": 4, "name": "Noise-cancelling headphones", "price": 279.0},
]

# Reads ADAPTIRATE_API_KEY and ADAPTIRATE_URL. If AdaptiRate is unreachable, keep
# enforcing the last limit it reported instead of failing open or closed.
limiter = AsyncAdaptiveLimiter(fail_mode="local", timeout=2.0)


class Service:
    """Stands in for the real work a service does: fast until it is overloaded."""

    capacity = 24  # concurrent requests handled comfortably
    base_latency = 0.012

    def __init__(self) -> None:
        self.in_flight = 0
        self.extra_latency = 0.0
        self.error_rate = 0.0

    async def work(self) -> None:
        self.in_flight += 1
        backlog = max(0, self.in_flight - self.capacity)
        limiter.set_queue_depth(API_POLICY, backlog)
        try:
            # Past capacity, every extra concurrent request adds queueing delay, the way
            # a saturated database pool would.
            delay = self.base_latency + backlog * 0.02 + self.extra_latency
            await asyncio.sleep(delay * random.uniform(0.8, 1.25))  # noqa: S311
            if random.random() < self.error_rate:  # noqa: S311
                raise HTTPException(503, "dependency unavailable")
        finally:
            self.in_flight -= 1


class FakeProvider:
    """An upstream API with its own quota. Answers like a real LLM provider:
    x-ratelimit-* headers on every response, 429 and Retry-After when exhausted."""

    limit = 120  # requests per minute

    def __init__(self) -> None:
        self.tokens = float(self.limit)
        self.stamp = time.monotonic()

    def call(self) -> tuple[int, dict[str, str]]:
        now = time.monotonic()
        self.tokens = min(self.limit, self.tokens + (now - self.stamp) * self.limit / 60)
        self.stamp = now
        headers = {
            "x-ratelimit-limit-requests": str(self.limit),
            "x-ratelimit-reset-requests": f"{(self.limit - self.tokens) * 60 / self.limit:.1f}",
        }
        if self.tokens < 1:
            headers["x-ratelimit-remaining-requests"] = "0"
            headers["retry-after"] = str(max(1, round((1 - self.tokens) * 60 / self.limit)))
            return 429, headers
        self.tokens -= 1
        headers["x-ratelimit-remaining-requests"] = str(int(self.tokens))
        return 200, headers


service = Service()
provider = FakeProvider()


def identify(scope: Mapping[str, Any]) -> Mapping[str, Any]:
    """Who is calling. A real service takes this from its authentication layer; here
    the load generator says who it is with a header."""
    headers = {name.decode(): value.decode() for name, value in scope.get("headers", [])}
    client = scope.get("client")
    identity: dict[str, Any] = {
        "user": headers.get("x-user") or "anonymous",
        "ip": client[0] if client else "unknown",
    }
    if "x-role" in headers:
        identity["role"] = headers["x-role"]
    return identity


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await limiter.close()  # sends any pending reports


app = FastAPI(title="AdaptiRate demo store", lifespan=lifespan)

# The whole integration: every request is checked against the "demo-api" policy, and
# its latency and status are reported back.
app.add_middleware(
    AdaptiRateMiddleware,
    limiter=limiter,
    policy=API_POLICY,
    identify=identify,
    exclude=("/health", "/chaos", "/upstream"),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/products")
async def products() -> list[dict[str, Any]]:
    await service.work()
    return PRODUCTS


class Order(BaseModel):
    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=100)


@app.post("/orders", status_code=201)
async def create_order(order: Order) -> dict[str, Any]:
    await service.work()
    product = next((item for item in PRODUCTS if item["id"] == order.product_id), None)
    if product is None:
        raise HTTPException(404, "No such product")
    return {"order_id": random.randint(10_000, 99_999), "total": product["price"] * order.quantity}  # noqa: S311


@app.get("/recommend")
async def recommend() -> dict[str, Any]:
    """Calls the upstream provider. AdaptiRate paces these calls against the provider's
    remaining quota, shared by every worker, and pauses them when it answers 429."""
    try:
        await limiter.acquire(LLM_POLICY, key="store")
    except RateLimitExceeded as exceeded:
        raise HTTPException(
            429, "Recommendations are busy, try again shortly", headers=exceeded.decision.headers()
        ) from None
    async with limiter.track(LLM_POLICY):
        await asyncio.sleep(0.05)
        status, headers = provider.call()
    await limiter.report_provider(PROVIDER, status, headers)
    if status == 429:
        raise HTTPException(503, "Upstream provider is rate limiting us")
    return {"recommendation": random.choice(PRODUCTS)["name"]}  # noqa: S311


class Chaos(BaseModel):
    extra_latency_ms: float = Field(default=0, ge=0, le=5000)
    error_rate: float = Field(default=0, ge=0, le=1)


@app.post("/chaos")
async def chaos(settings: Chaos) -> dict[str, float]:
    """Make the service slow or flaky on demand, to watch the limiter adapt."""
    service.extra_latency = settings.extra_latency_ms / 1000
    service.error_rate = settings.error_rate
    return {"extra_latency_ms": settings.extra_latency_ms, "error_rate": settings.error_rate}


@app.get("/upstream")
async def upstream_state() -> dict[str, float]:
    return {"provider_tokens": round(provider.tokens, 1), "in_flight": service.in_flight}

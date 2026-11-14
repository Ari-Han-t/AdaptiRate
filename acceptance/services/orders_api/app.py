"""Orders API protected by AdaptiRate through the Python SDK's ASGI middleware.

Identity comes from the X-User and optional X-Role headers. The policy name is read
from the environment variable named by ORDERS_POLICY_VAR (default ORDERS_POLICY) so the acceptance runner can point the
same image at different policies. POST /_chaos makes the service slow or flaky.
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from adaptirate import AsyncAdaptiveLimiter
from adaptirate.asgi import AdaptiRateMiddleware

POLICY = os.environ[os.environ.get("ORDERS_POLICY_VAR", "ORDERS_POLICY")]
HOSTNAME = socket.gethostname()
BOOT_ID = uuid.uuid4().hex
STARTED_AT = time.time()
BASE_LATENCY_SECONDS = 0.010

# Reads ADAPTIRATE_API_KEY and ADAPTIRATE_URL. If AdaptiRate is unreachable, keep
# enforcing the last limit it reported instead of failing open or closed.
limiter = AsyncAdaptiveLimiter(fail_mode="local", timeout=2.0)


class Chaos:
    def __init__(self) -> None:
        self.extra_latency = 0.0
        self.error_rate = 0.0


chaos = Chaos()
ORDERS: list[dict[str, Any]] = [
    {"id": 1001, "item": "keyboard", "quantity": 1},
    {"id": 1002, "item": "monitor", "quantity": 2},
]


async def do_work() -> None:
    """Stands in for real work: a few milliseconds, plus whatever chaos adds."""
    await asyncio.sleep((BASE_LATENCY_SECONDS + chaos.extra_latency) * random.uniform(0.9, 1.1))  # noqa: S311
    if random.random() < chaos.error_rate:  # noqa: S311
        raise HTTPException(503, "dependency unavailable")


def identify(scope: Mapping[str, Any]) -> Mapping[str, Any]:
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
    await limiter.close()


app = FastAPI(title="Acceptance orders API", lifespan=lifespan)
app.add_middleware(
    AdaptiRateMiddleware,
    limiter=limiter,
    policy=POLICY,
    identify=identify,
    exclude=("/health", "/whoami", "/_chaos"),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/whoami")
async def whoami() -> dict[str, Any]:
    return {"hostname": HOSTNAME, "boot_id": BOOT_ID, "started_at": STARTED_AT, "policy": POLICY}


@app.get("/orders")
async def list_orders() -> JSONResponse:
    await do_work()
    return JSONResponse({"orders": ORDERS, "served_by": HOSTNAME}, headers={"X-Served-By": HOSTNAME})


class Order(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=100)


@app.post("/orders", status_code=201)
async def create_order(order: Order) -> dict[str, Any]:
    await do_work()
    return {"order_id": random.randint(10_000, 99_999), "item": order.item, "served_by": HOSTNAME}  # noqa: S311


class ChaosSettings(BaseModel):
    extra_latency_ms: float = Field(default=0, ge=0, le=5000)
    error_rate: float = Field(default=0, ge=0, le=1)


@app.post("/_chaos")
async def set_chaos(settings: ChaosSettings) -> dict[str, Any]:
    chaos.extra_latency = settings.extra_latency_ms / 1000
    chaos.error_rate = settings.error_rate
    return {"hostname": HOSTNAME, **settings.model_dump()}

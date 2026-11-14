"""Traffic generator for the demo store.

Simulates a mix of clients and cycles through phases, so the dashboard has a story to
tell: steady traffic, a burst, a slow dependency (the limiter adapts), and recovery.

    python load.py            # against http://localhost:8090
    TARGET=http://demo-app:8090 python load.py
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import Counter
from dataclasses import dataclass

import httpx

TARGET = os.environ.get("TARGET", "http://localhost:8090").rstrip("/")


@dataclass(frozen=True)
class Client:
    user: str
    rate: float  # requests per second in the steady phase
    role: str | None = None


CLIENTS = [
    Client("alice", 6),
    Client("bob", 9),
    Client("carol", 4),
    Client("dave", 12),
    Client("erin", 3),
    Client("acme-corp", 35, role="premium"),
    Client("greedy-app", 45),  # keeps exceeding its 20/s
    Client("scraper-bot", 5),  # blocked outright by a deny rule
]


@dataclass(frozen=True)
class Phase:
    name: str
    seconds: int
    rate_factor: float = 1.0
    extra_latency_ms: float = 0.0
    error_rate: float = 0.0


PHASES = [
    Phase("steady", 45),
    Phase("traffic spike", 25, rate_factor=3.0),
    Phase("steady", 20),
    Phase("slow dependency", 40, extra_latency_ms=450),
    Phase("recovery", 45),
    Phase("flaky dependency", 30, error_rate=0.35),
    Phase("recovery", 40),
]

outcomes: Counter[str] = Counter()


async def one_request(http: httpx.AsyncClient, client: Client) -> None:
    headers = {"x-user": client.user}
    if client.role:
        headers["x-role"] = client.role
    roll = random.random()  # noqa: S311
    try:
        if roll < 0.7:
            response = await http.get("/products", headers=headers)
        elif roll < 0.9:
            response = await http.post(
                "/orders", headers=headers, json={"product_id": random.randint(1, 4), "quantity": 1}  # noqa: S311
            )
        else:
            response = await http.get("/recommend", headers=headers)
        outcomes[str(response.status_code)] += 1
    except httpx.HTTPError:
        outcomes["error"] += 1


async def run_client(http: httpx.AsyncClient, client: Client, factor: float, until: float) -> None:
    interval = 1 / (client.rate * factor)
    tasks: set[asyncio.Task[None]] = set()
    next_at = time.monotonic() + random.random() * interval  # noqa: S311
    while time.monotonic() < until:
        delay = next_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        # Open loop: send on schedule whether or not earlier requests have returned,
        # which is how real clients behave and what makes overload visible.
        if len(tasks) < 400:
            task = asyncio.create_task(one_request(http, client))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        next_at += interval * random.uniform(0.6, 1.4)  # noqa: S311
    if tasks:
        await asyncio.wait(tasks, timeout=10)


async def report(until: float, phase: str) -> None:
    while time.monotonic() < until:
        await asyncio.sleep(5)
        snapshot = dict(outcomes)
        outcomes.clear()
        total = sum(snapshot.values()) or 1
        ok = snapshot.get("200", 0) + snapshot.get("201", 0)
        limited = snapshot.get("429", 0)
        failed = total - ok - limited
        print(
            f"[{phase:<16}] {total / 5:6.0f} req/s   ok {ok / total:5.1%}   "
            f"rate-limited {limited / total:5.1%}   failed {failed / total:5.1%}",
            flush=True,
        )


async def main() -> None:
    limits = httpx.Limits(max_connections=600, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=TARGET, timeout=15, limits=limits) as http:
        for _ in range(60):
            try:
                if (await http.get("/health")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise SystemExit(f"demo app at {TARGET} never became healthy")

        print(f"sending traffic to {TARGET}", flush=True)
        while True:
            for phase in PHASES:
                await http.post(
                    "/chaos", json={"extra_latency_ms": phase.extra_latency_ms, "error_rate": phase.error_rate}
                )
                until = time.monotonic() + phase.seconds
                await asyncio.gather(
                    report(until, phase.name),
                    *(run_client(http, client, phase.rate_factor, until) for client in CLIENTS),
                )


if __name__ == "__main__":
    asyncio.run(main())

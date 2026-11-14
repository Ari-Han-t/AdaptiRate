"""Throughput benchmark for AdaptiRate.

Measures, against a running server and for a fixed duration:

  batch     POST /evaluate/batch with 100 checks per call (Python client)
  leased    SDK leasing: tokens fetched in blocks, decisions made in process

Single-request throughput is measured with ApacheBench instead (see README.md): a Python
HTTP client saturates long before the server does, so it would measure the client.

    ADAPTIRATE_URL=http://localhost:8000 ADAPTIRATE_ADMIN_EMAIL=... ADAPTIRATE_ADMIN_PASSWORD=... \
        python bench.py --seconds 15 --concurrency 64

Prints a JSON summary and writes it to results/latest.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk-python"))
from adaptirate import AsyncAdaptiveLimiter  # noqa: E402

URL = os.environ.get("ADAPTIRATE_URL", "http://localhost:8000").rstrip("/")
USERS = 1000  # spread load over many buckets, as real traffic would be
LIMIT = 10_000_000  # high enough that the benchmark measures overhead, not blocking


async def setup(http: httpx.AsyncClient) -> tuple[str, str, str]:
    login = await http.post(
        "/auth/login",
        json={"email": os.environ["ADAPTIRATE_ADMIN_EMAIL"], "password": os.environ["ADAPTIRATE_ADMIN_PASSWORD"]},
    )
    login.raise_for_status()
    http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    name = f"bench-{uuid.uuid4().hex[:8]}"
    policy = await http.post(
        "/policies",
        json={"name": name, "limit": LIMIT, "window_seconds": 1, "algorithm": "token_bucket", "scope": "user",
              "adaptive": False},
    )
    policy.raise_for_status()
    key = await http.post("/keys", json={"name": f"{name}-key", "scopes": ["evaluate"]})
    key.raise_for_status()
    return name, policy.json()["id"], key.json()["secret"]


async def run_batch(name: str, secret: str, seconds: float, concurrency: int, size: int = 100) -> dict[str, float]:
    decisions = 0
    deadline = time.perf_counter() + seconds
    async with httpx.AsyncClient(base_url=f"{URL}/api/v1", headers={"X-API-Key": secret}, timeout=10) as http:

        async def worker(index: int) -> None:
            nonlocal decisions
            offset = 0
            while time.perf_counter() < deadline:
                checks = [{"policy": name, "user": f"u{(index * 7919 + offset + i) % USERS}"} for i in range(size)]
                response = await http.post("/evaluate/batch", json={"checks": checks})
                response.raise_for_status()
                decisions += size
                offset += size

        started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(max(1, concurrency // 4))))
        elapsed = time.perf_counter() - started
    return {"decisions_per_second": round(decisions / elapsed), "batch_size": size}


async def run_leased(name: str, secret: str, seconds: float, lease_size: int = 1000) -> dict[str, float]:
    decisions = 0
    deadline = time.perf_counter() + seconds
    async with AsyncAdaptiveLimiter(api_key=secret, base_url=URL, lease_size=lease_size, report_interval=0) as sdk:

        async def worker(index: int) -> None:
            nonlocal decisions
            while time.perf_counter() < deadline:
                # A hot path: the same caller many times in a row, as in a busy worker.
                for _ in range(256):
                    decision = await sdk.check(name, user=f"hot{index}")
                    if not decision.allowed:
                        raise RuntimeError(f"unexpected denial: {decision}")
                    decisions += 1
                await asyncio.sleep(0)

        started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(8)))
        elapsed = time.perf_counter() - started
    return {"decisions_per_second": round(decisions / elapsed), "lease_size": lease_size}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--concurrency", type=int, default=64)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=f"{URL}/api/v1", timeout=10) as admin:
        name, policy_id, secret = await setup(admin)
        try:
            results = {
                "batch": await run_batch(name, secret, args.seconds, args.concurrency),
                "leased": await run_leased(name, secret, args.seconds),
            }
        finally:
            await admin.delete(f"/policies/{policy_id}")

    summary = {
        "results": results,
        "seconds": args.seconds,
        "concurrency": args.concurrency,
        "machine": {"platform": platform.platform(), "cpus": os.cpu_count(), "python": platform.python_version()},
    }
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

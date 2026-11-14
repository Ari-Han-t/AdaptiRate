"""One-time setup for the demo: creates the policies the store uses and an API key.

Prints the API key on stdout and nothing else, so a shell can capture it:

    export ADAPTIRATE_API_KEY=$(python bootstrap.py)

Needs ADAPTIRATE_URL plus an admin's ADAPTIRATE_ADMIN_EMAIL / ADAPTIRATE_ADMIN_PASSWORD.
In a real deployment you would create the policies and key once in the dashboard.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

API_POLICY = "demo-api"
LLM_POLICY = "demo-llm"
PROVIDER = "demo-llm-provider"

POLICIES = [
    {
        "name": API_POLICY,
        "description": "Store API: 20 requests per second per user, adapts to service latency",
        "algorithm": "token_bucket",
        "limit": 20,
        "window_seconds": 1,
        "burst": 10,
        "scope": "user",
        "adaptive": True,
        "min_limit": 2,
        "target_latency_ms": 120,
        "priority": 60,
        "rules": [
            {"attribute": "user", "values": ["scraper-bot"], "action": "deny", "priority": 100},
            {"attribute": "role", "values": ["premium"], "action": "limit", "limit": 60, "burst": 30, "priority": 10},
        ],
    },
    {
        "name": LLM_POLICY,
        "description": "Calls to the upstream LLM provider, paced against its quota",
        "algorithm": "sliding_window",
        "limit": 240,
        "window_seconds": 60,
        "scope": "custom",
        "adaptive": False,
        "provider": PROVIDER,
    },
]

PROVIDER_HEADERS = {
    "remaining_requests": "x-ratelimit-remaining-requests",
    "limit_requests": "x-ratelimit-limit-requests",
    "reset_requests": "x-ratelimit-reset-requests",
    "reset_format": "seconds",
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main() -> None:
    url = os.environ.get("ADAPTIRATE_URL", "http://localhost:8080").rstrip("/")
    email = os.environ["ADAPTIRATE_ADMIN_EMAIL"]
    password = os.environ["ADAPTIRATE_ADMIN_PASSWORD"]

    with httpx.Client(base_url=f"{url}/api/v1", timeout=10) as http:
        for attempt in range(30):
            try:
                login = http.post("/auth/login", json={"email": email, "password": password})
                login.raise_for_status()
                break
            except httpx.HTTPError as error:
                if attempt == 29:
                    raise SystemExit(f"could not sign in to AdaptiRate at {url}: {error}") from error
                time.sleep(1)
        http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        http.put(f"/providers/{PROVIDER}", json=PROVIDER_HEADERS).raise_for_status()

        existing = {policy["name"]: policy for policy in http.get("/policies").json()}
        for policy in POLICIES:
            if policy["name"] in existing:
                changes = {key: value for key, value in policy.items() if key != "name"}
                http.patch(f"/policies/{existing[policy['name']]['id']}", json=changes).raise_for_status()
                log(f"updated policy {policy['name']}")
            else:
                http.post("/policies", json=policy).raise_for_status()
                log(f"created policy {policy['name']}")

        # Keys cannot be read back, so each run issues a fresh one and retires the previous.
        for key in http.get("/keys").json():
            if key["name"] == "demo-store" and key["revoked_at"] is None:
                http.post(f"/keys/{key['id']}/revoke").raise_for_status()
        created = http.post("/keys", json={"name": "demo-store", "scopes": ["evaluate", "report"]})
        created.raise_for_status()
        log("issued API key demo-store")
        print(created.json()["secret"])


if __name__ == "__main__":
    main()

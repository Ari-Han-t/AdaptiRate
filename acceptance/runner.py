"""AdaptiRate acceptance runner.

Runs inside a container on the compose network and drives real HTTP services:
orders-api (3 replicas, Python SDK), orders-adaptive (3 replicas), llm-gateway
(Node SDK) and fake-provider. Commands, in the order run.sh uses them:

    setup      create policies, an API key and a provider header map; write .run/env
    wait       block until every service answers
    run        scenarios S1 to S6, then prepare S7
    outage     S7 part 1 (run.sh has paused the AdaptiRate API)
    recovery   S7 part 2 (run.sh has unpaused it)
    report     print the summary, write REPORT.md and report.json
    teardown   delete everything setup created and restore runtime configuration

Admin credentials come from ADAPTIRATE_ADMIN_EMAIL / ADAPTIRATE_ADMIN_PASSWORD and are
never printed. The API key is written only to .run/env, which is git-ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run"
ENV_FILE = RUN_DIR / "env"
STATE_FILE = RUN_DIR / "state.json"
RESULTS_FILE = RUN_DIR / "results.json"
REPORT_JSON = RUN_DIR / "report.json"
REPORT_MD = ROOT / "REPORT.md"

AR_URL = os.environ.get("ADAPTIRATE_URL", "http://api:8000").rstrip("/")
ORDERS_HOST = "orders-api"
ADAPTIVE_HOST = "orders-adaptive"
SERVICE_PORT = 8000
GATEWAY_URL = "http://llm-gateway:3000"
PROVIDER_URL = "http://fake-provider:9000"
EXPECTED_REPLICAS = 3

PROVIDER_HEADER_MAP = {
    "remaining_requests": "x-ratelimit-remaining-requests",
    "limit_requests": "x-ratelimit-limit-requests",
    "reset_requests": "x-ratelimit-reset-requests",
    "reset_format": "seconds",
}

SCENARIO_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
TITLES = {
    "S1": ("Exact shared limit across replicas", "Three replicas of a service share one exact per-user limit."),
    "S2": (
        "Isolation and rules",
        "One user cannot use up another's budget, and rules change limits per user or role without code.",
    ),
    "S3": (
        "Adaptive limit under latency",
        "When the service gets slow the limit falls on its own, and it recovers when the service does.",
    ),
    "S4": (
        "Provider pacing, before and after",
        "Calls to a rate-limited upstream API are paced so the gateway collects far fewer 429s.",
    ),
    "S5": (
        "Retry-After pause",
        "After an upstream 429 every caller is told to wait, and the pause lifts on schedule or on demand.",
    ),
    "S6": (
        "Live reconfiguration",
        "Changing a policy's limit and algorithm takes effect on the next burst with no restart.",
    ),
    "S7": (
        "Outage behaviour (fail_mode=local)",
        "If AdaptiRate goes down the service keeps answering and keeps limiting, then resumes exact limits.",
    ),
}


# ---------------------------------------------------------------------------
# Output helpers


def out(message: str = "") -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Scenario bookkeeping


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Scenario:
    sid: str
    title: str
    claim: str
    checks: list[Check] = field(default_factory=list)
    measured: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        out(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
        return ok

    def measure(self, text: str) -> None:
        self.measured.append(text)

    def note(self, text: str) -> None:
        self.notes.append(text)
        out(f"  {text}")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.sid,
            "title": self.title,
            "claim": self.claim,
            "passed": self.passed,
            "measured": "; ".join(self.measured),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "notes": self.notes,
            "seconds": round(self.seconds, 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        scenario = cls(data["id"], data["title"], data["claim"])
        scenario.checks = [Check(c["name"], c["ok"], c["detail"]) for c in data["checks"]]
        scenario.measured = [data["measured"]] if data["measured"] else []
        scenario.notes = list(data["notes"])
        scenario.seconds = float(data["seconds"])
        return scenario

    def finish(self) -> None:
        out(f"{self.sid} {'PASS' if self.passed else 'FAIL'}  {'; '.join(self.measured)}")
        out()


def begin(sid: str) -> Scenario:
    title, claim = TITLES[sid]
    out(f"=== {sid} {title} ===")
    out(f"Claim: {claim}")
    return Scenario(sid, title, claim)


# ---------------------------------------------------------------------------
# State persisted between invocations


@dataclass
class State:
    suffix: str
    orders_policy: str = ""
    adaptive_policy: str = ""
    llm_policy: str = ""
    provider: str = ""
    banned_user: str = ""
    policy_ids: dict[str, str] = field(default_factory=dict)
    key_id: str = ""
    original_config: dict[str, Any] | None = None
    config_changed: bool = False
    boots: dict[str, str] = field(default_factory=dict)

    def save(self) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls) -> State:
        if not STATE_FILE.exists():
            raise SystemExit("no .run/state.json: run `runner.py setup` first")
        return cls(**json.loads(STATE_FILE.read_text()))


def read_api_key() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("ADAPTIRATE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("ADAPTIRATE_API_KEY missing from .run/env")


# ---------------------------------------------------------------------------
# Admin API client


class Admin:
    """Management API client. Signs in once and signs in again if the token expires."""

    def __init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=f"{AR_URL}/api/v1", timeout=httpx.Timeout(15.0))
        self._token = ""

    async def login(self, attempts: int = 30) -> None:
        email = os.environ.get("ADAPTIRATE_ADMIN_EMAIL", "")
        password = os.environ.get("ADAPTIRATE_ADMIN_PASSWORD", "")
        if not email or not password:
            raise SystemExit("ADAPTIRATE_ADMIN_EMAIL and ADAPTIRATE_ADMIN_PASSWORD must be set")
        last = ""
        for _ in range(attempts):
            try:
                response = await self.http.post("/auth/login", json={"email": email, "password": password})
                if response.status_code == 200:
                    self._token = response.json()["access_token"]
                    return
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as error:
                last = type(error).__name__
            await asyncio.sleep(1)
        raise SystemExit(f"could not sign in to AdaptiRate at {AR_URL}: {last}")

    async def req(
        self, method: str, path: str, *, json_body: Any = None, allow: tuple[int, ...] = ()
    ) -> Any:
        for attempt in range(2):
            response = await self.http.request(
                method, path, json=json_body, headers={"Authorization": f"Bearer {self._token}"}
            )
            if response.status_code == 401 and attempt == 0:
                await self.login(attempts=3)
                continue
            break
        if response.status_code in allow:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    async def close(self) -> None:
        await self.http.aclose()


# ---------------------------------------------------------------------------
# Setup and teardown


def env_file_text(state: State, api_key: str) -> str:
    return (
        "\n".join(
            [
                f"ADAPTIRATE_API_KEY={api_key}",
                f"ACC_ORDERS_POLICY={state.orders_policy}",
                f"ACC_ADAPTIVE_POLICY={state.adaptive_policy}",
                f"LLM_POLICY={state.llm_policy}",
                f"PROVIDER_NAME={state.provider}",
            ]
        )
        + "\n"
    )


async def cmd_setup() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(3)
    state = State(
        suffix=suffix,
        orders_policy=f"acc-orders-{suffix}",
        adaptive_policy=f"acc-adaptive-{suffix}",
        llm_policy=f"acc-llm-{suffix}",
        provider=f"acc-provider-{suffix}",
        banned_user=f"banned-{suffix}",
    )
    state.save()  # so teardown can clean up even if setup fails part way
    admin = Admin()
    try:
        await admin.login()

        state.original_config = await admin.req("GET", "/config")
        state.save()
        changed = json.loads(json.dumps(state.original_config))
        changed["adaptive"]["use_host_signals"] = False
        await admin.req("PUT", "/config", json_body=changed)
        state.config_changed = True
        state.save()
        out("runtime config: adaptive.use_host_signals=false for the run (restored in teardown)")

        await admin.req("PUT", f"/providers/{state.provider}", json_body=PROVIDER_HEADER_MAP)
        out(f"provider header map registered: {state.provider}")

        policies = [
            (
                "orders",
                {
                    "name": state.orders_policy,
                    "description": "Acceptance: orders API, per-user fixed window",
                    "algorithm": "fixed_window",
                    "limit": 50,
                    "window_seconds": 60,
                    "scope": "user",
                    "adaptive": False,
                    "rules": [
                        {"attribute": "user", "values": [state.banned_user], "action": "deny", "priority": 100},
                        {
                            "attribute": "role",
                            "values": ["premium"],
                            "action": "limit",
                            "limit": 120,
                            "window_seconds": 60,
                            "priority": 10,
                        },
                    ],
                },
            ),
            (
                "adaptive",
                {
                    "name": state.adaptive_policy,
                    "description": "Acceptance: adaptive token bucket",
                    "algorithm": "token_bucket",
                    "limit": 40,
                    "window_seconds": 1,
                    "scope": "user",
                    "adaptive": True,
                    "min_limit": 4,
                    "target_latency_ms": 100,
                },
            ),
            (
                "llm",
                {
                    "name": state.llm_policy,
                    "description": "Acceptance: upstream LLM provider, paced against its quota",
                    "algorithm": "sliding_window",
                    "limit": 600,
                    "window_seconds": 60,
                    "scope": "custom",
                    "adaptive": False,
                    "provider": state.provider,
                },
            ),
        ]
        for label, body in policies:
            created = await admin.req("POST", "/policies", json_body=body)
            state.policy_ids[label] = created["id"]
            state.save()
            out(f"policy created: {body['name']}")

        key = await admin.req(
            "POST", "/keys", json_body={"name": f"acceptance-{suffix}", "scopes": ["evaluate", "report"]}
        )
        state.key_id = key["id"]
        state.save()
        ENV_FILE.write_text(env_file_text(state, key["secret"]))
        ENV_FILE.chmod(0o600)
        out("API key issued (written to acceptance/.run/env only)")
        RESULTS_FILE.write_text("[]")
        return 0
    except (RuntimeError, httpx.HTTPError) as error:
        out(f"setup failed: {error}")
        return 1
    finally:
        await admin.close()


async def cmd_teardown() -> int:
    if not STATE_FILE.exists():
        out("nothing to tear down")
        return 0
    state = State.load()
    admin = Admin()
    failures: list[str] = []

    async def attempt(label: str, call: Awaitable[Any]) -> None:
        try:
            await call
            out(f"teardown: {label}")
        except (RuntimeError, httpx.HTTPError) as error:
            failures.append(f"{label}: {error}")
            out(f"teardown FAILED: {label}: {error}")

    try:
        await admin.login(attempts=5)
        for label, policy_id in state.policy_ids.items():
            await attempt(f"delete policy {label}", admin.req("DELETE", f"/policies/{policy_id}", allow=(404,)))
        if state.key_id:
            await attempt("revoke API key", admin.req("POST", f"/keys/{state.key_id}/revoke", allow=(404, 409)))
        if state.provider:
            await attempt("delete provider", admin.req("DELETE", f"/providers/{state.provider}", allow=(404,)))
        if state.config_changed and state.original_config is not None:
            await attempt("restore runtime config", admin.req("PUT", "/config", json_body=state.original_config))
    except SystemExit as error:
        failures.append(str(error))
        out(f"teardown could not sign in: {error}")
    finally:
        await admin.close()
        ENV_FILE.write_text("")  # drop the API key from disk
    if not failures:
        STATE_FILE.unlink(missing_ok=True)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Traffic helpers


@dataclass(frozen=True)
class Reply:
    status: int  # 0 when the request failed at the transport level
    headers: dict[str, str]
    body: Any
    elapsed: float
    error: str = ""

    @property
    def served_by(self) -> str:
        return self.headers.get("x-served-by", "")

    @property
    def reason(self) -> str:
        return str(self.body.get("reason", "")) if isinstance(self.body, dict) else ""


async def call(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
) -> Reply:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as error:
        return Reply(0, {}, None, time.perf_counter() - started, type(error).__name__)
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    return Reply(
        response.status_code,
        {name.lower(): value for name, value in response.headers.items()},
        body,
        time.perf_counter() - started,
    )


async def resolve(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, SERVICE_PORT, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def user_headers(user: str, role: str | None = None) -> dict[str, str]:
    headers = {"X-User": user}
    if role:
        headers["X-Role"] = role
    return headers


async def burst(
    client: httpx.AsyncClient,
    ips: list[str],
    count: int,
    user: str,
    *,
    role: str | None = None,
    path: str = "/orders",
) -> list[Reply]:
    """Fire `count` GETs at once, rotating across replica addresses."""
    headers = user_headers(user, role)
    tasks = [
        call(client, "GET", f"http://{ips[index % len(ips)]}:{SERVICE_PORT}{path}", headers=headers)
        for index in range(count)
    ]
    return list(await asyncio.gather(*tasks))


def tally(replies: list[Reply]) -> Counter[int]:
    return Counter(reply.status for reply in replies)


async def align_window(window_seconds: int = 60, margin: float = 12.0) -> None:
    """Fixed windows are aligned to the clock; start a burst early in one so it cannot straddle two."""
    position = time.time() % window_seconds
    if position > window_seconds - margin:
        await asyncio.sleep(window_seconds - position + 0.5)


async def eval_direct(client: httpx.AsyncClient, api_key: str, policy: str, key: str) -> dict[str, Any]:
    response = await client.post(
        f"{AR_URL}/api/v1/evaluate", headers={"X-API-Key": api_key}, json={"policy": policy, "key": key}
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


@dataclass
class World:
    admin: Admin
    client: httpx.AsyncClient
    state: State
    api_key: str


# ---------------------------------------------------------------------------
# S1 to S7


async def s1_exact_limit(world: World) -> Scenario:
    sc = begin("S1")
    ips = await resolve(ORDERS_HOST)
    sc.check("service resolves to all replicas", len(ips) == EXPECTED_REPLICAS, f"{len(ips)} addresses")
    for ip in ips:
        who = await call(world.client, "GET", f"http://{ip}:{SERVICE_PORT}/whoami")
        if isinstance(who.body, dict):
            world.state.boots[ip] = who.body.get("boot_id", "")
    world.state.save()

    await align_window()
    user = f"s1-{world.state.suffix}"
    replies = await burst(world.client, ips, 300, user)
    counts = tally(replies)
    served = Counter(reply.served_by for reply in replies if reply.status == 200)
    sc.check("exactly 50 requests returned 200", counts[200] == 50, f"got {counts[200]}")
    sc.check("exactly 250 requests returned 429", counts[429] == 250, f"got {counts[429]}")
    sc.check("no other outcome", sum(counts.values()) - counts[200] - counts[429] == 0, f"statuses {dict(counts)}")
    sc.check("at least 2 distinct replicas served 200s", len(served) >= 2, f"{len(served)} replicas: {sorted(served.values())}")
    sc.measure(f"300 concurrent -> 200={counts[200]} 429={counts[429]}, served by {len(served)} replicas {sorted(served.values())}")
    return sc


async def s2_isolation_rules(world: World) -> Scenario:
    sc = begin("S2")
    ips = await resolve(ORDERS_HOST)
    sfx = world.state.suffix
    await align_window()

    greedy = await burst(world.client, ips, 80, f"s2-greedy-{sfx}")
    g = tally(greedy)
    sc.check("greedy user limited to exactly 50", g[200] == 50 and g[429] == 30, f"200={g[200]} 429={g[429]}")
    polite = await burst(world.client, ips, 20, f"s2-polite-{sfx}")
    p = tally(polite)
    sc.check("a different user is unaffected (20 of 20 allowed)", p[200] == 20, f"200={p[200]} 429={p[429]}")
    again = await burst(world.client, ips, 5, f"s2-greedy-{sfx}")
    sc.check("greedy user is still blocked afterwards", tally(again)[429] == 5, f"statuses {dict(tally(again))}")

    premium = await burst(world.client, ips, 150, f"s2-premium-{sfx}", role="premium")
    pr = tally(premium)
    sc.check("role=premium rule gives exactly 120", pr[200] == 120 and pr[429] == 30, f"200={pr[200]} 429={pr[429]}")

    banned = await burst(world.client, ips, 6, world.state.banned_user)
    reasons = {reply.reason for reply in banned}
    sc.check(
        "deny rule blocks the user with reason rule_deny",
        tally(banned)[429] == 6 and reasons == {"rule_deny"},
        f"statuses {dict(tally(banned))} reasons {sorted(reasons)}",
    )

    rejected = next((reply for reply in greedy if reply.status == 429), None)
    headers = rejected.headers if rejected else {}
    retry_after = headers.get("retry-after", "")
    sc.check("429 carries Retry-After", retry_after.isdigit() and int(retry_after) >= 1, f"Retry-After={retry_after!r}")
    sc.check(
        "429 carries X-RateLimit-Limit/Remaining/Reset",
        headers.get("x-ratelimit-limit") == "50"
        and headers.get("x-ratelimit-remaining") == "0"
        and "x-ratelimit-reset" in headers,
        f"limit={headers.get('x-ratelimit-limit')} remaining={headers.get('x-ratelimit-remaining')} reset={headers.get('x-ratelimit-reset')}",
    )
    sc.measure(
        f"default user 50/80, other user 20/20, premium 120/150, banned 0/6 (rule_deny), "
        f"Retry-After={retry_after}s"
    )
    return sc


@dataclass
class Traffic:
    """Open-loop steady traffic from several users to a set of replica addresses."""

    client: httpx.AsyncClient
    ips: list[str]
    users: list[str]
    rate_per_user: float
    samples: list[tuple[float, int, float]] = field(default_factory=list)  # (time, status, latency)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def _one(self, user: str, ip: str) -> None:
        reply = await call(self.client, "GET", f"http://{ip}:{SERVICE_PORT}/orders", headers=user_headers(user))
        self.samples.append((time.monotonic(), reply.status, reply.elapsed))

    async def _user_loop(self, user: str, stop: asyncio.Event, offset: int) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.rate_per_user
        next_at = loop.time() + (offset % 10) * interval / 10
        index = offset
        while not stop.is_set():
            task = asyncio.create_task(self._one(user, self.ips[index % len(self.ips)]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            index += 1
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - loop.time()))

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(self._user_loop(user, stop, i) for i, user in enumerate(self.users)))
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def latency_p50_ms(self, since: float, until: float) -> float:
        values = [lat for stamp, status, lat in self.samples if since <= stamp < until and status == 200]
        return statistics.median(values) * 1000 if values else 0.0


async def effective_limit(world: World, policy_key: str) -> int:
    policy = await world.admin.req("GET", f"/policies/{world.state.policy_ids[policy_key]}")
    return int(policy["effective_limit"])


async def set_chaos(client: httpx.AsyncClient, ips: list[str], extra_latency_ms: float) -> bool:
    replies = await asyncio.gather(
        *(
            call(client, "POST", f"http://{ip}:{SERVICE_PORT}/_chaos", json_body={"extra_latency_ms": extra_latency_ms})
            for ip in ips
        )
    )
    return all(reply.status == 200 for reply in replies)


async def s3_adaptive(world: World) -> Scenario:
    sc = begin("S3")
    ips = await resolve(ADAPTIVE_HOST)
    users = [f"s3-user{i}-{world.state.suffix}" for i in range(6)]
    traffic = Traffic(world.client, ips, users, rate_per_user=8.0)
    stop = asyncio.Event()
    runner = asyncio.create_task(traffic.run(stop))
    timeline: list[tuple[int, int]] = []
    started = time.monotonic()

    async def sample() -> int:
        value = await effective_limit(world, "adaptive")
        timeline.append((round(time.monotonic() - started), value))
        return value

    try:
        for _ in range(10):
            await asyncio.sleep(1)
            await sample()
        baseline = int(statistics.median([value for _, value in timeline[-5:]]))
        sc.check("baseline limit is near the configured 40 under healthy traffic", baseline >= 36, f"baseline={baseline}")
        chaos_start = time.monotonic()
        sc.check("chaos (+600 ms) applied to all replicas", await set_chaos(world.client, ips, 600), f"{len(ips)} replicas")

        cut_after: float | None = None
        deadline = chaos_start + 25
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            value = await sample()
            if value <= baseline * 0.5:
                cut_after = time.monotonic() - chaos_start
                break
        sc.check(
            "limit fell to <= 50% of baseline within 25 s",
            cut_after is not None,
            f"{'cut after %.0f s' % cut_after if cut_after is not None else 'did not fall'}, now {timeline[-1][1]}",
        )
        for _ in range(3):  # hold the slowdown briefly so the dip is visible
            await asyncio.sleep(1)
            await sample()
        low = min(value for _, value in timeline)
        chaos_end = time.monotonic()
        sc.check("chaos cleared on all replicas", await set_chaos(world.client, ips, 0), f"{len(ips)} replicas")

        recovered_after: float | None = None
        deadline = chaos_end + 45
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            value = await sample()
            if value >= baseline * 0.9:
                recovered_after = time.monotonic() - chaos_end
                break
        sc.check(
            "limit returned to >= 90% of baseline within 45 s",
            recovered_after is not None,
            f"{'recovered after %.0f s' % recovered_after if recovered_after is not None else 'did not recover'}, now {timeline[-1][1]}",
        )
        p50_before = traffic.latency_p50_ms(started, chaos_start)
        p50_during = traffic.latency_p50_ms(chaos_start, chaos_end)
        sc.measure(
            f"limit {baseline} -> {low} (cut in {cut_after if cut_after is None else round(cut_after)} s) -> "
            f"{timeline[-1][1]} (recovered in {recovered_after if recovered_after is None else round(recovered_after)} s); "
            f"client p50 latency {p50_before:.0f} ms -> {p50_during:.0f} ms during slowdown"
        )
    finally:
        await set_chaos(world.client, ips, 0)
        stop.set()
        await runner
    sc.note("limit timeline (seconds:limit) " + " ".join(f"{t}:{v}" for t, v in timeline))
    return sc


async def reset_provider_state(world: World) -> None:
    provider = world.state.provider
    await world.admin.req("DELETE", f"/providers/{provider}", allow=(404,))
    await world.admin.req("PUT", f"/providers/{provider}", json_body=PROVIDER_HEADER_MAP)
    await world.admin.req("POST", f"/policies/{world.state.policy_ids['llm']}/reset")
    response = await world.client.post(f"{PROVIDER_URL}/_reset")
    response.raise_for_status()


async def set_gateway_mode(client: httpx.AsyncClient, use_adaptirate: bool) -> None:
    response = await client.post(f"{GATEWAY_URL}/_mode", json={"use_adaptirate": use_adaptirate})
    response.raise_for_status()


async def gateway_stats(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(f"{GATEWAY_URL}/stats")
    response.raise_for_status()
    stats: dict[str, Any] = response.json()
    return stats


async def provider_stats(client: httpx.AsyncClient) -> dict[str, int]:
    response = await client.get(f"{PROVIDER_URL}/_stats")
    response.raise_for_status()
    stats: dict[str, int] = response.json()
    return stats


async def offered_load(client: httpx.AsyncClient, rate: float, seconds: float) -> Counter[int]:
    """Send `rate` requests per second to the gateway for `seconds`, open loop."""
    statuses: Counter[int] = Counter()
    loop = asyncio.get_running_loop()
    tasks: list[asyncio.Task[Reply]] = []
    start = loop.time()
    total = int(rate * seconds)
    for index in range(total):
        tasks.append(asyncio.create_task(call(client, "POST", f"{GATEWAY_URL}/complete")))
        await asyncio.sleep(max(0.0, start + (index + 1) / rate - loop.time()))
    for reply in await asyncio.gather(*tasks):
        statuses[reply.status] += 1
    return statuses


async def gateway_run(world: World, use_adaptirate: bool) -> dict[str, Any]:
    client = world.client
    await set_gateway_mode(client, use_adaptirate)
    await reset_provider_state(world)
    await call(client, "POST", f"{GATEWAY_URL}/complete")  # first call: provider quota becomes known
    if use_adaptirate:
        await asyncio.sleep(2.5)  # let the adaptive worker tick pick it up
    reset = await client.post(f"{GATEWAY_URL}/_stats/reset")
    reset.raise_for_status()
    before = await provider_stats(client)
    statuses = await offered_load(client, rate=4.0, seconds=30.0)
    await asyncio.sleep(1)
    gateway = await gateway_stats(client)
    provider = await provider_stats(client)
    return {
        "statuses": statuses,
        "gateway": gateway,
        "provider_served": provider["served"] - before["served"],
        "provider_rejected": provider["rejected"] - before["rejected"],
    }


async def s4_provider_pacing(world: World) -> Scenario:
    sc = begin("S4")
    try:
        control = await gateway_run(world, use_adaptirate=False)
        out("  control run finished")
        protected = await gateway_run(world, use_adaptirate=True)
    finally:
        await set_gateway_mode(world.client, True)
    c429 = int(control["gateway"]["upstream_429"])
    p429 = int(protected["gateway"]["upstream_429"])
    plocal = int(protected["gateway"]["limited_locally"])
    c_ok = control["statuses"][200]
    p_ok = protected["statuses"][200]
    sc.check("control run hit the provider's limit (> 10 upstream 429s)", c429 > 10, f"control upstream_429={c429}")
    sc.check(
        "protected run had <= 20% of the control's upstream 429s",
        p429 <= 0.2 * c429,
        f"protected upstream_429={p429} vs control {c429}",
    )
    sc.check(
        "protected run still completed useful work (>= 30 successful calls)",
        p_ok >= 30,
        f"protected 200s={p_ok}, held back by AdaptiRate={plocal}",
    )
    sc.check(
        "gateway counters agree with the provider's own counters",
        c429 == control["provider_rejected"] and p429 == protected["provider_rejected"],
        f"provider rejected control={control['provider_rejected']} protected={protected['provider_rejected']}",
    )
    sc.measure(
        f"120 requests at 4/s against a 60/min provider: control upstream 429s={c429} (200s={c_ok}); "
        f"with AdaptiRate upstream 429s={p429} (200s={p_ok}, paced locally={plocal})"
    )
    return sc


async def s5_retry_after(world: World) -> Scenario:
    sc = begin("S5")
    client, policy = world.client, world.state.llm_policy
    await set_gateway_mode(client, True)

    # Part A: pause, then it lifts on schedule.
    await reset_provider_state(world)
    (await client.post(f"{PROVIDER_URL}/_drain", json={"penalty_seconds": 6})).raise_for_status()
    first = await call(client, "POST", f"{GATEWAY_URL}/complete")
    sc.check(
        "provider rejected the call with 429 and the gateway reported it",
        first.status == 429 and isinstance(first.body, dict) and first.body.get("source") == "provider",
        f"gateway answered {first.status}",
    )
    decision = await eval_direct(client, world.api_key, policy, "gateway")
    retry_ms = int(decision["retry_after_ms"])
    sc.check(
        "next check is denied with reason paused and retry_after_ms > 0",
        decision["allowed"] is False and decision["reason"] == "paused" and retry_ms > 0,
        f"allowed={decision['allowed']} reason={decision['reason']} retry_after_ms={retry_ms}",
    )
    second = await call(client, "POST", f"{GATEWAY_URL}/complete")
    sc.check(
        "gateway sheds the call locally while paused (no upstream call)",
        second.status == 429 and isinstance(second.body, dict) and second.body.get("source") == "gateway",
        f"status {second.status} source {second.body.get('source') if isinstance(second.body, dict) else None}",
    )
    started = time.monotonic()
    cleared_after: float | None = None
    while time.monotonic() - started < 12:
        decision = await eval_direct(client, world.api_key, policy, "gateway")
        if decision["allowed"]:
            cleared_after = time.monotonic() - started
            break
        await asyncio.sleep(0.25)
    sc.check(
        "pause lifts by itself after the provider's Retry-After",
        cleared_after is not None and cleared_after <= retry_ms / 1000 + 2.0,
        f"cleared after {cleared_after:.1f} s (advertised {retry_ms / 1000:.1f} s)" if cleared_after is not None else "still paused after 12 s",
    )

    # Part B: pause, then lift it on demand by forgetting the provider.
    await reset_provider_state(world)
    (await client.post(f"{PROVIDER_URL}/_drain", json={"penalty_seconds": 60})).raise_for_status()
    await call(client, "POST", f"{GATEWAY_URL}/complete")
    paused = await eval_direct(client, world.api_key, policy, "gateway")
    sc.check("long pause is in force", paused["reason"] == "paused", f"reason={paused['reason']}")
    await world.admin.req("DELETE", f"/providers/{world.state.provider}")
    resumed = await eval_direct(client, world.api_key, policy, "gateway")
    sc.check(
        "DELETE provider lifts the pause immediately",
        resumed["allowed"] is True,
        f"allowed={resumed['allowed']} reason={resumed['reason']}",
    )
    await reset_provider_state(world)
    sc.measure(
        f"upstream 429 -> paused (retry_after {retry_ms} ms), lifted after "
        f"{'%.1f s' % cleared_after if cleared_after is not None else 'never'}; DELETE provider lifted a 60 s pause at once"
    )
    return sc


async def patch_policy(world: World, changes: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await world.admin.req(
        "PATCH", f"/policies/{world.state.policy_ids['orders']}", json_body=changes
    )
    return result


async def whoami_boots(world: World, ips: list[str]) -> dict[str, str]:
    boots: dict[str, str] = {}
    for ip in ips:
        who = await call(world.client, "GET", f"http://{ip}:{SERVICE_PORT}/whoami")
        boots[ip] = str(who.body.get("boot_id", "")) if isinstance(who.body, dict) else ""
    return boots


async def s6_reconfigure(world: World) -> Scenario:
    sc = begin("S6")
    ips = await resolve(ORDERS_HOST)
    sfx = world.state.suffix
    before = await whoami_boots(world, ips)

    await align_window()
    updated = await patch_policy(world, {"algorithm": "sliding_window", "limit": 25})
    sc.check(
        "PATCH accepted (algorithm=sliding_window, limit=25)",
        updated["algorithm"] == "sliding_window" and updated["limit"] == 25,
        f"{updated['algorithm']} {updated['limit']}",
    )
    first = tally(await burst(world.client, ips, 100, f"s6-a-{sfx}"))
    sc.check("next burst is held to exactly 25", first[200] == 25 and first[429] == 75, f"200={first[200]} 429={first[429]}")

    await patch_policy(world, {"limit": 10})
    second = tally(await burst(world.client, ips, 60, f"s6-b-{sfx}"))
    sc.check("a second change (limit=10) applies to the following burst", second[200] == 10 and second[429] == 50, f"200={second[200]} 429={second[429]}")

    after = await whoami_boots(world, ips)
    original = {ip: boot for ip, boot in world.state.boots.items() if boot}
    sc.check(
        "no service restarted (same process ids before and after)",
        before == after and all(after.get(ip) == boot for ip, boot in original.items()),
        f"{len(after)} replicas unchanged",
    )
    sc.measure(f"50/min fixed -> 25 sliding: {first[200]} allowed; then 10: {second[200]} allowed; replicas not restarted")
    return sc


async def s7_prepare(world: World) -> None:
    """Teach every replica the limit it will fall back on, before run.sh pauses the API."""
    ips = await resolve(ORDERS_HOST)
    await patch_policy(world, {"algorithm": "fixed_window", "limit": 30, "window_seconds": 60, "burst": 0})
    await burst(world.client, ips, 4 * len(ips), f"s7-warm-{world.state.suffix}")
    out("S7 prepared: policy set to fixed_window 30/60s and replicas warmed")


async def cmd_outage(world: World) -> Scenario:
    sc = begin("S7")
    ips = await resolve(ORDERS_HOST)
    replies = await burst(world.client, ips, 240, f"s7-out-{world.state.suffix}")
    counts = tally(replies)
    reasons = {reply.reason for reply in replies if reply.status == 429}
    ceiling = len(ips) * (30 + 5)
    sc.check("service kept answering during the outage (no errors or timeouts)", counts[200] + counts[429] == len(replies), f"statuses {dict(counts)}")
    sc.check("some requests were allowed", counts[200] > 0, f"200={counts[200]}")
    sc.check("some requests were still limited", counts[429] > 0, f"429={counts[429]}")
    sc.check(
        f"local limiting stayed near the policy ({len(ips)} replicas x 30), not unlimited",
        counts[200] <= ceiling,
        f"200={counts[200]}, ceiling {ceiling}",
    )
    sc.check("limits came from the local fallback", "fail_local" in reasons, f"429 reasons {sorted(reasons)}")
    sc.measure(f"AdaptiRate paused: 240 requests -> 200={counts[200]} 429={counts[429]} (local fallback)")
    (RUN_DIR / "s7_outage.json").write_text(json.dumps(sc.to_dict()))
    return sc


async def cmd_recovery(world: World) -> Scenario:
    outage_file = RUN_DIR / "s7_outage.json"
    if outage_file.exists():
        sc = Scenario.from_dict(json.loads(outage_file.read_text()))
    else:
        sc = begin("S7")
        sc.check("outage phase ran", False, "no .run/s7_outage.json")
    out("S7 recovery")
    await asyncio.sleep(3)  # SDK breaker cooldown is 1 s
    ips = await resolve(ORDERS_HOST)
    exact = False
    detail = ""
    attempts = 0
    for attempts in range(1, 4):
        await align_window()
        replies = await burst(world.client, ips, 100, f"s7-back{attempts}-{world.state.suffix}")
        counts = tally(replies)
        reasons = {reply.reason for reply in replies if reply.status == 429}
        detail = f"attempt {attempts}: 200={counts[200]} 429={counts[429]} reasons {sorted(reasons)}"
        if counts[200] == 30 and counts[429] == 70 and reasons == {"limit"}:
            exact = True
            break
        await asyncio.sleep(3)
    sc.check("after unpause the exact server-side limit is back (30 of 100, reason limit)", exact, detail)
    sc.measure(f"after unpause: {detail}")
    sc.seconds = float(sc.seconds)
    outage_file.unlink(missing_ok=True)
    return sc


# ---------------------------------------------------------------------------
# Results and reports


def load_results() -> dict[str, dict[str, Any]]:
    if not RESULTS_FILE.exists():
        return {}
    return {item["id"]: item for item in json.loads(RESULTS_FILE.read_text())}


def save_result(scenario: Scenario) -> None:
    results = load_results()
    results[scenario.sid] = scenario.to_dict()
    RESULTS_FILE.write_text(json.dumps([results[sid] for sid in SCENARIO_ORDER if sid in results], indent=2))


def md_cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def cmd_report() -> int:
    results = load_results()
    rows: list[dict[str, Any]] = []
    for sid in SCENARIO_ORDER:
        title, claim = TITLES[sid]
        rows.append(
            results.get(
                sid,
                {
                    "id": sid,
                    "title": title,
                    "claim": claim,
                    "passed": False,
                    "measured": "not run",
                    "checks": [],
                    "notes": [],
                    "seconds": 0.0,
                },
            )
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    cpus = os.cpu_count() or 0
    passed = sum(1 for row in rows if row["passed"])
    overall = passed == len(rows)

    out("=" * 78)
    out(f"AdaptiRate acceptance report   {stamp}   machine cpus: {cpus}")
    out("=" * 78)
    for row in rows:
        out(f"{row['id']}  {'PASS' if row['passed'] else 'FAIL'}  {row['title']}")
        out(f"      {row['measured']}")
    out("-" * 78)
    out(f"{passed}/{len(rows)} scenarios passed  ->  {'ACCEPTED' if overall else 'NOT ACCEPTED'}")

    lines = [
        "# AdaptiRate acceptance report",
        "",
        f"- Run at: {stamp}",
        f"- Machine CPUs (as seen by Docker): {cpus}",
        f"- Result: {passed} of {len(rows)} scenarios passed ({'ACCEPTED' if overall else 'NOT ACCEPTED'})",
        "",
        "| Scenario | Claim | Measured | Result | Seconds |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['id']} {md_cell(row['title'])} | {md_cell(row['claim'])} | {md_cell(row['measured'])} "
            f"| {'PASS' if row['passed'] else 'FAIL'} | {row['seconds']} |"
        )
    lines += ["", "## Details", ""]
    for row in rows:
        lines += [f"### {row['id']} {row['title']} - {'PASS' if row['passed'] else 'FAIL'}", ""]
        for check in row["checks"]:
            lines.append(f"- {'ok' if check['ok'] else 'FAIL'}: {check['name']}" + (f" ({check['detail']})" if check["detail"] else ""))
        for note in row["notes"]:
            lines.append(f"- note: {note}")
        lines.append("")
    REPORT_MD.write_text("\n".join(lines))
    REPORT_JSON.write_text(
        json.dumps(
            {"timestamp": stamp, "machine_cpus": cpus, "passed": overall, "scenarios": rows},
            indent=2,
        )
    )
    out(f"wrote {REPORT_MD.relative_to(ROOT.parent)} and {REPORT_JSON.relative_to(ROOT.parent)}")
    return 0 if overall else 1


# ---------------------------------------------------------------------------
# Commands that need the live world


async def cmd_wait(timeout: float = 180.0) -> int:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
        while time.monotonic() < deadline:
            problems: list[str] = []
            for host in (ORDERS_HOST, ADAPTIVE_HOST):
                try:
                    ips = await resolve(host)
                except OSError:
                    ips = []
                if len(ips) < EXPECTED_REPLICAS:
                    problems.append(f"{host}: {len(ips)}/{EXPECTED_REPLICAS} replicas resolve")
                    continue
                for ip in ips:
                    reply = await call(client, "GET", f"http://{ip}:{SERVICE_PORT}/health")
                    if reply.status != 200:
                        problems.append(f"{host} {ip} not healthy")
            for name, url in (("llm-gateway", f"{GATEWAY_URL}/health"), ("fake-provider", f"{PROVIDER_URL}/health")):
                if (await call(client, "GET", url)).status != 200:
                    problems.append(f"{name} not healthy")
            if not problems:
                out("all services healthy")
                return 0
            await asyncio.sleep(2)
    out("timed out waiting for services: " + "; ".join(problems))
    return 1


async def with_world(action: Callable[[World], Awaitable[None]], *, sign_in: bool = True) -> int:
    state = State.load()
    admin = Admin()
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=100)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), limits=limits)
    try:
        if sign_in:  # the outage phase runs while the API is paused, so it must not need it
            await admin.login()
        world = World(admin, client, state, read_api_key())
        await action(world)
        return 0
    finally:
        await client.aclose()
        await admin.close()


async def timed(scenario_fn: Callable[[World], Awaitable[Scenario]], world: World, sid: str) -> Scenario:
    started = time.monotonic()
    try:
        sc = await scenario_fn(world)
    except Exception as error:  # a crashed scenario is a failed scenario, and the suite carries on
        sc = Scenario(sid, *TITLES[sid])
        sc.check("scenario ran to completion", False, f"{type(error).__name__}: {error}")
    sc.seconds = time.monotonic() - started
    sc.finish()
    save_result(sc)
    return sc


async def cmd_run(only: list[str]) -> int:
    scenarios: list[tuple[str, Callable[[World], Awaitable[Scenario]]]] = [
        ("S1", s1_exact_limit),
        ("S2", s2_isolation_rules),
        ("S3", s3_adaptive),
        ("S4", s4_provider_pacing),
        ("S5", s5_retry_after),
        ("S6", s6_reconfigure),
    ]

    async def action(world: World) -> None:
        for sid, function in scenarios:
            if not only or sid in only:
                await timed(function, world, sid)
        if not only or "S7" in only:
            await s7_prepare(world)

    return await with_world(action)


async def cmd_s7(phase: str) -> int:
    async def action(world: World) -> None:
        started = time.monotonic()
        try:
            sc = await (cmd_outage(world) if phase == "outage" else cmd_recovery(world))
        except Exception as error:
            sc = Scenario("S7", *TITLES["S7"])
            sc.check(f"S7 {phase} ran to completion", False, f"{type(error).__name__}: {error}")
        elapsed = time.monotonic() - started
        sc.seconds += elapsed
        if phase == "recovery" or not sc.passed:
            sc.finish()
            save_result(sc)
        elif phase == "outage":
            out("S7 outage phase done; recovery check follows")

    return await with_world(action, sign_in=phase != "outage")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "wait", "outage", "recovery", "report", "teardown"):
        sub.add_parser(name)
    run = sub.add_parser("run")
    run.add_argument("--only", nargs="*", default=[], help="scenario ids to run, for example S3 S4")
    args = parser.parse_args()

    if args.command == "setup":
        return asyncio.run(cmd_setup())
    if args.command == "teardown":
        return asyncio.run(cmd_teardown())
    if args.command == "wait":
        return asyncio.run(cmd_wait())
    if args.command == "report":
        return cmd_report()
    if args.command == "run":
        return asyncio.run(cmd_run([item.upper() for item in args.only]))
    return asyncio.run(cmd_s7(args.command))


if __name__ == "__main__":
    sys.exit(main())

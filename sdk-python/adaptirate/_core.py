"""Transport-independent logic shared by the sync and async clients."""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

API_PREFIX = "/api/v1"
DEFAULT_URL = "http://localhost:8080"

# Upper bounds (ms) of the latency histogram; must match the server.
LATENCY_BOUNDS_MS: tuple[float, ...] = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

FailMode = Literal["open", "closed", "local"]


class AdaptiRateError(Exception):
    """The server rejected the request itself: bad key, unknown policy, invalid input."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"AdaptiRate returned {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int
    reset_after_ms: int
    reason: str
    policy: str
    # "server", "lease" (decided locally from leased tokens) or "fallback" (server unreachable).
    source: str = "server"

    @property
    def retry_after_seconds(self) -> float:
        return max(0.0, self.retry_after_ms / 1000)

    def headers(self) -> dict[str, str]:
        """Standard rate-limit response headers for this decision."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(max(0, -(-self.reset_after_ms // 1000))),
        }
        if not self.allowed and self.retry_after_ms > 0:
            headers["Retry-After"] = str(max(1, -(-self.retry_after_ms // 1000)))
        return headers


class RateLimitExceeded(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(f"Rate limit exceeded for policy '{decision.policy}' ({decision.reason})")
        self.decision = decision
        self.retry_after_seconds = decision.retry_after_seconds


@dataclass
class Options:
    api_key: str
    base_url: str = DEFAULT_URL
    timeout: float = 1.0
    fail_mode: FailMode = "open"
    # Tokens to lease per round trip. 0 asks the server on every check.
    lease_size: int = 0
    report_interval: float = 1.0
    # After a failed call, skip the server for this long instead of timing out on every request.
    cooldown: float = 1.0
    instance_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    )

    @classmethod
    def resolve(cls, api_key: str | None, base_url: str | None, **overrides: Any) -> Options:
        key = api_key or os.environ.get("ADAPTIRATE_API_KEY")
        if not key:
            raise ValueError("An API key is required: pass api_key or set ADAPTIRATE_API_KEY")
        url = (base_url or os.environ.get("ADAPTIRATE_URL") or DEFAULT_URL).rstrip("/")
        if url.endswith(API_PREFIX):
            url = url[: -len(API_PREFIX)]
        options = cls(api_key=key, base_url=url, **overrides)
        if options.fail_mode not in ("open", "closed", "local"):
            raise ValueError("fail_mode must be 'open', 'closed' or 'local'")
        if options.lease_size < 0:
            raise ValueError("lease_size cannot be negative")
        return options

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "User-Agent": "adaptirate-python/0.1.0"}


def subject_payload(
    policy: str,
    *,
    key: str | None,
    user: str | None,
    ip: str | None,
    api_key: str | None,
    role: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"policy": policy}
    for name, value in (("key", key), ("user", user), ("ip", ip), ("api_key", api_key), ("role", role)):
        if value is not None:
            payload[name] = str(value)
    return payload


def bucket_id(payload: dict[str, Any]) -> tuple[str, ...]:
    """Identity of the server-side bucket a payload maps to, for local lease bookkeeping."""
    return tuple(str(payload.get(name, "")) for name in ("policy", "key", "user", "ip", "api_key", "role"))


def decision_from(body: dict[str, Any], source: str = "server") -> Decision:
    return Decision(
        allowed=bool(body["allowed"]),
        limit=int(body["limit"]),
        remaining=int(body["remaining"]),
        retry_after_ms=int(body["retry_after_ms"]),
        reset_after_ms=int(body["reset_after_ms"]),
        reason=str(body["reason"]),
        policy=str(body["policy"]),
        source=source,
    )


def error_detail(status: int, body: Any) -> AdaptiRateError:
    detail = body.get("detail") if isinstance(body, dict) else body
    return AdaptiRateError(status, str(detail))


# ---- leases ---------------------------------------------------------------


@dataclass
class _Lease:
    tokens: int = 0
    expires_at: float = 0.0
    blocked_until: float = 0.0
    limit: int = 0
    last: Decision | None = None


class LeasePool:
    """Tokens leased from the server and spent locally. Thread-safe.

    Expired tokens are discarded, never carried over, so local decisions can lag the
    server's limit by at most one lease.
    """

    MAX_LOCAL_BLOCK_SECONDS = 1.0

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._leases: dict[tuple[str, ...], _Lease] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def try_spend(self, bucket: tuple[str, ...], cost: int, policy: str) -> Decision | None:
        """A local decision, or None when the server must be asked."""
        now = self._clock()
        with self._lock:
            lease = self._leases.get(bucket)
            if lease is None:
                return None
            if now < lease.expires_at and lease.tokens >= cost:
                lease.tokens -= cost
                return Decision(True, lease.limit, lease.tokens, 0, 0, "ok", policy, "lease")
            if now < lease.blocked_until and lease.last is not None:
                remaining_ms = int((lease.blocked_until - now) * 1000)
                last = lease.last
                return Decision(
                    False,
                    last.limit,
                    0,
                    max(1, remaining_ms),
                    last.reset_after_ms,
                    last.reason,
                    policy,
                    "lease",
                )
            return None

    def store(
        self, bucket: tuple[str, ...], granted: int, cost: int, ttl_ms: int, decision: Decision
    ) -> Decision:
        """Record a lease response and decide the request that triggered it."""
        now = self._clock()
        with self._lock:
            lease = self._leases.setdefault(bucket, _Lease())
            lease.limit = decision.limit
            lease.last = decision
            if granted >= cost:
                lease.tokens = granted - cost
                lease.expires_at = now + ttl_ms / 1000
                lease.blocked_until = 0.0
                return Decision(
                    True,
                    decision.limit,
                    lease.tokens,
                    0,
                    decision.reset_after_ms,
                    "ok",
                    decision.policy,
                    "server",
                )
            # Not enough for this request. Keep what was granted for cheaper requests, and
            # stop asking the server until it could plausibly say yes.
            lease.tokens = granted
            lease.expires_at = now + ttl_ms / 1000
            wait = (
                decision.retry_after_ms / 1000
                if decision.retry_after_ms > 0
                else self.MAX_LOCAL_BLOCK_SECONDS
            )
            lease.blocked_until = now + min(wait, self.MAX_LOCAL_BLOCK_SECONDS)
            reason = decision.reason if decision.reason != "ok" else "limit"
            return Decision(
                False,
                decision.limit,
                granted,
                max(1, decision.retry_after_ms),
                decision.reset_after_ms,
                reason,
                decision.policy,
                "server",
            )


# ---- fallback when the server is unreachable ------------------------------


class LocalFallback:
    """What to do when AdaptiRate cannot be reached.

    'open' allows, 'closed' denies, and 'local' enforces the last limit the server
    reported with an in-process token bucket, so an outage neither blocks everything
    nor removes all protection.
    """

    def __init__(self, mode: FailMode, clock: Any = time.monotonic) -> None:
        self.mode = mode
        self._clock = clock
        self._lock = threading.Lock()
        self._known: dict[str, tuple[int, float]] = {}  # policy -> (limit, window seconds)
        self._buckets: dict[tuple[str, ...], tuple[float, float]] = {}  # bucket -> (tokens, timestamp)

    def learn(self, decision: Decision) -> None:
        if decision.source != "server" or decision.limit <= 0:
            return
        window = decision.reset_after_ms / 1000 if decision.reset_after_ms > 0 else 0.0
        with self._lock:
            previous = self._known.get(decision.policy)
            # reset_after shrinks as a window elapses; the largest seen approximates the window.
            best_window = max(window, previous[1] if previous else 0.0) or 1.0
            self._known[decision.policy] = (decision.limit, best_window)

    def decide(self, policy: str, bucket: tuple[str, ...], cost: int) -> Decision:
        if self.mode == "closed":
            return Decision(False, 0, 0, 1000, 1000, "fail_closed", policy, "fallback")
        if self.mode == "open":
            return Decision(True, 0, 0, 0, 0, "fail_open", policy, "fallback")
        with self._lock:
            known = self._known.get(policy)
            if known is None:
                return Decision(True, 0, 0, 0, 0, "fail_open", policy, "fallback")
            limit, window = known
            rate = limit / window
            now = self._clock()
            tokens, stamp = self._buckets.get(bucket, (float(limit), now))
            tokens = min(float(limit), tokens + (now - stamp) * rate)
            if tokens >= cost:
                self._buckets[bucket] = (tokens - cost, now)
                return Decision(True, limit, int(tokens - cost), 0, 0, "fail_local", policy, "fallback")
            self._buckets[bucket] = (tokens, now)
            wait_ms = int((cost - tokens) / rate * 1000) + 1
            return Decision(False, limit, 0, wait_ms, wait_ms, "fail_local", policy, "fallback")


class Breaker:
    """After a failure, short-circuit to the fallback for a moment rather than making
    every request wait out a timeout."""

    def __init__(self, cooldown: float, clock: Any = time.monotonic) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return bool(self._clock() < self._open_until)

    def failure(self) -> None:
        self._open_until = self._clock() + self._cooldown

    def success(self) -> None:
        self._open_until = 0.0


# ---- signal reporting -----------------------------------------------------


def _bucket_index(latency_ms: float) -> int:
    for index, bound in enumerate(LATENCY_BOUNDS_MS):
        if latency_ms <= bound:
            return index
    return len(LATENCY_BOUNDS_MS)


@dataclass
class _Window:
    requests: int = 0
    errors: int = 0
    throttled: int = 0
    timeouts: int = 0
    latency_sum_ms: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(LATENCY_BOUNDS_MS) + 1))


class Reporter:
    """Aggregates what the protected service observes; flushed once per interval. Thread-safe."""

    def __init__(self, instance_id: str) -> None:
        self._instance_id = instance_id
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        self._in_flight: dict[str, int] = {}
        self._queue_depth: dict[str, int] = {}
        self._seen: set[str] = set()

    def started(self, policy: str) -> None:
        with self._lock:
            self._in_flight[policy] = self._in_flight.get(policy, 0) + 1
            self._seen.add(policy)

    def finished(
        self,
        policy: str,
        latency_ms: float,
        *,
        error: bool = False,
        throttled: bool = False,
        timeout: bool = False,
    ) -> None:
        with self._lock:
            self._in_flight[policy] = max(0, self._in_flight.get(policy, 0) - 1)
            window = self._windows.setdefault(policy, _Window())
            window.requests += 1
            window.errors += int(error)
            window.throttled += int(throttled)
            window.timeouts += int(timeout)
            window.latency_sum_ms += latency_ms
            window.buckets[_bucket_index(latency_ms)] += 1

    def set_queue_depth(self, policy: str, depth: int) -> None:
        with self._lock:
            self._queue_depth[policy] = max(0, depth)
            self._seen.add(policy)

    def drain(self) -> list[dict[str, Any]]:
        """Reports to send. Policies with in-flight work report even when nothing finished,
        so a stalled service still shows up as a growing in-flight count."""
        with self._lock:
            windows, self._windows = self._windows, {}
            policies = set(windows) | {
                p for p in self._seen if self._in_flight.get(p) or p in self._queue_depth
            }
            payloads: list[dict[str, Any]] = []
            for policy in sorted(policies):
                window = windows.get(policy, _Window())
                payload: dict[str, Any] = {
                    "policy": policy,
                    "instance_id": self._instance_id,
                    "requests": window.requests,
                    "errors": window.errors,
                    "throttled": window.throttled,
                    "timeouts": window.timeouts,
                    "latency_sum_ms": round(window.latency_sum_ms, 3),
                    "latency_buckets": window.buckets,
                    "in_flight": self._in_flight.get(policy, 0),
                }
                if policy in self._queue_depth:
                    payload["queue_depth"] = self._queue_depth[policy]
                payloads.append(payload)
            return payloads


def host_gauges() -> dict[str, float]:
    """CPU and memory of this host when psutil is installed; empty otherwise."""
    try:
        import psutil  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        return {}
    return {
        "cpu_percent": float(psutil.cpu_percent(interval=None)),
        "memory_percent": float(psutil.virtual_memory().percent),
    }


def is_server_error(status: int) -> bool:
    """Whether a response the protected service produced counts against its health.

    Only 5xx does. A 4xx is the caller's mistake, and a 429 the service chose to send is
    deliberate load shedding: treating either as ill health would make the limiter cut
    limits because it is limiting.
    """
    return status >= 500

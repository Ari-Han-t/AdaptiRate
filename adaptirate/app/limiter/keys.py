"""Redis key names. One place so the API, worker and tests agree."""

from __future__ import annotations

import hashlib

PREFIX = "ar:"

INVALIDATE_CHANNEL = PREFIX + "invalidate"
EVENT_STREAM = PREFIX + "events"
GLOBAL_COUNTER_PREFIX = PREFIX + "c:_:"
TOTALS = GLOBAL_COUNTER_PREFIX + "total"
TOP_PREFIX = PREFIX + "top:"
EVENT_CURSOR = PREFIX + "events:cursor"
WORKER_LOCK = PREFIX + "worker:lock"
WORKER_HEARTBEAT = PREFIX + "worker:heartbeat"
API_HEARTBEATS = PREFIX + "api:heartbeats"
HOST_SIGNALS = PREFIX + "host"
CACHE_STATS = PREFIX + "cache:stats"

MAX_SUBJECT_LENGTH = 200


def normalize_subject(subject: str) -> str:
    """Bound key length; long or awkward subjects are replaced by a stable hash."""
    if len(subject) <= MAX_SUBJECT_LENGTH and subject.isprintable() and " " not in subject:
        return subject
    return "h:" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


def state_key(policy_id: str, algorithm: str, subject: str) -> str:
    # The algorithm is part of the key so a live algorithm switch never reads foreign state.
    return f"{PREFIX}s:{policy_id}:{algorithm}:{subject}"


def state_pattern(policy_id: str) -> str:
    return f"{PREFIX}s:{policy_id}:*"


def multiplier_key(policy_id: str) -> str:
    return f"{PREFIX}m:{policy_id}"


def pause_key(policy_id: str) -> str:
    return f"{PREFIX}p:{policy_id}"


def counter_prefix(policy_id: str) -> str:
    return f"{PREFIX}c:{policy_id}:"


def counter_key(policy_id: str | None, second: int) -> str:
    return f"{GLOBAL_COUNTER_PREFIX if policy_id is None else counter_prefix(policy_id)}{second}"


def top_key(kind: str, hour: int) -> str:
    """kind is 'a' (allowed) or 'b' (blocked)."""
    return f"{TOP_PREFIX}{kind}:{hour}"


def signal_key(policy_id: str, second: int) -> str:
    return f"{PREFIX}sig:{policy_id}:{second}"


def instances_key(policy_id: str) -> str:
    return f"{PREFIX}inst:{policy_id}"


def adaptive_state_key(policy_id: str) -> str:
    return f"{PREFIX}ad:{policy_id}"


def adaptive_series_key(policy_id: str, second: int) -> str:
    return f"{PREFIX}ads:{policy_id}:{second}"


def provider_state_key(name: str) -> str:
    return f"{PREFIX}prov:{name}"


def key_usage_key(day: str) -> str:
    return f"{PREFIX}ku:{day}"


def key_last_used_key() -> str:
    return f"{PREFIX}klu"

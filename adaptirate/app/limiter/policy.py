"""In-memory policy representation and request-to-bucket resolution. Pure; no I/O."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .engine import Algorithm, Check


class Scope(StrEnum):
    GLOBAL = "global"
    USER = "user"
    IP = "ip"
    API_KEY = "api_key"
    ROLE = "role"
    CUSTOM = "custom"


class RuleAction(StrEnum):
    LIMIT = "limit"
    ALLOW = "allow"
    DENY = "deny"


RULE_ATTRIBUTES = ("user", "ip", "api_key", "role", "key")


def canonical_ip(value: str) -> str:
    """One spelling per address, so a rule or bucket cannot be dodged by writing the
    same address differently (IPv4-mapped IPv6, uncompressed IPv6, upper case)."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


class ResolutionError(ValueError):
    """The request lacks what the policy needs to pick a bucket."""


@dataclass(frozen=True, slots=True)
class Rule:
    attribute: str
    values: frozenset[str]
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    action: RuleAction
    priority: int
    limit: int | None
    window_ms: int | None
    burst: int | None

    def matches(self, attrs: dict[str, str]) -> bool:
        value = attrs.get(self.attribute)
        if value is None:
            return False
        if value in self.values:
            return True
        if self.networks:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                return False
            return any(address.version == network.version and address in network for network in self.networks)
        return False


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    id: str
    name: str
    enabled: bool
    algorithm: Algorithm
    limit: int
    window_ms: int
    burst: int
    priority: int
    scope: Scope
    adaptive: bool
    min_limit: int
    max_limit: int
    target_latency_ms: float | None
    provider: str | None
    provider_dimension: str
    rules: tuple[Rule, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Resolution:
    """Either a Redis check to run, or a decision already made by an allow/deny rule."""

    check: Check | None
    subject: str
    verdict: RuleAction


def compile_rule(raw: dict[str, Any]) -> Rule:
    attribute = str(raw["attribute"])
    values: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.get("values", []):
        text = str(item)
        if attribute == "ip" and "/" in text:
            networks.append(ipaddress.ip_network(text, strict=False))
        elif attribute == "ip":
            values.add(canonical_ip(text))
        else:
            values.add(text)
    window = raw.get("window_seconds")
    return Rule(
        attribute=attribute,
        values=frozenset(values),
        networks=tuple(networks),
        action=RuleAction(raw.get("action", "limit")),
        priority=int(raw.get("priority", 0)),
        limit=raw.get("limit"),
        window_ms=None if window is None else max(1, round(float(window) * 1000)),
        burst=raw.get("burst"),
    )


def compile_policy(row: Any) -> CompiledPolicy:
    """Build from an ORM Policy row (or anything with the same attributes)."""
    rules = sorted((compile_rule(raw) for raw in row.rules or []), key=lambda rule: -rule.priority)
    limit = int(row.limit)
    min_limit = int(row.min_limit) if row.min_limit is not None else max(1, limit // 10)
    max_limit = int(row.max_limit) if row.max_limit is not None else limit
    return CompiledPolicy(
        id=row.id,
        name=row.name,
        enabled=bool(row.enabled),
        algorithm=Algorithm(row.algorithm),
        limit=limit,
        window_ms=max(1, round(float(row.window_seconds) * 1000)),
        burst=int(row.burst),
        priority=int(row.priority),
        scope=Scope(row.scope),
        adaptive=bool(row.adaptive),
        min_limit=min(min_limit, limit),
        max_limit=max(max_limit, limit),
        target_latency_ms=row.target_latency_ms,
        provider=row.provider,
        provider_dimension=row.provider_dimension,
        rules=tuple(rules),
    )


def resolve(
    policy: CompiledPolicy,
    *,
    key: str | None,
    attrs: dict[str, str],
    cost: int = 1,
) -> Resolution:
    """Pick the bucket and limit for one request.

    A missing attribute that the policy's scope requires is an error rather than a
    shared fallback bucket: silently pooling such requests would let a caller dodge
    its own limit by omitting the field.
    """
    lookup = dict(attrs)
    if key is not None:
        lookup["key"] = key
    if "ip" in lookup:
        lookup["ip"] = canonical_ip(lookup["ip"])

    if policy.scope is Scope.GLOBAL:
        subject = "*"
    else:
        attribute = "key" if policy.scope is Scope.CUSTOM else policy.scope.value
        value = lookup.get(attribute)
        if not value:
            raise ResolutionError(
                f"policy '{policy.name}' is scoped by '{attribute}' but the request has none"
            )
        subject = value

    limit, window_ms, burst = policy.limit, policy.window_ms, policy.burst
    for rule in policy.rules:
        if not rule.matches(lookup):
            continue
        if rule.action is not RuleAction.LIMIT:
            return Resolution(check=None, subject=subject, verdict=rule.action)
        if rule.limit is not None:
            limit = rule.limit
        if rule.window_ms is not None:
            window_ms = rule.window_ms
        if rule.burst is not None:
            burst = rule.burst
        break

    check = Check(
        policy_id=policy.id,
        policy_name=policy.name,
        algorithm=policy.algorithm,
        limit=limit,
        window_ms=window_ms,
        burst=burst,
        subject=subject,
        cost=cost,
    )
    return Resolution(check=check, subject=subject, verdict=RuleAction.LIMIT)

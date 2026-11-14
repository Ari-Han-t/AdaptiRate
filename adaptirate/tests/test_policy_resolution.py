from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.limiter.policy import ResolutionError, RuleAction, compile_policy, resolve


def policy(**overrides: Any):
    row = {
        "id": "p1",
        "name": "api",
        "enabled": True,
        "algorithm": "token_bucket",
        "limit": 100,
        "window_seconds": 60,
        "burst": 0,
        "priority": 50,
        "scope": "user",
        "rules": [],
        "adaptive": True,
        "min_limit": None,
        "max_limit": None,
        "target_latency_ms": None,
        "provider": None,
        "provider_dimension": "requests",
    }
    row.update(overrides)
    return compile_policy(SimpleNamespace(**row))


def test_defaults_for_adaptive_bounds():
    compiled = policy(limit=100)
    assert (compiled.min_limit, compiled.max_limit) == (10, 100)
    assert compiled.window_ms == 60_000


def test_scope_selects_the_bucket_subject():
    assert resolve(policy(scope="user"), key=None, attrs={"user": "u1"}).subject == "u1"
    assert resolve(policy(scope="ip"), key=None, attrs={"ip": "10.0.0.1"}).subject == "10.0.0.1"
    assert resolve(policy(scope="custom"), key="tenant-7", attrs={}).subject == "tenant-7"
    assert resolve(policy(scope="global"), key=None, attrs={}).subject == "*"


def test_missing_scope_attribute_is_an_error_not_a_shared_bucket():
    with pytest.raises(ResolutionError):
        resolve(policy(scope="user"), key=None, attrs={"ip": "10.0.0.1"})
    with pytest.raises(ResolutionError):
        resolve(policy(scope="user"), key=None, attrs={"user": ""})


def test_rule_overrides_limit_for_matching_role():
    compiled = policy(rules=[{"attribute": "role", "values": ["premium"], "limit": 1000, "burst": 50}])
    premium = resolve(compiled, key=None, attrs={"user": "u1", "role": "premium"})
    basic = resolve(compiled, key=None, attrs={"user": "u2", "role": "free"})
    assert premium.check is not None and basic.check is not None
    assert (premium.check.limit, premium.check.burst) == (1000, 50)
    assert (basic.check.limit, basic.check.burst) == (100, 0)


def test_highest_priority_rule_wins():
    compiled = policy(
        rules=[
            {"attribute": "role", "values": ["staff"], "limit": 500, "priority": 1},
            {"attribute": "user", "values": ["u1"], "limit": 5, "priority": 10},
        ]
    )
    resolution = resolve(compiled, key=None, attrs={"user": "u1", "role": "staff"})
    assert resolution.check is not None
    assert resolution.check.limit == 5


def test_cidr_rule_matches_addresses_in_range():
    compiled = policy(scope="ip", rules=[{"attribute": "ip", "values": ["10.0.0.0/8"], "action": "allow"}])
    assert resolve(compiled, key=None, attrs={"ip": "10.20.30.40"}).verdict is RuleAction.ALLOW
    outside = resolve(compiled, key=None, attrs={"ip": "192.168.1.1"})
    assert outside.verdict is RuleAction.LIMIT
    assert outside.check is not None


def test_deny_rule_short_circuits_without_a_check():
    compiled = policy(rules=[{"attribute": "user", "values": ["banned"], "action": "deny"}])
    resolution = resolve(compiled, key=None, attrs={"user": "banned"})
    assert resolution.verdict is RuleAction.DENY
    assert resolution.check is None


def test_rule_window_override_converts_to_milliseconds():
    compiled = policy(rules=[{"attribute": "role", "values": ["batch"], "limit": 10, "window_seconds": 1.5}])
    resolution = resolve(compiled, key=None, attrs={"user": "u", "role": "batch"})
    assert resolution.check is not None
    assert resolution.check.window_ms == 1500

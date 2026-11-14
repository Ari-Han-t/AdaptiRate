"""The hot path: decisions, leases and signal reports. Redis only; no Postgres."""

from __future__ import annotations

import time

import orjson
from fastapi import APIRouter, HTTPException, Response, status
from redis.asyncio import Redis

from ..adaptive import signals
from ..deps import CacheDep, EngineDep, EvaluatePrincipal, EventsDep, Principal, RedisDep, ReportPrincipal
from ..limiter import keys
from ..limiter.cache import RuntimeCache
from ..limiter.engine import Check, Decision
from ..limiter.policy import CompiledPolicy, Resolution, ResolutionError, RuleAction, resolve
from ..providers import service as provider_service
from ..schemas import (
    BatchEvaluateRequest,
    BatchEvaluateResponse,
    EvaluateRequest,
    EvaluateResponse,
    LeaseRequest,
    LeaseResponse,
    ProviderReport,
    ReportRequest,
    Subject,
)
from ..services.events import EventBuffer

router = APIRouter(tags=["Evaluate"])


def _json(body: object) -> Response:
    """Serialise once with orjson and skip response-model validation on the hot path."""
    return Response(content=orjson.dumps(body), media_type="application/json")


MIN_LEASE_TTL_MS = 100
MAX_LEASE_TTL_MS = 2000


def _policy(cache: RuntimeCache, principal: Principal, name: str) -> CompiledPolicy:
    policy = cache.policies.get(name)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy '{name}' does not exist")
    if not principal.can_use_policy(policy.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This API key cannot use policy '{name}'")
    return policy


def _resolve(policy: CompiledPolicy, subject: Subject, cost: int) -> Resolution:
    try:
        return resolve(policy, key=subject.key, attrs=subject.attrs(), cost=cost)
    except ResolutionError as exc:
        raise HTTPException(422, str(exc)) from exc


def _static_decision(policy: CompiledPolicy, allowed: bool, reason: str) -> Decision:
    return Decision(
        allowed=allowed,
        limit=policy.limit,
        remaining=policy.limit if allowed else 0,
        retry_after_ms=0 if allowed else -1,
        reset_after_ms=0,
        reason=reason,
    )


def _count_static(pipe: Redis, policy: CompiledPolicy, subject: str, allowed: bool, cost: int) -> None:
    """Counters for decisions made without running a limiter script (allow/deny rules)."""
    now = int(time.time())
    field = "a" if allowed else "b"
    for key in (keys.counter_key(policy.id, now), keys.counter_key(None, now)):
        pipe.hincrby(key, field, cost)
        pipe.expire(key, 3700)
    top = keys.top_key(field, now // 3600)
    pipe.zincrby(top, cost, f"{policy.name}|{keys.normalize_subject(subject)}")
    pipe.expire(top, 90000)
    pipe.hincrby(keys.TOTALS, f"{field}:{policy.name}", cost)


def _body(policy: CompiledPolicy, decision: Decision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "limit": decision.limit,
        "remaining": decision.remaining,
        "retry_after_ms": decision.retry_after_ms,
        "reset_after_ms": decision.reset_after_ms,
        "reason": decision.reason,
        "policy": policy.name,
    }


def _log(
    events: EventBuffer,
    cache: RuntimeCache,
    principal: Principal,
    policy: CompiledPolicy,
    subject: str,
    decision: Decision,
    cost: int,
) -> None:
    logging = cache.config.logging
    events.record(
        policy_id=policy.id,
        policy_name=policy.name,
        subject=subject,
        allowed=decision.allowed,
        reason=decision.reason,
        limit=decision.limit,
        remaining=decision.remaining,
        cost=cost,
        api_key_id=principal.id if principal.kind == "api_key" else None,
        log_blocked=logging.log_blocked,
        sample_allowed=logging.sample_allowed,
    )


@router.post("/evaluate", response_model=EvaluateResponse, summary="Decide whether one request may proceed")
async def evaluate(
    body: EvaluateRequest,
    principal: EvaluatePrincipal,
    cache: CacheDep,
    engine: EngineDep,
    events: EventsDep,
    redis: RedisDep,
) -> Response:
    policy = _policy(cache, principal, body.policy)
    if not policy.enabled:
        return _json(_body(policy, _static_decision(policy, True, "disabled")))

    resolution = _resolve(policy, body, body.cost)
    if resolution.check is None:
        allowed = resolution.verdict is RuleAction.ALLOW
        decision = _static_decision(policy, allowed, "rule_allow" if allowed else "rule_deny")
        pipe = redis.pipeline(transaction=False)
        _count_static(pipe, policy, resolution.subject, allowed, body.cost)
        await pipe.execute()
    else:
        decision = await engine.check(resolution.check)

    _log(events, cache, principal, policy, resolution.subject, decision, body.cost)
    return _json(_body(policy, decision))


@router.post(
    "/evaluate/batch",
    response_model=BatchEvaluateResponse,
    summary="Decide many requests in one Redis round trip",
)
async def evaluate_batch(
    body: BatchEvaluateRequest,
    principal: EvaluatePrincipal,
    cache: CacheDep,
    engine: EngineDep,
    events: EventsDep,
    redis: RedisDep,
) -> Response:
    prepared: list[tuple[CompiledPolicy, Resolution | None, int]] = []
    checks: list[Check] = []
    static = redis.pipeline(transaction=False)
    has_static = False
    for item in body.checks:
        policy = _policy(cache, principal, item.policy)
        if not policy.enabled:
            prepared.append((policy, None, item.cost))
            continue
        resolution = _resolve(policy, item, item.cost)
        prepared.append((policy, resolution, item.cost))
        if resolution.check is not None:
            checks.append(resolution.check)
        else:
            _count_static(
                static, policy, resolution.subject, resolution.verdict is RuleAction.ALLOW, item.cost
            )
            has_static = True

    decisions = iter(await engine.check_many(checks))
    if has_static:
        await static.execute()

    results: list[dict[str, object]] = []
    for policy, resolved, cost in prepared:
        if resolved is None:
            results.append(_body(policy, _static_decision(policy, True, "disabled")))
            continue
        if resolved.check is None:
            allowed = resolved.verdict is RuleAction.ALLOW
            decision = _static_decision(policy, allowed, "rule_allow" if allowed else "rule_deny")
        else:
            decision = next(decisions)
        _log(events, cache, principal, policy, resolved.subject, decision, cost)
        results.append(_body(policy, decision))
    return _json({"results": results})


@router.post(
    "/lease",
    response_model=LeaseResponse,
    summary="Reserve a block of tokens for an SDK to spend locally",
)
async def lease(
    body: LeaseRequest,
    principal: EvaluatePrincipal,
    cache: CacheDep,
    engine: EngineDep,
    events: EventsDep,
) -> Response:
    policy = _policy(cache, principal, body.policy)
    if not policy.enabled:
        decision = _static_decision(policy, True, "disabled")
        return _json({**_body(policy, decision), "granted": body.tokens, "ttl_ms": MAX_LEASE_TTL_MS})

    resolution = _resolve(policy, body, body.tokens)
    if resolution.check is None:
        allowed = resolution.verdict is RuleAction.ALLOW
        decision = _static_decision(policy, allowed, "rule_allow" if allowed else "rule_deny")
        granted = body.tokens if allowed else 0
        ttl_ms = MAX_LEASE_TTL_MS
    else:
        granted, decision = await engine.lease(resolution.check)
        # A lease may be spent a little after it was granted. Keeping it short bounds how
        # much traffic can slip from one window into the next.
        ttl_ms = int(min(MAX_LEASE_TTL_MS, max(MIN_LEASE_TTL_MS, resolution.check.window_ms / 2)))

    _log(events, cache, principal, policy, resolution.subject, decision, granted or body.tokens)
    return _json({**_body(policy, decision), "granted": granted, "ttl_ms": ttl_ms})


@router.post(
    "/report", status_code=status.HTTP_202_ACCEPTED, summary="Report what a protected service observed"
)
async def report(
    body: ReportRequest, principal: ReportPrincipal, cache: CacheDep, redis: RedisDep
) -> dict[str, bool]:
    policy = _policy(cache, principal, body.policy)
    if body.latency_buckets is not None and len(body.latency_buckets) != signals.BUCKET_COUNT:
        raise HTTPException(
            422,
            f"latency_buckets must have {signals.BUCKET_COUNT} entries",
        )
    await signals.record_report(
        redis,
        policy.id,
        instance_id=body.instance_id,
        requests=body.requests,
        errors=body.errors,
        throttled=body.throttled,
        timeouts=body.timeouts,
        latency_sum_ms=body.latency_sum_ms,
        latency_buckets=body.latency_buckets,
        in_flight=body.in_flight,
        queue_depth=body.queue_depth,
        cpu_percent=body.cpu_percent,
        memory_percent=body.memory_percent,
    )
    return {"accepted": True}


@router.post("/providers/report", summary="Report an upstream provider response (status and headers)")
async def provider_report(
    body: ProviderReport, principal: ReportPrincipal, cache: CacheDep, redis: RedisDep
) -> dict[str, object]:
    observation, pause = await provider_service.record_response(
        redis,
        body.provider,
        body.status,
        body.headers,
        policies=list(cache.policies.values()),
        custom=cache.custom_providers.get(body.provider),
    )
    return {"provider": body.provider, "throttled": observation.throttled, "paused_ms": int(pause * 1000)}

"""Policy management and the algorithm catalogue."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..deps import (
    CacheDep,
    EngineDep,
    ManagePrincipal,
    Principal,
    ReadPrincipal,
    RedisDep,
    SessionDep,
    client_ip,
)
from ..limiter import keys
from ..limiter.cache import publish_invalidation
from ..limiter.engine import ALGORITHM_INFO, Algorithm
from ..models import Policy
from ..schemas import PolicyCreate, PolicyOut, PolicyUpdate
from ..services import audit

router = APIRouter(tags=["Policies"])


async def _with_live_state(redis: Redis, rows: list[Policy]) -> list[PolicyOut]:
    """Attach the adaptive engine's current view of each policy."""
    if not rows:
        return []
    pipe = redis.pipeline(transaction=False)
    for row in rows:
        pipe.get(keys.multiplier_key(row.id))
        pipe.pttl(keys.pause_key(row.id))
        pipe.hget(keys.adaptive_state_key(row.id), "health")
    raw = await pipe.execute()
    result: list[PolicyOut] = []
    for index, row in enumerate(rows):
        multiplier_raw, pause_ttl, health_raw = raw[index * 3 : index * 3 + 3]
        multiplier = float(multiplier_raw) if multiplier_raw is not None else 1.0
        out = PolicyOut.model_validate(row)
        out.multiplier = multiplier
        out.effective_limit = max(1, round(row.limit * multiplier))
        out.health = float(health_raw) if health_raw is not None else None
        out.paused_ms = max(0, int(pause_ttl))
        result.append(out)
    return result


async def _get(session: SessionDep, policy_id: str) -> Policy:
    row = await session.get(Policy, policy_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    return row


def _require_access(principal: Principal, row: Policy) -> None:
    if not principal.can_use_policy(row.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This API key cannot manage policy '{row.name}'")


def _validate_bounds(row: Policy) -> None:
    if row.min_limit is not None and row.min_limit > row.limit:
        raise HTTPException(422, "min_limit cannot exceed limit")
    if row.max_limit is not None and row.max_limit < row.limit:
        raise HTTPException(422, "max_limit cannot be below limit")


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(_: ReadPrincipal, session: SessionDep, redis: RedisDep) -> list[PolicyOut]:
    rows = list((await session.execute(select(Policy).order_by(Policy.name))).scalars().all())
    return await _with_live_state(redis, rows)


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyCreate,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> PolicyOut:
    if principal.policy_ids:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "A key restricted to certain policies cannot create policies"
        )
    row = Policy(**body.model_dump(mode="json"))
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A policy named '{body.name}' already exists"
        ) from None
    audit.record(
        session,
        principal,
        "policy.create",
        resource_type="policy",
        resource_id=row.id,
        detail=body.model_dump(mode="json"),
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(row)
    await cache.load()
    await publish_invalidation(redis)
    return (await _with_live_state(redis, [row]))[0]


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(policy_id: str, _: ReadPrincipal, session: SessionDep, redis: RedisDep) -> PolicyOut:
    return (await _with_live_state(redis, [await _get(session, policy_id)]))[0]


@router.patch("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> PolicyOut:
    row = await _get(session, policy_id)
    _require_access(principal, row)
    changes: dict[str, Any] = body.model_dump(mode="json", exclude_unset=True)
    non_nullable = {
        "description",
        "enabled",
        "algorithm",
        "limit",
        "window_seconds",
        "burst",
        "priority",
        "scope",
        "rules",
        "adaptive",
        "provider_dimension",
    }
    for field, value in changes.items():
        if value is None and field in non_nullable:
            raise HTTPException(422, f"'{field}' cannot be null")
        setattr(row, field, value)
    _validate_bounds(row)
    audit.record(
        session,
        principal,
        "policy.update",
        resource_type="policy",
        resource_id=row.id,
        detail={"name": row.name, "changes": changes},
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(row)
    await cache.load()
    await publish_invalidation(redis)
    return (await _with_live_state(redis, [row]))[0]


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: str,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    engine: EngineDep,
    request: Request,
) -> None:
    row = await _get(session, policy_id)
    _require_access(principal, row)
    await session.delete(row)
    audit.record(
        session,
        principal,
        "policy.delete",
        resource_type="policy",
        resource_id=policy_id,
        detail={"name": row.name},
        ip=client_ip(request),
    )
    await session.commit()
    await cache.load()
    await publish_invalidation(redis)
    await engine.reset(policy_id)
    await redis.unlink(
        keys.multiplier_key(policy_id),
        keys.pause_key(policy_id),
        keys.adaptive_state_key(policy_id),
        keys.instances_key(policy_id),
    )


@router.post("/policies/{policy_id}/reset", summary="Clear all limiter state and any pause for this policy")
async def reset_policy(
    policy_id: str,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    engine: EngineDep,
    request: Request,
) -> dict[str, int]:
    row = await _get(session, policy_id)
    _require_access(principal, row)
    removed = await engine.reset(policy_id)
    await redis.unlink(keys.pause_key(policy_id))
    audit.record(
        session,
        principal,
        "policy.reset",
        resource_type="policy",
        resource_id=policy_id,
        detail={"name": row.name, "keys_removed": removed},
        ip=client_ip(request),
    )
    await session.commit()
    return {"keys_removed": removed}


@router.get("/algorithms", tags=["Algorithms"])
async def list_algorithms(_: ReadPrincipal, cache: CacheDep) -> list[dict[str, Any]]:
    usage = Counter(policy.algorithm for policy in cache.policies.values())
    return [
        {
            "id": algorithm.value,
            **ALGORITHM_INFO[algorithm],
            "policies": usage.get(algorithm, 0),
            "policy_names": sorted(p.name for p in cache.policies.values() if p.algorithm is algorithm),
        }
        for algorithm in Algorithm
    ]

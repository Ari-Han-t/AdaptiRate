"""API key management. The secret is returned once, at creation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select

from ..deps import CacheDep, ManagePrincipal, ReadPrincipal, RedisDep, SessionDep, client_ip
from ..limiter import keys as redis_keys
from ..limiter.cache import publish_invalidation
from ..models import ApiKey, Policy
from ..schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from ..security import new_api_key
from ..services import audit

router = APIRouter(prefix="/keys", tags=["API Keys"])

USAGE_DAYS = 30


def _days(count: int) -> list[str]:
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(count)]


async def _usage(redis: Redis, key_ids: list[str]) -> dict[str, list[int]]:
    """Requests per day for each key, most recent day first."""
    if not key_ids:
        return {}
    pipe = redis.pipeline(transaction=False)
    days = _days(USAGE_DAYS)
    for day in days:
        pipe.hmget(redis_keys.key_usage_key(day), key_ids)
    pipe.hmget(redis_keys.key_last_used_key(), key_ids)
    raw = await pipe.execute()
    usage: dict[str, list[int]] = {key_id: [] for key_id in key_ids}
    for per_day in raw[:-1]:
        for key_id, value in zip(key_ids, per_day, strict=True):
            usage[key_id].append(int(value) if value else 0)
    return usage


async def _decorate(redis: Redis, rows: list[ApiKey]) -> list[ApiKeyOut]:
    usage = await _usage(redis, [row.id for row in rows])
    last_used = await redis.hmget(redis_keys.key_last_used_key(), [row.id for row in rows]) if rows else []
    result: list[ApiKeyOut] = []
    for row, seen in zip(rows, last_used, strict=True):
        out = ApiKeyOut.model_validate(row)
        days = usage.get(row.id, [])
        out.requests_today = days[0] if days else 0
        out.requests_30d = sum(days)
        if seen:
            out.last_used_at = datetime.fromtimestamp(int(seen), UTC)
        result.append(out)
    return result


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(_: ReadPrincipal, session: SessionDep, redis: RedisDep) -> list[ApiKeyOut]:
    rows = list((await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all())
    return await _decorate(redis, rows)


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> ApiKeyCreated:
    # A key may not grant more than its creator holds.
    excess = set(body.scopes) - principal.scopes
    if excess:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You cannot grant: {', '.join(sorted(excess))}")
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise HTTPException(422, "expires_at must be in the future")
    if body.policy_ids:
        found = set(
            (await session.execute(select(Policy.id).where(Policy.id.in_(body.policy_ids)))).scalars().all()
        )
        missing = set(body.policy_ids) - found
        if missing:
            raise HTTPException(422, f"Unknown policy ids: {sorted(missing)}")

    material = new_api_key()
    row = ApiKey(
        name=body.name,
        prefix=material.prefix,
        key_hash=material.key_hash,
        scopes=sorted(set(body.scopes)),
        policy_ids=sorted(set(body.policy_ids)),
        created_by=principal.id,
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.flush()
    audit.record(
        session,
        principal,
        "api_key.create",
        resource_type="api_key",
        resource_id=row.id,
        detail={"name": row.name, "scopes": row.scopes, "prefix": row.prefix},
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(row)
    await cache.load()
    await publish_invalidation(redis)
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), secret=material.secret)


@router.post("/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_key(
    key_id: str,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> ApiKeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        audit.record(
            session,
            principal,
            "api_key.revoke",
            resource_type="api_key",
            resource_id=row.id,
            detail={"name": row.name, "prefix": row.prefix},
            ip=client_ip(request),
        )
        await session.commit()
        await session.refresh(row)
        await cache.load()
        await publish_invalidation(redis)
    return (await _decorate(redis, [row]))[0]


@router.get("/{key_id}/usage")
async def key_usage(key_id: str, _: ReadPrincipal, session: SessionDep, redis: RedisDep) -> dict[str, object]:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    usage = (await _usage(redis, [key_id]))[key_id]
    days = _days(USAGE_DAYS)
    series = [
        {"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "requests": count}
        for day, count in zip(reversed(days), reversed(usage), strict=True)
    ]
    return {"key_id": key_id, "total": sum(usage), "daily": series}

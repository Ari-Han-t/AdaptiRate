"""Runtime configuration and provider management."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from ..deps import AdminPrincipal, CacheDep, ManagePrincipal, ReadPrincipal, RedisDep, SessionDep, client_ip
from ..limiter import keys
from ..limiter.cache import publish_invalidation
from ..models import ProviderConfig, Setting
from ..providers.parsers import BUILTIN
from ..runtime_config import RUNTIME_SETTING_KEY, RuntimeConfig
from ..schemas import CustomProviderIn
from ..services import audit, metrics

router = APIRouter()


@router.get("/config", response_model=RuntimeConfig, tags=["Configuration"])
async def get_config(_: ReadPrincipal, cache: CacheDep) -> RuntimeConfig:
    return cache.config


@router.put("/config", response_model=RuntimeConfig, tags=["Configuration"])
async def put_config(
    body: RuntimeConfig,
    principal: AdminPrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> RuntimeConfig:
    value = body.model_dump(mode="json")
    setting = await session.get(Setting, RUNTIME_SETTING_KEY)
    if setting is None:
        session.add(Setting(key=RUNTIME_SETTING_KEY, value=value))
    else:
        setting.value = value
    audit.record(
        session, principal, "config.update", resource_type="config", detail=value, ip=client_ip(request)
    )
    await session.commit()
    await cache.load()
    await publish_invalidation(redis)
    return cache.config


@router.get("/providers", tags=["Providers"])
async def list_providers(_: ReadPrincipal, redis: RedisDep, cache: CacheDep) -> list[dict[str, Any]]:
    names = (
        set(BUILTIN)
        | set(cache.custom_providers)
        | {p.provider for p in cache.policies.values() if p.provider}
    )
    states = await metrics.provider_states(redis, names, time.time())
    for state in states:
        state["custom_headers"] = cache.custom_providers.get(state["name"])
        state["policies"] = sorted(p.name for p in cache.policies.values() if p.provider == state["name"])
    return states


@router.put(
    "/providers/{name}", tags=["Providers"], summary="Define or replace a custom provider's header map"
)
async def put_provider(
    name: str,
    body: CustomProviderIn,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> dict[str, Any]:
    if not name or len(name) > 64 or not all(ch.islower() or ch.isdigit() or ch in "_-" for ch in name):
        raise HTTPException(422, "Provider names use a-z, 0-9, '_' and '-'")
    header_map = body.model_dump(exclude_none=True)
    row = await session.get(ProviderConfig, name)
    if row is None:
        session.add(ProviderConfig(name=name, header_map=header_map))
    else:
        row.header_map = header_map
    audit.record(
        session,
        principal,
        "provider.update",
        resource_type="provider",
        resource_id=name,
        detail=header_map,
        ip=client_ip(request),
    )
    await session.commit()
    await cache.load()
    await publish_invalidation(redis)
    return {"name": name, "custom_headers": header_map}


@router.delete("/providers/{name}", status_code=status.HTTP_204_NO_CONTENT, tags=["Providers"])
async def delete_provider(
    name: str,
    principal: ManagePrincipal,
    session: SessionDep,
    redis: RedisDep,
    cache: CacheDep,
    request: Request,
) -> None:
    row = await session.get(ProviderConfig, name)
    if row is not None:
        await session.delete(row)
    audit.record(
        session,
        principal,
        "provider.reset",
        resource_type="provider",
        resource_id=name,
        ip=client_ip(request),
    )
    await session.commit()
    # Also forget observed quota and lift any pause it caused.
    pause_keys = [keys.pause_key(p.id) for p in cache.policies.values() if p.provider == name]
    await redis.unlink(keys.provider_state_key(name), *pause_keys)
    await cache.load()
    await publish_invalidation(redis)

"""FastAPI dependencies: shared services and authentication."""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import get_session
from .limiter.cache import RuntimeCache
from .limiter.engine import LimiterEngine
from .security import API_KEY_PREFIX, TokenError, decode_access_token, sha256_hex
from .services.events import EventBuffer

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": frozenset({"read", "manage", "admin", "evaluate", "report"}),
    "developer": frozenset({"read", "manage", "evaluate", "report"}),
    "viewer": frozenset({"read"}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    kind: str  # "user" or "api_key"
    id: str
    label: str
    scopes: frozenset[str]
    role: str | None = None
    policy_ids: frozenset[str] = frozenset()

    def can_use_policy(self, policy_id: str) -> bool:
        return not self.policy_ids or policy_id in self.policy_ids


def get_redis_dep(request: Request) -> Redis:
    return request.app.state.redis  # type: ignore[no-any-return]


def get_cache(request: Request) -> RuntimeCache:
    return request.app.state.cache  # type: ignore[no-any-return]


def get_engine_dep(request: Request) -> LimiterEngine:
    return request.app.state.engine  # type: ignore[no-any-return]


def get_events(request: Request) -> EventBuffer:
    return request.app.state.events  # type: ignore[no-any-return]


RedisDep = Annotated[Redis, Depends(get_redis_dep)]
CacheDep = Annotated[RuntimeCache, Depends(get_cache)]
EngineDep = Annotated[LimiterEngine, Depends(get_engine_dep)]
EventsDep = Annotated[EventBuffer, Depends(get_events)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Missing or invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _credential(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return request.headers.get("x-api-key")


def authenticate(request: Request) -> Principal:
    """Resolve the caller from a dashboard JWT or an API key. No database access."""
    credential = _credential(request)
    if not credential:
        raise _UNAUTHENTICATED

    if credential.startswith(API_KEY_PREFIX):
        cache: RuntimeCache = request.app.state.cache
        key = cache.api_keys.get(sha256_hex(credential))
        if key is None or key.is_expired(time.time()):
            raise _UNAUTHENTICATED
        return Principal(
            kind="api_key", id=key.id, label=key.name, scopes=key.scopes, policy_ids=key.policy_ids
        )

    try:
        payload = decode_access_token(credential)
    except TokenError:
        raise _UNAUTHENTICATED from None
    role = str(payload.get("role", ""))
    return Principal(
        kind="user",
        id=str(payload["sub"]),
        label=str(payload.get("email", "")),
        scopes=ROLE_SCOPES.get(role, frozenset()),
        role=role,
    )


def require(scope: str) -> Callable[[Request], Awaitable[Principal]]:
    async def dependency(request: Request) -> Principal:
        principal = authenticate(request)
        if scope not in principal.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"This action needs the '{scope}' permission")
        request.state.principal = principal
        return principal

    return dependency


ReadPrincipal = Annotated[Principal, Depends(require("read"))]
ManagePrincipal = Annotated[Principal, Depends(require("manage"))]
AdminPrincipal = Annotated[Principal, Depends(require("admin"))]
EvaluatePrincipal = Annotated[Principal, Depends(require("evaluate"))]
ReportPrincipal = Annotated[Principal, Depends(require("report"))]


def client_ip(request: Request) -> str:
    """The caller's address. X-Forwarded-For is honoured only when the direct peer is
    a configured trusted proxy; otherwise anyone could spoof their way past a limit."""
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    networks = request.app.state.trusted_networks
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in networks):
        return peer
    # Walk right to left, skipping our own proxies; the first foreign hop is the client.
    for hop in reversed([part.strip() for part in forwarded.split(",")]):
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not any(address in network for network in networks):
            return hop
    return peer

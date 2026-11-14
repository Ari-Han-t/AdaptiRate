"""Decision log and audit log: search, filter, paginate, export."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

import orjson
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, select

from ..db import session_factory
from ..deps import AdminPrincipal, ReadPrincipal, SessionDep
from ..models import AuditLog, DecisionLog

router = APIRouter(tags=["Logs"])

EXPORT_MAX_ROWS = 100_000
EXPORT_PAGE = 2000
COLUMNS = ("id", "ts", "policy", "subject", "allowed", "reason", "limit", "remaining", "cost", "api_key_id")


def _like(term: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filtered(
    *,
    q: str | None,
    policy: str | None,
    allowed: bool | None,
    reason: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Select[tuple[DecisionLog]]:
    query = select(DecisionLog)
    if q:
        query = query.where(DecisionLog.subject.ilike(_like(q), escape="\\"))
    if policy:
        query = query.where(DecisionLog.policy_name == policy)
    if allowed is not None:
        query = query.where(DecisionLog.allowed.is_(allowed))
    if reason:
        query = query.where(DecisionLog.reason == reason)
    if since:
        query = query.where(DecisionLog.ts >= since)
    if until:
        query = query.where(DecisionLog.ts <= until)
    return query


def _row(log: DecisionLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "ts": log.ts.isoformat(),
        "policy": log.policy_name,
        "subject": log.subject,
        "allowed": log.allowed,
        "reason": log.reason,
        "limit": log.limit,
        "remaining": log.remaining,
        "cost": log.cost,
        "api_key_id": log.api_key_id,
    }


@router.get("/logs")
async def list_logs(
    _: ReadPrincipal,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    policy: Annotated[str | None, Query(max_length=100)] = None,
    allowed: bool | None = None,
    reason: Annotated[str | None, Query(max_length=16)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    query = _filtered(q=q, policy=policy, allowed=allowed, reason=reason, since=since, until=until)
    if cursor is not None:
        query = query.where(DecisionLog.id < cursor)
    rows = list(
        (await session.execute(query.order_by(DecisionLog.id.desc()).limit(limit + 1))).scalars().all()
    )
    page = rows[:limit]
    return {
        "items": [_row(row) for row in page],
        "next_cursor": page[-1].id if len(rows) > limit else None,
    }


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection: subjects are caller-controlled text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@router.get("/logs/export")
async def export_logs(
    _: ReadPrincipal,
    format: Literal["csv", "ndjson"] = "csv",
    q: Annotated[str | None, Query(max_length=200)] = None,
    policy: Annotated[str | None, Query(max_length=100)] = None,
    allowed: bool | None = None,
    reason: Annotated[str | None, Query(max_length=16)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StreamingResponse:
    base = _filtered(q=q, policy=policy, allowed=allowed, reason=reason, since=since, until=until)

    async def rows() -> AsyncIterator[bytes]:
        if format == "csv":
            yield (",".join(COLUMNS) + "\r\n").encode()
        cursor: int | None = None
        sent = 0
        # Own session: the request-scoped one closes before a streamed body finishes.
        async with session_factory()() as session:
            while sent < EXPORT_MAX_ROWS:
                query = base if cursor is None else base.where(DecisionLog.id < cursor)
                page = list(
                    (await session.execute(query.order_by(DecisionLog.id.desc()).limit(EXPORT_PAGE)))
                    .scalars()
                    .all()
                )
                if not page:
                    return
                cursor = page[-1].id
                sent += len(page)
                if format == "csv":
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
                    for log in page:
                        record = _row(log)
                        writer.writerow([_csv_safe(record[column]) for column in COLUMNS])
                    yield buffer.getvalue().encode()
                else:
                    yield b"".join(orjson.dumps(_row(log)) + b"\n" for log in page)

    media_type = "text/csv" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        rows(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="adaptirate-logs.{format}"'},
    )


@router.get("/audit", summary="Management actions (admin only)")
async def list_audit(
    _: AdminPrincipal,
    session: SessionDep,
    action: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action.like(_like(action).lstrip("%"), escape="\\"))
    if cursor is not None:
        query = query.where(AuditLog.id < cursor)
    rows = list((await session.execute(query.order_by(AuditLog.id.desc()).limit(limit + 1))).scalars().all())
    page = rows[:limit]
    return {
        "items": [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "actor_type": row.actor_type,
                "actor": row.actor_label,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "detail": row.detail,
                "ip": row.ip,
            }
            for row in page
        ],
        "next_cursor": page[-1].id if len(rows) > limit else None,
    }

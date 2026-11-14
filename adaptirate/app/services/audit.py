"""Audit trail of every management action."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal
from ..models import AuditLog


def record(
    session: AsyncSession,
    principal: Principal | None,
    action: str,
    *,
    resource_type: str = "",
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    label: str | None = None,
) -> None:
    """Add an audit row to the caller's transaction, so it commits or rolls back with the change."""
    session.add(
        AuditLog(
            actor_type=principal.kind if principal else "anonymous",
            actor_id=principal.id if principal else None,
            actor_label=(principal.label if principal else label) or "",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail or {},
            ip=ip,
        )
    )

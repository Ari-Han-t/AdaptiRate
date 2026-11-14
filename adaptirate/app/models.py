"""Database tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    algorithm: Mapped[str] = mapped_column(String(32), default="token_bucket")
    limit: Mapped[int] = mapped_column(Integer)
    window_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    burst: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=50)

    # Which request attribute identifies a bucket: global, user, ip, api_key, role, custom.
    scope: Mapped[str] = mapped_column(String(16), default="custom")
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    adaptive: Mapped[bool] = mapped_column(Boolean, default=True)
    min_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_dimension: Mapped[str] = mapped_column(String(16), default="requests")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(24), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Empty list means every policy.
    policy_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(32), default="")
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DecisionLog(Base):
    __tablename__ = "decision_logs"
    __table_args__ = (
        Index("ix_decision_logs_policy_ts", "policy_name", "ts"),
        Index("ix_decision_logs_allowed_ts", "allowed", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    policy_id: Mapped[str] = mapped_column(String(36))
    policy_name: Mapped[str] = mapped_column(String(100))
    subject: Mapped[str] = mapped_column(String(300))
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(16))
    limit: Mapped[int] = mapped_column(Integer)
    remaining: Mapped[int] = mapped_column(Integer)
    cost: Mapped[int] = mapped_column(Integer)
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MetricRollup(Base):
    """One row per minute per policy. policy_id '' is the all-policy total."""

    __tablename__ = "metric_rollups"

    ts_minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(36), primary_key=True, default="")
    allowed: Mapped[int] = mapped_column(BigInteger, default=0)
    blocked: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_health: Mapped[float | None] = mapped_column(Float, nullable=True)
    upstream_requests: Mapped[int] = mapped_column(BigInteger, default=0)
    upstream_errors: Mapped[int] = mapped_column(BigInteger, default=0)
    upstream_latency_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    upstream_latency_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderConfig(Base):
    """A custom upstream provider: which response headers carry its quota."""

    __tablename__ = "provider_configs"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    header_map: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

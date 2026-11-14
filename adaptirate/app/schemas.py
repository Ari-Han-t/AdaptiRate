"""Request and response bodies. Everything entering the API is validated here."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .limiter.engine import Algorithm
from .limiter.policy import RULE_ATTRIBUTES, RuleAction, Scope

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[\w.:\-]+$")
]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=512)]
# Shape check only. Deliverability is not our concern, and strict validators reject
# legitimate internal domains such as .local and .internal.
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Role = Literal["admin", "developer", "viewer"]
KeyScope = Literal["evaluate", "report", "read", "manage"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- auth -----------------------------------------------------------------


class LoginRequest(Strict):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int
    user: UserOut


class UserCreate(Strict):
    email: Email
    password: Annotated[str, StringConstraints(min_length=12, max_length=256)]
    role: Role = "viewer"


class UserUpdate(Strict):
    password: Annotated[str, StringConstraints(min_length=12, max_length=256)] | None = None
    role: Role | None = None
    is_active: bool | None = None


# ---- policies -------------------------------------------------------------


class RuleIn(Strict):
    attribute: Literal["user", "ip", "api_key", "role", "key"]
    values: list[Annotated[str, StringConstraints(min_length=1, max_length=256)]] = Field(
        min_length=1, max_length=1000
    )
    action: RuleAction = RuleAction.LIMIT
    priority: int = Field(default=0, ge=-1000, le=1000)
    limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    window_seconds: float | None = Field(default=None, gt=0, le=31 * 86400)
    burst: int | None = Field(default=None, ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def _check(self) -> RuleIn:
        if self.attribute not in RULE_ATTRIBUTES:
            raise ValueError("unknown attribute")
        if self.attribute == "ip":
            for value in self.values:
                try:
                    ipaddress.ip_network(value, strict=False)
                except ValueError as exc:
                    raise ValueError(f"'{value}' is not an IP address or CIDR range") from exc
        if self.action is RuleAction.LIMIT and self.limit is None:
            raise ValueError("a 'limit' rule needs a limit")
        return self


class PolicyBase(Strict):
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    limit: int = Field(ge=1, le=1_000_000_000)
    window_seconds: float = Field(default=60, gt=0, le=31 * 86400)
    burst: int = Field(default=0, ge=0, le=1_000_000_000)
    priority: int = Field(default=50, ge=0, le=100)
    scope: Scope = Scope.CUSTOM
    rules: list[RuleIn] = Field(default_factory=list, max_length=200)
    adaptive: bool = True
    min_limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    target_latency_ms: float | None = Field(default=None, gt=0, le=600_000)
    provider: (
        Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")] | None
    ) = None
    provider_dimension: Literal["requests", "tokens"] = "requests"


def _check_bounds(limit: int | None, min_limit: int | None, max_limit: int | None) -> None:
    if limit is None:
        return
    if min_limit is not None and min_limit > limit:
        raise ValueError("min_limit cannot exceed limit")
    if max_limit is not None and max_limit < limit:
        raise ValueError("max_limit cannot be below limit")


class PolicyCreate(PolicyBase):
    name: Name

    @model_validator(mode="after")
    def _bounds(self) -> PolicyCreate:
        _check_bounds(self.limit, self.min_limit, self.max_limit)
        return self


class PolicyUpdate(Strict):
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    algorithm: Algorithm | None = None
    limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    window_seconds: float | None = Field(default=None, gt=0, le=31 * 86400)
    burst: int | None = Field(default=None, ge=0, le=1_000_000_000)
    priority: int | None = Field(default=None, ge=0, le=100)
    scope: Scope | None = None
    rules: list[RuleIn] | None = Field(default=None, max_length=200)
    adaptive: bool | None = None
    min_limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_limit: int | None = Field(default=None, ge=1, le=1_000_000_000)
    target_latency_ms: float | None = Field(default=None, gt=0, le=600_000)
    provider: (
        Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")] | None
    ) = None
    provider_dimension: Literal["requests", "tokens"] | None = None


class PolicyOut(PolicyBase):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    # Live state, filled from Redis.
    effective_limit: int | None = None
    multiplier: float | None = None
    health: float | None = None
    paused_ms: int = 0


# ---- API keys -------------------------------------------------------------


class ApiKeyCreate(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scopes: list[KeyScope] = Field(default=["evaluate", "report"], min_length=1)
    policy_ids: list[str] = Field(default_factory=list, max_length=500)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str
    scopes: list[str]
    policy_ids: list[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    requests_today: int = 0
    requests_30d: int = 0


class ApiKeyCreated(ApiKeyOut):
    # Shown exactly once; only its hash is stored.
    secret: str


# ---- evaluate -------------------------------------------------------------


class Subject(Strict):
    key: Identifier | None = None
    user: Identifier | None = None
    ip: Identifier | None = None
    api_key: Identifier | None = None
    role: Identifier | None = None

    def attrs(self) -> dict[str, str]:
        return {
            name: value
            for name, value in (
                ("user", self.user),
                ("ip", self.ip),
                ("api_key", self.api_key),
                ("role", self.role),
            )
            if value is not None
        }


class EvaluateRequest(Subject):
    policy: Name
    cost: int = Field(default=1, ge=1, le=1_000_000)


class BatchEvaluateRequest(Strict):
    checks: list[EvaluateRequest] = Field(min_length=1, max_length=500)


class EvaluateResponse(BaseModel):
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int
    reset_after_ms: int
    reason: str
    policy: str
    # The window the limit applies over, so an SDK's offline fallback need not guess it.
    window_ms: int


class BatchEvaluateResponse(BaseModel):
    results: list[EvaluateResponse]


class LeaseRequest(Subject):
    policy: Name
    tokens: int = Field(ge=1, le=1_000_000)


class LeaseResponse(EvaluateResponse):
    granted: int
    # Unspent tokens must be discarded after this long.
    ttl_ms: int


class ReportRequest(Strict):
    """What a protected service observed since its last report."""

    policy: Name
    instance_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    requests: int = Field(default=0, ge=0, le=100_000_000)
    errors: int = Field(default=0, ge=0, le=100_000_000)
    throttled: int = Field(default=0, ge=0, le=100_000_000)
    timeouts: int = Field(default=0, ge=0, le=100_000_000)
    latency_sum_ms: float = Field(default=0, ge=0)
    latency_buckets: list[Annotated[int, Field(ge=0)]] | None = None
    in_flight: int | None = Field(default=None, ge=0)
    queue_depth: int | None = Field(default=None, ge=0)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_percent: float | None = Field(default=None, ge=0, le=100)


class ProviderReport(Strict):
    """Status and headers of one upstream provider response."""

    provider: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")]
    status: int = Field(ge=100, le=599)
    headers: dict[
        Annotated[str, StringConstraints(max_length=128)], Annotated[str, StringConstraints(max_length=1024)]
    ] = Field(default_factory=dict, max_length=100)


class CustomProviderIn(Strict):
    """Header names (case-insensitive) that carry a custom provider's quota."""

    remaining_requests: str | None = Field(default=None, max_length=128)
    limit_requests: str | None = Field(default=None, max_length=128)
    reset_requests: str | None = Field(default=None, max_length=128)
    remaining_tokens: str | None = Field(default=None, max_length=128)
    limit_tokens: str | None = Field(default=None, max_length=128)
    reset_tokens: str | None = Field(default=None, max_length=128)
    # How reset headers are expressed.
    reset_format: Literal["seconds", "epoch", "duration", "rfc3339"] = "seconds"
    # "continuous": the quota refills steadily and the reset header says when it is full
    # again (OpenAI-style). "window": nothing comes back until the reset (GitHub-style).
    refill: Literal["continuous", "window"] = "continuous"


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None

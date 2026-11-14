"""Runtime configuration. Every value comes from an AR_-prefixed environment variable."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AR_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://adaptirate:adaptirate@localhost:5432/adaptirate"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: SecretStr
    access_token_ttl_seconds: int = Field(default=900, ge=60)
    refresh_token_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=300)
    cookie_secure: bool = False

    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # When set, GET /metrics requires "Authorization: Bearer <token>".
    metrics_token: SecretStr | None = None

    cors_origins: CsvList = []
    trusted_proxies: CsvList = []

    # Decision event log: bounded so a traffic spike cannot exhaust memory or Redis.
    events_per_second: int = Field(default=100, ge=0)
    event_stream_maxlen: int = Field(default=100_000, ge=1000)
    cache_refresh_seconds: float = Field(default=30.0, gt=0)

    db_pool_size: int = Field(default=10, ge=1)
    redis_max_connections: int = Field(default=200, ge=10)

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("AR_JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("bootstrap_admin_email", "bootstrap_admin_password", "metrics_token", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()

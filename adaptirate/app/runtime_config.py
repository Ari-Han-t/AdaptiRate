"""Settings that can be changed at runtime through the API, stored in Postgres."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AdaptiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    signal_window_seconds: int = Field(default=5, ge=1, le=60)

    # Health score thresholds (0-100).
    decrease_threshold: float = Field(default=60, ge=1, le=99)
    increase_threshold: float = Field(default=80, ge=1, le=100)

    # Multiplicative decrease: at health 0 the limit is cut by max_decrease.
    max_decrease: float = Field(default=0.5, gt=0, lt=1)
    decrease_cooldown_seconds: float = Field(default=2.0, ge=0)
    # Additive increase per tick, as a fraction of the policy's base limit.
    increase_step: float = Field(default=0.05, gt=0, le=1)

    default_target_latency_ms: float = Field(default=500, gt=0)
    latency_critical_factor: float = Field(default=3.0, gt=1)
    error_rate_ok: float = Field(default=0.01, ge=0, lt=1)
    error_rate_critical: float = Field(default=0.2, gt=0, le=1)
    throttle_rate_ok: float = Field(default=0.0, ge=0, lt=1)
    throttle_rate_critical: float = Field(default=0.1, gt=0, le=1)
    cpu_ok: float = Field(default=70, ge=0, lt=100)
    cpu_critical: float = Field(default=95, gt=0, le=100)
    memory_ok: float = Field(default=85, ge=0, lt=100)
    memory_critical: float = Field(default=97, gt=0, le=100)
    queue_ok: float = Field(default=10, ge=0)
    queue_critical: float = Field(default=100, gt=0)

    # Fall back to the AdaptiRate host's own CPU and memory when no SDK reports them.
    use_host_signals: bool = True
    # Keep this share of a provider's remaining quota unspent.
    provider_safety_margin: float = Field(default=0.1, ge=0, lt=1)

    @model_validator(mode="after")
    def _ordered(self) -> AdaptiveConfig:
        pairs = [
            ("decrease_threshold", "increase_threshold"),
            ("error_rate_ok", "error_rate_critical"),
            ("throttle_rate_ok", "throttle_rate_critical"),
            ("cpu_ok", "cpu_critical"),
            ("memory_ok", "memory_critical"),
            ("queue_ok", "queue_critical"),
        ]
        for low, high in pairs:
            if getattr(self, low) >= getattr(self, high):
                raise ValueError(f"{low} must be below {high}")
        return self


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_blocked: bool = True
    # Share of allowed decisions written to the event log (blocked ones are always kept
    # while log_blocked is on, up to the per-process rate cap). Counters and analytics
    # are exact regardless; this only thins the searchable log.
    sample_allowed: float = Field(default=0.1, ge=0, le=1)
    retention_days: int = Field(default=7, ge=1, le=365)
    # Hard cap on stored log rows; the oldest are removed first.
    max_rows: int = Field(default=1_000_000, ge=1000, le=1_000_000_000)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adaptive: AdaptiveConfig = AdaptiveConfig()
    logging: LoggingConfig = LoggingConfig()


RUNTIME_SETTING_KEY = "runtime"

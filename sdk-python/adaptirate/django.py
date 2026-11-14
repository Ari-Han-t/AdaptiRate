"""Django middleware.

settings.py:

    MIDDLEWARE = ["adaptirate.django.AdaptiRateMiddleware", ...]
    ADAPTIRATE = {
        "API_KEY": os.environ["ADAPTIRATE_API_KEY"],
        "URL": "http://localhost:8080",
        "POLICY": "api",
        "EXCLUDE": ("/health",),          # optional
        "IDENTIFY": "myapp.limits.identify",  # optional dotted path: request -> dict
    }
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any

from .client import AdaptiveLimiter


def _default_identify(request: Any) -> Mapping[str, Any]:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return {"user": str(user.pk), "ip": request.META.get("REMOTE_ADDR") or "unknown"}
    return {"ip": request.META.get("REMOTE_ADDR") or "unknown"}


def _load(path: str) -> Callable[[Any], Mapping[str, Any]]:
    module, _, attribute = path.rpartition(".")
    return getattr(import_module(module), attribute)  # type: ignore[no-any-return]


class AdaptiRateMiddleware:
    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured

        config: dict[str, Any] = getattr(settings, "ADAPTIRATE", {})
        if "POLICY" not in config:
            raise ImproperlyConfigured("settings.ADAPTIRATE['POLICY'] is required")
        self.get_response = get_response
        self.policy: str = config["POLICY"]
        self.exclude: tuple[str, ...] = tuple(config.get("EXCLUDE", ()))
        identify = config.get("IDENTIFY")
        self.identify = _load(identify) if isinstance(identify, str) else (identify or _default_identify)
        self.limiter = AdaptiveLimiter(
            api_key=config.get("API_KEY"),
            base_url=config.get("URL"),
            fail_mode=config.get("FAIL_MODE", "open"),
            lease_size=config.get("LEASE_SIZE", 0),
            timeout=config.get("TIMEOUT", 1.0),
        )

    def __call__(self, request: Any) -> Any:
        from django.http import JsonResponse

        if any(request.path.startswith(prefix) for prefix in self.exclude):
            return self.get_response(request)

        decision = self.limiter.check(self.policy, **dict(self.identify(request)))
        if not decision.allowed:
            response = JsonResponse(
                {
                    "detail": "Rate limit exceeded",
                    "policy": decision.policy,
                    "reason": decision.reason,
                    "retry_after_ms": decision.retry_after_ms,
                },
                status=429,
            )
            for name, value in decision.headers().items():
                response[name] = value
            return response

        started = time.perf_counter()
        status = 500
        try:
            response = self.get_response(request)
            status = response.status_code
            for name, value in decision.headers().items():
                response[name] = value
            return response
        finally:
            self.limiter.record(self.policy, (time.perf_counter() - started) * 1000, status=status)

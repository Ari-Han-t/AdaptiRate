"""An existing Flask service with AdaptiRate added.

    pip install flask ../../sdk-python
    ADAPTIRATE_API_KEY=ar_... ADAPTIRATE_URL=http://localhost:8080 flask --app app run

Needs a policy named "flask-api" scoped "Per user" (create it in the dashboard).
"""

from __future__ import annotations

import atexit
from typing import Any

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from adaptirate import AdaptiveLimiter, RateLimitExceeded
from adaptirate.wsgi import AdaptiRateWSGIMiddleware

app = Flask(__name__)
limiter = AdaptiveLimiter(fail_mode="local")
atexit.register(limiter.close)  # send pending latency reports on shutdown


def identify(environ: dict[str, Any]) -> dict[str, str]:
    """Who is calling. Use whatever your auth layer establishes; here, a header."""
    address = environ.get("REMOTE_ADDR") or "unknown"
    return {"user": environ.get("HTTP_X_USER") or f"ip:{address}", "ip": address}


# Option 1: limit every request.
app.wsgi_app = AdaptiRateWSGIMiddleware(  # type: ignore[method-assign]
    app.wsgi_app, limiter=limiter, policy="flask-api", identify=identify, exclude=("/health",)
)
# Behind one reverse proxy, apply ProxyFix outside the middleware so REMOTE_ADDR is the client.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # type: ignore[method-assign]


@app.get("/health")
def health() -> Any:
    return {"status": "ok"}


@app.get("/products")
def products() -> Any:
    return jsonify([{"id": 1, "name": "Keyboard"}])


# Option 2: limit one expensive operation, keyed however you like.
@app.post("/reports/<account>")
def build_report(account: str) -> Any:
    try:
        limiter.acquire("reports", key=account)
    except RateLimitExceeded as exceeded:
        return {"detail": "Too many reports"}, 429, exceeded.decision.headers()
    with limiter.track("reports"):
        return {"account": account, "status": "queued"}

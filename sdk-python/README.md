# adaptirate

Python SDK for [AdaptiRate](https://github.com/Ari-Han-t/AdaptiRate), a self-hosted rate limiter
whose limits are shared across every process and adapt to latency, errors and upstream API
quotas.

You need a running AdaptiRate server; see the
[main repository](https://github.com/Ari-Han-t/AdaptiRate) to start one with Docker Compose.

```bash
pip install adaptirate
```

Set the server address and an API key (created on the dashboard's API Keys page):

```bash
export ADAPTIRATE_URL=http://localhost:8080
export ADAPTIRATE_API_KEY=ar_...
```

## Limit a whole app

FastAPI, Starlette or any ASGI app:

```python
from adaptirate import AsyncAdaptiveLimiter
from adaptirate.asgi import AdaptiRateMiddleware

limiter = AsyncAdaptiveLimiter()
app.add_middleware(AdaptiRateMiddleware, limiter=limiter, policy="api")
```

Flask or any WSGI app:

```python
from adaptirate import AdaptiveLimiter
from adaptirate.wsgi import AdaptiRateWSGIMiddleware

limiter = AdaptiveLimiter()
app.wsgi_app = AdaptiRateWSGIMiddleware(app.wsgi_app, limiter=limiter, policy="api")
```

Django: add `"adaptirate.django.AdaptiRateMiddleware"` to `MIDDLEWARE` and an `ADAPTIRATE`
settings dict with `API_KEY`, `URL` and `POLICY`.

By default callers are identified by their address. Pass `identify=` to use a user id, tenant or
API key instead. Blocked requests get `429` with `Retry-After` and `X-RateLimit-*` headers, and
every request's latency and status is reported so the limit can adapt.

## Limit one operation

```python
from adaptirate import AdaptiveLimiter, RateLimitExceeded

limiter = AdaptiveLimiter()

decision = limiter.check("send-email", user=user_id)
if not decision.allowed:
    ...  # decision.retry_after_ms says when to try again

limiter.acquire("send-email", user=user_id)   # raises RateLimitExceeded instead
limiter.wait("send-email", user=user_id)      # sleeps until allowed

@limiter.limit("reports", subject=lambda account: {"key": account})
def build_report(account): ...
```

`AsyncAdaptiveLimiter` has the same methods, awaitable.

## Pace calls to an upstream API

Bind a policy to a provider on the server, then report each response:

```python
import httpx

limiter = AdaptiveLimiter()
client = httpx.Client(event_hooks={"response": [limiter.httpx_hook("openai")]})

limiter.wait("openai-calls", key="my-service")
client.post("https://api.openai.com/v1/chat/completions", ...)
```

AdaptiRate reads the provider's rate-limit headers, spreads the remaining quota across every
process using the policy, and pauses them all when the provider answers 429.

## Options

| Option | Default | Meaning |
|---|---|---|
| `fail_mode` | `"open"` | When the server is unreachable: `"open"` allows, `"closed"` denies, `"local"` keeps enforcing the last known limit in process |
| `lease_size` | `0` | Fetch this many permits at once and decide locally; for very hot paths |
| `timeout` | `1.0` | Seconds per request to the server |
| `report_interval` | `1.0` | Seconds between latency and error reports; `0` disables the background reporter |

## License

MIT

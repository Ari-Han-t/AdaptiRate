# Django

Add AdaptiRate to an existing Django project in two steps.

```bash
pip install /path/to/AdaptiRate/sdk-python
```

**settings.py**

```python
import os

MIDDLEWARE = [
    # After AuthenticationMiddleware, so request.user is known.
    *MIDDLEWARE,
    "adaptirate.django.AdaptiRateMiddleware",
]

ADAPTIRATE = {
    "API_KEY": os.environ["ADAPTIRATE_API_KEY"],
    "URL": os.environ.get("ADAPTIRATE_URL", "http://localhost:8080"),
    "POLICY": "django-api",          # create it in the dashboard, scoped "Per user"
    "EXCLUDE": ("/health", "/static"),
    "FAIL_MODE": "local",            # keep enforcing the last known limit if AdaptiRate is down
}
```

The default identity is the logged-in user's primary key, falling back to `REMOTE_ADDR`. To
use something else (a tenant, an API key), point `IDENTIFY` at a function:

```python
# myapp/limits.py
def identify(request):
    return {"key": request.headers.get("X-Tenant", "anonymous"), "ip": request.META["REMOTE_ADDR"]}
```

```python
ADAPTIRATE["IDENTIFY"] = "myapp.limits.identify"
```

Behind a reverse proxy, make `REMOTE_ADDR` the real client address (for example with
`django-xff` or your server's proxy settings). The middleware never reads `X-Forwarded-For`
itself, because any client can set it.

For a single view instead of the whole site:

```python
from adaptirate import AdaptiveLimiter, RateLimitExceeded
from django.http import JsonResponse

limiter = AdaptiveLimiter()

def export(request):
    try:
        limiter.acquire("exports", user=str(request.user.pk))
    except RateLimitExceeded as exceeded:
        response = JsonResponse({"detail": "Too many exports"}, status=429)
        for name, value in exceeded.decision.headers().items():
            response[name] = value
        return response
    ...
```

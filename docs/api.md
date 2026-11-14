# API reference

All endpoints are under `/api/v1`. A running server also serves interactive documentation,
generated from the code, at `/api/v1/docs`.

## Authentication

Send one of:

- `X-API-Key: ar_...` or `Authorization: Bearer ar_...` for services (API keys).
- `Authorization: Bearer <access token>` for dashboard users (from `POST /auth/login`).

| Permission | Granted to | Allows |
|---|---|---|
| `evaluate` | Keys; admin and developer users | Decisions and leases |
| `report` | Keys; admin and developer users | Sending signals and provider responses |
| `read` | Keys; every user | Viewing policies, metrics, logs |
| `manage` | Keys; admin and developer users | Changing policies, keys, providers |
| `admin` | Admin users only | Users, runtime configuration, audit log |

A key cannot create another key with permissions it does not hold itself.

## Decisions

### `POST /evaluate`

```json
{ "policy": "checkout", "user": "u_42", "ip": "203.0.113.9", "role": "premium", "cost": 1 }
```

Send every attribute you know (`key`, `user`, `ip`, `api_key`, `role`). The policy's scope
decides which one identifies the bucket; rules can match on any of them. `cost` defaults to 1.

```json
{
  "allowed": false, "limit": 100, "remaining": 0,
  "retry_after_ms": 1840, "reset_after_ms": 12000,
  "reason": "limit", "policy": "checkout"
}
```

| `reason` | Meaning |
|---|---|
| `ok` | Allowed |
| `limit` | Over the limit; retry after `retry_after_ms` |
| `paused` | The policy's upstream provider asked for a backoff |
| `too_large` | `cost` exceeds what the policy could ever grant (`retry_after_ms` is -1) |
| `rule_allow`, `rule_deny` | Decided by an allow or deny rule |
| `disabled` | The policy is disabled, so everything passes |

Errors: `404` unknown policy, `403` key not permitted for this policy, `422` the request lacks
the attribute the policy is scoped by.

### `POST /evaluate/batch`

`{ "checks": [ ...up to 500 evaluate bodies... ] }` returns `{ "results": [ ... ] }` in the
same order, using one Redis round trip.

### `POST /lease`

An evaluate body with `tokens` instead of `cost`. The response adds `granted` (which may be
fewer than requested) and `ttl_ms` (discard unspent tokens after this long).

### `POST /report`

What a protected service observed since its last report. Sent by the SDKs once a second.

```json
{
  "policy": "checkout", "instance_id": "web-1:4312",
  "requests": 120, "errors": 2, "throttled": 0, "timeouts": 0,
  "latency_sum_ms": 5400.5, "latency_buckets": [0,0,3,40,60,12,4,1,0,0,0,0,0,0],
  "in_flight": 7, "queue_depth": 0, "cpu_percent": 41.5, "memory_percent": 63.0
}
```

`latency_buckets` has 14 counts for the upper bounds 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000,
2500, 5000, 10000 ms and one open-ended bucket.

### `POST /providers/report`

`{ "provider": "openai", "status": 429, "headers": { "retry-after": "20" } }`. Updates that
provider's known quota; on a throttle, pauses every policy bound to it.

## Management

| Method and path | Permission | Purpose |
|---|---|---|
| `GET /policies`, `GET /policies/{id}` | read | List or fetch, with live limit, multiplier and health |
| `POST /policies` | manage | Create |
| `PATCH /policies/{id}` | manage | Change any field except the name; takes effect on the next request |
| `DELETE /policies/{id}` | manage | Delete, and clear its limiter state |
| `POST /policies/{id}/reset` | manage | Clear every client's usage and any pause |
| `GET /algorithms` | read | The algorithm catalogue and which policies use each |
| `GET /keys`, `GET /keys/{id}/usage` | read | Keys (never the secret) and 30 days of usage |
| `POST /keys` | manage | Create; the secret is in this response only |
| `POST /keys/{id}/revoke` | manage | Revoke |
| `GET /providers` | read | Known quota and pause state per provider |
| `PUT /providers/{name}` | manage | Define a custom provider's header map |
| `DELETE /providers/{name}` | manage | Forget its state and lift pauses |
| `GET /config` | read | Runtime configuration |
| `PUT /config` | admin | Replace it |
| `GET /users`, `POST /users`, `PATCH /users/{id}`, `DELETE /users/{id}` | admin | Manage dashboard users |
| `GET /audit` | admin | Management actions, newest first |

### Policy fields

| Field | Default | Notes |
|---|---|---|
| `name` | required | Letters, digits, `.`, `-`, `_`, `:`; cannot be changed later |
| `limit`, `window_seconds` | required, 60 | `limit` requests per window |
| `algorithm` | `token_bucket` | `token_bucket`, `sliding_window`, `fixed_window`, `leaky_bucket`, `adaptive_hybrid` |
| `burst` | 0 | Meaning depends on the algorithm (see `GET /algorithms`) |
| `scope` | `custom` | `global`, `user`, `ip`, `api_key`, `role`, `custom` |
| `rules` | `[]` | Each: `attribute`, `values`, `action` (`limit`, `allow`, `deny`), `priority`, and for `limit`: `limit`, optional `window_seconds` and `burst`. IP rules accept CIDR ranges. |
| `adaptive` | true | Whether the adaptive engine adjusts this policy |
| `min_limit`, `max_limit` | 10% of limit, limit | Bounds for the adaptive engine |
| `target_latency_ms` | global setting | p95 latency above this starts to count against health |
| `priority` | 50 | 0–100; higher-priority policies are cut less under stress |
| `provider`, `provider_dimension` | none, `requests` | Bind to an upstream provider's quota, counted in requests or tokens |

## Observability

| Method and path | Purpose |
|---|---|
| `GET /metrics/live` | Current rates, health, every policy's live state, provider quotas |
| `GET /metrics/series?seconds=&step=&policy=` | Per-second traffic, up to the last hour |
| `GET /stream` | Server-sent events: a metrics frame each second and new decisions |
| `GET /analytics/overview`, `/timeseries`, `/heatmap`, `/top` | Aggregates from minute rollups |
| `GET /monitoring` | Health of Redis, Postgres, the worker and API processes |
| `GET /logs?q=&policy=&allowed=&reason=&since=&until=&cursor=` | Search the decision log |
| `GET /logs/export?format=csv\|ndjson` | Export up to 100,000 matching rows |

Outside `/api/v1`: `GET /health` (liveness), `GET /ready` (Redis and Postgres reachable),
`GET /metrics` (Prometheus; set `AR_METRICS_TOKEN` to require a bearer token).

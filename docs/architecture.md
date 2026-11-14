# Architecture

## Processes

| Process | Role | Scales by |
|---|---|---|
| **API** (`uvicorn app.main:app`) | Answers decisions, serves the management API and live stream | Adding workers or replicas; they share nothing but Redis and Postgres |
| **Worker** (`python -m app.worker`) | Adaptive loop, decision-log sink, minute rollups, housekeeping | Run two for availability; a Redis lock lets one lead and the other take over within seconds |
| **Redis** | Limiter state, live counters, adaptive multipliers, pub/sub | Single instance (see Limitations in the README) |
| **Postgres** | Policies, users, API keys, audit log, decision log, rollups | Standard Postgres practice |
| **Dashboard** | Static files plus an Nginx proxy to the API | Stateless |

## The decision path

`POST /api/v1/evaluate` does the following and nothing else:

1. Authenticate the API key against an in-memory map of SHA-256 hashes.
2. Look the policy up in an in-memory cache.
3. Resolve the bucket: the policy's scope picks which attribute (user, IP, API key, role, custom
   key) identifies it; the highest-priority matching rule may override the limit or short-circuit
   to allow or deny.
4. Run one Lua script in Redis. The script reads the clock from Redis (so every caller shares
   one), applies the policy's adaptive multiplier, checks for a provider pause, runs the
   algorithm, and increments the per-second counters, all atomically.
5. Put a log event in a bounded in-memory buffer, flushed to a Redis stream four times a second.

Postgres is never read on this path. Policies and keys reach the cache through a pub/sub
invalidation message on every change, with a periodic reload as a safety net.

### Why a request without an identity is rejected

If a policy is scoped per user and a check arrives with no user, the server answers `422` rather
than putting the request in a shared bucket. Pooling such requests would let a caller escape its
own limit by leaving the field out.

## Algorithms

| Algorithm | State per bucket | Behaviour |
|---|---|---|
| Token bucket | Tokens and a timestamp | Refills at `limit/window`; holds up to `limit + burst` |
| Sliding window | Two counters | Current window plus the previous one weighted by overlap; no edge bursts, constant memory |
| Fixed window | One counter per aligned window | Cheapest; allows up to twice the limit across a window boundary |
| Leaky bucket | One timestamp (GCRA) | Spaces requests `window/limit` apart; `burst + 1` may arrive together |
| Adaptive hybrid | Token bucket and sliding window together | A small bucket shapes bursts while the window enforces the total; both must pass |

The algorithm is part of the Redis key, so switching a policy's algorithm starts from clean state
instead of misreading the old algorithm's data.

## The adaptive engine

Once a second, for every enabled adaptive policy, the worker:

1. **Collects signals** from the last few seconds: what SDKs reported (requests, errors, upstream
   429s, timeouts, a latency histogram, in-flight and queue gauges, CPU and memory), with the
   AdaptiRate host's own CPU and memory as a fallback.
2. **Scores health** from 0 to 100. Each signal becomes a penalty between 0 (fine) and 1
   (critical). The score is 70% the worst penalty and 30% the weighted average, so one critical
   signal is enough to act on and several mild ones still add up.
3. **Steps the multiplier.** Below the decrease threshold it cuts multiplicatively, in proportion
   to severity and scaled by the policy's priority, with a cooldown between cuts. Above the
   increase threshold it adds a fixed step. Between the two it holds, which prevents oscillation.
   With no signals at all it drifts back to the configured limit.
4. **Applies the provider cap.** For a policy bound to an upstream provider, the limit cannot
   exceed that provider's remaining quota spread over the time until it resets.
5. **Writes the multiplier** to Redis with a 30-second expiry. If the worker dies, limits return
   to their configured values rather than staying stuck.

The control law is a pure function (`app/adaptive/controller.py`) and is tested against simulated
load curves, including a closed-loop test that it settles near a service's real capacity.

### What counts as unhealthy

Only responses with a 5xx status count as errors. A 429 the protected service returns itself is
deliberate load shedding; counting it would make the limiter cut limits because it was limiting.
Upstream throttling has to be reported explicitly (`report_provider`, or `throttled=True`).

## Leasing

`POST /api/v1/lease` debits a block of tokens in one call and returns how many were granted and
for how long they are valid. The SDK spends them in process and asks again when they run out or
expire. Unspent tokens are discarded, so a lease can under-admit but the total granted never
exceeds the limit. The validity is capped at two seconds, which bounds both how stale a client's
view of an adaptive limit can be and how much traffic can shift across a window boundary.

## Data retention

- Live per-second counters: one hour, in Redis.
- Minute rollups: kept indefinitely in Postgres; these feed analytics.
- Decision log: bounded by both age (default 7 days) and row count (default 1,000,000).
- Audit log: kept indefinitely.

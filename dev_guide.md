# AdaptiRate developer guide

This explains how AdaptiRate works from the inside, and why each part is built the way it is.
It is written for someone who will change the code, or who has to defend its design to someone
else. The README says what the project does; `docs/` covers the API, deployment and tuning. This
file is the reasoning.

Contents:

1. [Running it](#1-running-it)
2. [The idea](#2-the-idea)
3. [One request, start to finish](#3-one-request-start-to-finish)
4. [The limiter: Redis and Lua](#4-the-limiter-redis-and-lua)
5. [The five algorithms](#5-the-five-algorithms)
6. [Policies, scopes and rules](#6-policies-scopes-and-rules)
7. [The decision path in the API](#7-the-decision-path-in-the-api)
8. [Three ways to ask: single, batch, lease](#8-three-ways-to-ask-single-batch-lease)
9. [The adaptive engine](#9-the-adaptive-engine)
10. [Provider quotas](#10-provider-quotas)
11. [The worker](#11-the-worker)
12. [Storage](#12-storage)
13. [Authentication and access control](#13-authentication-and-access-control)
14. [The live stream](#14-the-live-stream)
15. [The dashboard](#15-the-dashboard)
16. [The SDKs](#16-the-sdks)
17. [Docker and deployment layout](#17-docker-and-deployment-layout)
18. [Testing](#18-testing)
19. [Performance: what was measured and what it taught](#19-performance-what-was-measured-and-what-it-taught)
20. [Mistakes made along the way](#20-mistakes-made-along-the-way)
21. [What I would do next](#21-what-i-would-do-next)

---

## 1. Running it

Everything runs in Docker. The host needs Docker Desktop and nothing else.

```bash
cd AdaptiRate
docker compose up -d                              # redis, postgres, api, worker, dashboard
docker compose --profile demo up -d --build       # plus a demo store API and a traffic generator
```

| What | Where |
|---|---|
| Dashboard | http://localhost:8080 |
| API, and its interactive docs | http://localhost:8000, http://localhost:8000/api/v1/docs |
| Demo store API | http://localhost:8090 |
| Prometheus metrics | http://localhost:8000/metrics |

Sign in to the dashboard with `AR_BOOTSTRAP_ADMIN_EMAIL` and `AR_BOOTSTRAP_ADMIN_PASSWORD` from
`.env`. That file was generated with random secrets and is not committed.

The demo's traffic generator cycles through phases about every four minutes: steady traffic, a
3x spike, a slow dependency, recovery, a flaky dependency, recovery. Watch the Dashboard page.
During the slow phase the `demo-api` policy shows "Shedding load" and its limit falls; during
recovery it climbs back.

```bash
docker compose --profile demo stop demo-load      # stop the traffic (it uses real CPU)
docker compose --profile demo down                # stop everything
```

Tests:

```bash
docker compose --profile dev run --rm dev pytest          # server, 185 tests
bash acceptance/run.sh                                    # 7 end-to-end scenarios, about 4 minutes
```

The SDK suites and how to run them are in `.github/workflows/ci.yml`, which is the authoritative
list of every check.

---

## 2. The idea

A rate limiter answers one question: may this request proceed? Most answer it by comparing a
counter to a constant. AdaptiRate adds three things.

**The counter is shared.** It lives in Redis, so ten processes on five machines draw from one
budget. This is the "distributed" part and it is table stakes.

**The constant is not constant.** The services being protected report how they are doing
(latency, errors, how much work is in flight). A worker turns that into a health score once a
second and moves the limit down when health is poor and back up when it is good. This is the
"adaptive" part.

**Upstream quotas count too.** If a policy guards calls to an API that has its own rate limit,
AdaptiRate reads that API's rate-limit headers and paces calls so the quota lasts, and pauses
when the API answers 429.

The original scaffold in this repo had the first idea, half-built, and the second idea wrong: it
watched the CPU of the machine the *limiter* ran on. That tells you nothing about whether the
service behind the limiter is in trouble. The single most important design decision in the
rebuild was moving the signal source from "the limiter's own host" to "what the protected
services report through the SDK". Everything in section 9 follows from that.

---

## 3. One request, start to finish

A client calls your service. Your service has the SDK middleware installed with
`policy="checkout"`.

1. **SDK** (`sdk-python/adaptirate/asgi.py`) works out who is calling, by default the client
   address, or whatever your `identify` function returns, such as `{"user": "u_42"}`.
2. **SDK** sends `POST /api/v1/evaluate` with `{"policy": "checkout", "user": "u_42"}` and the
   API key in a header.
3. **API** (`adaptirate/app/api/evaluate.py`) authenticates the key against an in-memory map,
   looks up the policy in an in-memory cache, and resolves which bucket this request belongs to.
4. **API** runs one Lua script in Redis (`adaptirate/app/limiter/lua/`). The script reads the
   time from Redis, reads the policy's current adaptive multiplier, checks for a provider pause,
   runs the algorithm, and increments counters. All of that is one atomic step.
5. **API** returns `{"allowed": true, "limit": 100, "remaining": 57, ...}`.
6. **SDK** either lets the request through or answers 429 with `Retry-After`.
7. When the handler finishes, **SDK** records its latency and status. Once a second it sends the
   totals to `POST /api/v1/report`.
8. Once a second the **worker** (`adaptirate/app/worker.py`) reads those reports, scores the
   policy's health, and writes a new multiplier to Redis. Step 4 picks it up on the next request.

Postgres is not touched anywhere in steps 1 to 6. That is deliberate and explained in section 7.

---

## 4. The limiter: Redis and Lua

### Why Redis

The counter has to be shared and it has to be fast. Redis is the standard answer and I saw no
reason to be original. Postgres could hold the counters but every check would be a write with a
round trip several times slower.

### Why Lua scripts

A rate-limit check is "read the count, decide, write the count". If two processes do that at the
same moment without coordination, both read 99, both decide "under 100", both write 100, and 101
requests get through. The fix is to make read-decide-write one indivisible step. Redis runs a
Lua script to completion before doing anything else, so a script is atomic for free.

The alternative was `WATCH`/`MULTI` transactions, which retry on conflict. Under contention,
which is exactly when a rate limiter matters, they retry a lot. Scripts do not.

The test that proves this is `test_concurrent_requests_never_over_admit` in
`adaptirate/tests/test_engine.py`: 400 concurrent checks against a capacity of 60 must admit
exactly 60, for every algorithm.

### Why the time comes from Redis

Each script begins with `redis.call('TIME')`. The original scaffold passed in the API server's
own clock. With several API servers whose clocks differ by even a few hundred milliseconds,
token buckets refill at the wrong rate and windows roll over at different moments for different
callers. Using Redis's clock means there is exactly one clock.

### Why the script is assembled from three files

Every algorithm needs the same preamble (parse arguments, read the time, read the multiplier)
and the same ending (check for a pause, record counters, format the reply). `engine.py` builds
each script as `_header.lua` + the algorithm + `_footer.lua`. Each algorithm file only defines a
`run()` function. This keeps five algorithms from carrying five copies of the bookkeeping.

### Why the adaptive multiplier is read inside the script

The worker changes limits every second. The API processes need the new value. I considered
caching it in each API process, but that means either a staleness window or another pub/sub
channel. Instead the script does `GET ar:m:<policy>` itself. It costs one extra key read inside a
round trip that was happening anyway, and every process sees the change on the very next request.

The script computes `limit = max(1, floor(base_limit * multiplier + 0.5))`. The floor of 1 means
a policy can be squeezed hard but never to zero by adaptation; only a provider pause blocks
outright.

### Why counters are incremented in the same script

Allowed and blocked counts feed the dashboard, analytics and Prometheus. Recording them in the
decision script means they cannot drift from the decisions, and it costs no extra round trip.
They are per-second hashes (`ar:c:<policy>:<second>`) that expire after an hour.

### What this costs: no Redis Cluster

The scripts build some key names at run time (the per-second counter key depends on the time the
script just read). Redis Cluster requires every key to be declared up front so it can route the
script to the right shard. So Cluster is not supported. I accepted this because a single Redis
handles far more traffic than the Python API in front of it can generate, and it is stated in the
README.

Redis is configured with `noeviction`. If Redis evicted limiter keys under memory pressure, it
would silently reset people's limits, which is the worst way for a rate limiter to fail.

---

## 5. The five algorithms

All in `adaptirate/app/limiter/lua/`.

**Token bucket.** A bucket holds up to `limit + burst` tokens and refills at `limit / window`.
Each request takes one. State is a token count and a timestamp. Good default: it tolerates
bursts and is cheap.

**Fixed window.** One counter per aligned window (minute 12:00, minute 12:01). Cheapest of all.
Its known flaw is that a caller can send a full limit at 12:00:59 and another at 12:01:00, twice
the limit in two seconds. Included because for coarse quotas ("1,000 a day") that flaw is
irrelevant.

**Sliding window.** Fixes the edge burst. The textbook version keeps a log of every request
timestamp, which costs memory proportional to the limit. I used the *sliding window counter*
instead: keep the current window's count and the previous window's, and weight the previous one
by how much of it still overlaps the sliding window. Two numbers per caller regardless of limit.
It is an approximation, and a good one. The original scaffold used the log version and had a bug
in it: two requests in the same millisecond produced the same set member and were counted once.

**Leaky bucket.** Implemented as GCRA (generic cell rate algorithm), which stores a single
number: the theoretical arrival time of the next allowed request. It spaces requests evenly,
`window / limit` apart, with `burst + 1` allowed to arrive together. I made it deliberately
strict rather than a second token bucket under a different name, because otherwise there is no
reason for it to exist.

**Adaptive hybrid.** A small token bucket (a fifth of the limit unless `burst` says otherwise)
and a sliding window, both of which must pass, committed atomically. The bucket smooths bursts
and the window enforces the total. It suits adaptive policies because when the limit is cut, the
small bucket makes the cut bite immediately rather than after a large bucket drains.

### Decisions that apply to all of them

**The algorithm is part of the key name** (`ar:s:<policy>:<algorithm>:<subject>`). You can
switch a policy's algorithm from the dashboard at any time. If the key did not include the
algorithm, the new algorithm would read the old one's state and misinterpret it. With it, a
switch starts clean.

**Retry-after is honest.** Each script computes when the request would actually succeed.
`test_retry_after_is_honest` waits exactly that long and asserts the next request passes. A
limiter that tells clients to retry in 1 second when they need 30 creates a thundering herd.

**A request that can never fit is flagged differently.** A cost of 50 against a capacity of 10
returns reason `too_large` with `retry_after = -1`, not "try again later", because later will
not help.

---

## 6. Policies, scopes and rules

`adaptirate/app/limiter/policy.py`. Pure functions, no I/O, easy to test.

A **policy** has a limit, a window, an algorithm, and a **scope**: which attribute of the request
identifies a bucket. Scope `user` means each user has their own bucket; `ip` means each address;
`global` means one bucket for everybody; `custom` means whatever string the caller passes as
`key`.

**Rules** override the policy for matching requests: "role `premium` gets 1,000 instead of 100",
"this IP range is always allowed", "this user is always denied". Rules are sorted by priority and
the first match wins.

### Why a missing identity is an error

If a policy is scoped per user and a check arrives with no user, the API returns 422. The
tempting alternative is to put such requests in a shared "anonymous" bucket. That would let any
caller escape its own limit by simply not sending the field. Failing loudly also catches
integration mistakes on day one rather than in an incident.

### Why IP addresses are canonicalised

The same address can be written many ways: `10.1.2.3`, `::ffff:10.1.2.3`, `2001:db8::1`,
`2001:0DB8:0:0:0:0:0:1`. Before this was fixed, a deny rule for `10.0.0.0/8` did not match a
dual-stack client reported as `::ffff:10.1.2.3`, and one client could get several buckets by
varying the spelling. `canonical_ip()` reduces every address to one form before rules or buckets
see it.

### Why rules live in a JSON column

Rules are always read and written together with their policy and never queried on their own. A
separate table would add joins and migrations for no benefit.

---

## 7. The decision path in the API

`adaptirate/app/api/evaluate.py` and `adaptirate/app/limiter/cache.py`.

### No Postgres on the hot path

The original scaffold ran a Postgres query on every check to load the policy. That makes
Postgres a dependency of every request your service handles and caps throughput at whatever
Postgres can do.

Now every API process keeps policies, API keys and runtime settings in memory (`RuntimeCache`).
A check is two dictionary lookups and one Redis call.

The cache stays fresh two ways. When anything changes, the writer publishes a message on a Redis
pub/sub channel and every process reloads. Pub/sub can drop messages, so each process also
reloads every 30 seconds. The first makes changes fast; the second makes missing one harmless.
The process that made the change also reloads synchronously before responding, so you can create
a policy and use it in the next request.

### Why API keys are SHA-256 hashed, not bcrypt

Passwords need a slow hash because people choose guessable ones. API keys are 256 random bits;
there is nothing to guess, so a fast hash is enough, and a fast hash can be checked on every
request without becoming the bottleneck. The key is shown once at creation and only the hash is
stored.

### Why responses bypass FastAPI's response model

The evaluate handlers build a dict and return `Response(orjson.dumps(...))` directly. Letting
FastAPI validate and serialise through the response model costs measurable time per request, and
this endpoint is called on every request your service handles. The response model is still
declared so the generated API docs are right.

### The decision log is a bounded buffer

Each API process holds log events in memory and flushes them to a Redis stream four times a
second (`services/events.py`). The buffer is capped at 100 events per second per process. Past
that, events are dropped and counted.

The principle: logging must never slow or break a decision. A traffic spike is precisely when
you cannot afford the logger to back up. Counters are exact because they are incremented in the
Lua script; only the per-request log is sampled.

---

## 8. Three ways to ask: single, batch, lease

**Single** (`POST /evaluate`): one HTTP call, one Redis call, one decision. Exact. A few
milliseconds.

**Batch** (`POST /evaluate/batch`): up to 500 checks in one HTTP call, executed as one Redis
pipeline. For queue consumers and fan-out jobs.

**Lease** (`POST /lease`): the SDK asks for a block of tokens, say 50, and the server debits all
50 at once. The SDK then answers the next 50 checks itself, in memory, with no network call.

### Why leasing exists

The original brief asked for 50,000 decisions per second. A Python HTTP endpoint cannot do that
honestly; a few thousand per core is realistic. The only way to get much higher throughput is to
not make a network call per decision. Leasing does that.

### What leasing costs, and how the cost is bounded

- **Unused tokens are lost.** If a process leases 50 and uses 30, the other 20 are gone. So a
  lease can under-admit. It can never over-admit, because the server debited them already.
- **Limit changes arrive late.** A leasing client is working from tokens granted under the old
  limit.

Both are bounded by the lease's time-to-live: half the window, capped at two seconds. After
that the SDK throws away whatever it has left and asks again.

### A lease larger than capacity is clamped

Found in review: an SDK configured with `lease_size=50` against a policy of 20 per second was
refused with `too_large`, forever, because 50 can never fit. Every request was then denied even
on an idle limiter. `LimiterEngine.lease()` now clamps to what the algorithm can grant, using
the adaptively reduced limit, then falls back to whatever is left.

The probe requests a lease makes are flagged so they do not count as blocked traffic. Otherwise a
lease of 50 with 12 left would record 50 blocked and 12 allowed, and the dashboard would lie.

---

## 9. The adaptive engine

`adaptirate/app/adaptive/`. This is the part that makes the project more than a Redis counter.

### Where the signals come from

From the SDK. The middleware wraps each request your service handles, times it, and notes the
status. Once a second it sends: request count, errors, timeouts, upstream 429s, a latency
histogram, how many requests are in flight, queue depth if you set it, and CPU and memory if
`psutil` is installed.

As a fallback, the worker also samples CPU and memory of its own host. That is only meaningful
when AdaptiRate runs on the same machine as the service. There is a setting to turn it off.

### Why a latency histogram and not an average

Averages hide the tail. If 95 requests take 10 ms and 5 take 5 seconds, the average is 260 ms
and looks tolerable while one user in twenty is having a terrible time. The engine works from
p95.

You cannot average percentiles across reporters, though: the p95 of two services is not the mean
of their p95s. So each SDK sends counts in fixed buckets (up to 1 ms, 2, 5, 10, 25, 50, 100, 250,
500, 1000, 2500, 5000, 10000, and beyond). Bucket counts from any number of reporters can be
added, and the percentile is computed from the total. This is the same reason Prometheus
histograms work the way they do. The bucket boundaries are hard-coded identically in all three
SDKs and the server.

### How health is scored

Each signal becomes a penalty from 0 (fine) to 1 (critical), linear in between. For latency, 0 at
the policy's target and 1 at three times the target. For error rate, 0 at 1% and 1 at 20%.

The health score is `100 * (1 - (0.7 * worst + 0.3 * weighted_average))`.

The weighting toward the worst signal was arrived at by getting it wrong first. The first
version used 50/50. With latency at eight times target but errors, timeouts and queue all fine,
the healthy signals diluted the score to 36, which produced a cut of only 20% per step. A service
that slow needs a much harder response. At 70/30 the same situation scores 22 and cuts 32% per
step. The principle: one critical signal must be enough to act on, and several mild ones should
still add up.

### How the limit moves: AIMD

Additive increase, multiplicative decrease. The same scheme TCP uses for congestion control, for
the same reason.

- **Health below 60:** cut multiplicatively. The cut is proportional to how far below 60, up to
  50% in one step.
- **Health 80 or above:** add 5% of the base limit per second.
- **Between 60 and 80:** hold.

Decrease is multiplicative because when a service is overloaded you need to back off fast, and a
proportional cut backs off faster the worse things are. Increase is additive because you are
probing for where the edge is and want to approach it gently.

**The dead band** between 60 and 80 prevents oscillation. Without it, a system sitting right at
the threshold would cut, recover, cut, recover forever.

**A cooldown** of two seconds between cuts gives each cut time to show its effect. The signal
window is five seconds, so right after a cut the signals still mostly reflect the time before
it. Without a cooldown the engine would cut again on stale evidence and overshoot.

**Priority** scales the cut. Each policy has a priority from 0 to 100; a priority-100 policy is
cut half as hard as a priority-50 one. Under shared stress, search gets shed before checkout.

**With no signals at all** the multiplier drifts back toward 1. If nothing is reporting, the
engine has no basis for a judgement, and holding a reduced limit forever on no evidence would be
wrong.

### Why the control law is a pure function

`controller.step(state, signals, ...)` takes the current state and signals and returns the new
state. No Redis, no clock, no I/O. This makes it testable with simulated load curves.
`test_closed_loop_settles_at_the_capacity_of_the_service` simulates a service whose latency
climbs past a capacity of 60 and asserts the limit settles near 60, neither collapsing nor
overshooting. You could not write that test against something entangled with Redis.

### Why the worker is a separate process

The adaptive loop must run exactly once per second, in one place. If it ran inside each API
process, two processes would each compute a multiplier and fight. The original scaffold kept the
multiplier in process memory, which had exactly that bug.

A separate worker process takes a Redis lock (`SET NX PX 5000`) and renews it every tick. You
can run two workers for availability; the second sits idle and takes over within five seconds if
the first stops.

### Why the multiplier expires

The worker writes the multiplier with a 30-second expiry. If the worker dies, limits return to
their configured values within 30 seconds, rather than staying frozen at whatever they were
during the incident that may have killed it.

### What counts as unhealthy: only 5xx

This was a real bug, found by running the demo. The middleware originally reported any 429
response as "upstream throttling". But the demo app returns 429 itself when its own internal
limiter says no. So: the limiter blocks a request, the app returns 429, the SDK reports that as
ill health, the engine cuts the limit, more requests get blocked, more 429s. The limiter was
cutting limits because it was limiting.

Now only 5xx counts as an error. A 429 your service chooses to send is deliberate load shedding.
Upstream throttling must be reported explicitly.

---

## 10. Provider quotas

`adaptirate/app/providers/`.

### Reading the headers

`parsers.py` knows the rate-limit header formats of OpenAI, Groq, Anthropic and GitHub. The
formats differ: OpenAI gives reset times as durations (`6m0s`), Anthropic as timestamps, GitHub
as epoch seconds. Gemini publishes no quota headers, so for Gemini only 429 and `Retry-After`
are usable. For anything else you register a header map.

The parser is a pure function, tested against header sets in each provider's documented format.
Malformed values are ignored rather than raised: a provider sending something odd should not
take the limiter down.

### Pacing: two kinds of quota

This is where the acceptance suite found a design flaw.

The first version computed `allowed rate = remaining / time until reset`. That is correct for a
**fixed-window** quota like GitHub's: you have N calls until the top of the hour and nothing
comes back until then.

OpenAI, Anthropic and Groq work differently. Their quota **refills continuously**, and the reset
header means "when the bucket will be full again". When such a provider says "0 remaining, full
in 60 seconds", it does not mean "no calls for 60 seconds". It means "I refill at limit/60 per
second". The first version read it as the former and held the policy at one call per minute,
long after the provider's own `Retry-After` had expired.

For continuous-refill providers the usable amount over the reset period is what remains plus
what refills, which is the whole limit: `allowed rate = limit / time until reset`. Each header
map now says which kind it is. See `provider_cap()` in `controller.py`.

A 10% safety margin is held back by default, because the headers are a snapshot and other
clients may be spending the same quota.

### Pausing

When a provider answers 429, every policy bound to it is paused: a Redis key with a TTL, which
the Lua script checks first. The pause length is, in order of preference: the provider's
`Retry-After`; the reset time if remaining is 0; otherwise exponential backoff (2, 4, 8 seconds,
doubling up to an hour).

The backoff ceiling was originally 60 seconds. Checking Anthropic's documentation showed that
its monthly spend cap answers 429 with no `Retry-After` and clears only on the 1st. With a
one-minute ceiling AdaptiRate would have probed it every minute for weeks. It now takes about a
dozen consecutive refusals to reach the hour, so a single 429 still costs only two seconds.

OpenAI documents its rate-limit headers but not whether its quotas refill continuously or in
fixed windows. AdaptiRate assumes continuous, which matches observed behaviour and Anthropic's
documented model. That is an assumption, written down in `parsers.py` and `docs/tuning.md`,
with a per-provider override if it proves wrong.

The pause is capped at an hour. A hostile or broken `Retry-After: 999999999` cannot park traffic
forever.

A pause is extended but never shortened: if a 30-second pause is in force and a new report says
5 seconds, the 30 stands.

### Stale quota is ignored

If the reset time has passed, the stored "remaining" figure is out of date: the provider has
refilled. `ProviderState.reset_in()` returns "unknown" in that case and no cap is applied.
Before this was fixed, "0 remaining" from an hour ago would keep the cap at zero for up to a day.

---

## 11. The worker

`adaptirate/app/worker.py`. One process, three loops, all gated on holding the leader lock.

**Control loop, every second.** Section 9.

**Sink loop.** Reads decision events from the Redis stream and bulk-inserts them into Postgres.
It tracks its position with a cursor key and advances it only after the insert commits. That
makes delivery at-least-once: a crash between commit and cursor update re-inserts one batch.
I chose at-least-once over at-most-once because a duplicate log line is harmless and a lost one
is not.

If a batch insert fails, it retries row by row and drops only the rows the database rejects.
This was a real bug: a subject containing a NUL byte (which Postgres cannot store in text) made
every batch containing it fail, the cursor never advanced, and the log stalled permanently. NUL
bytes are now stripped at the source too.

**Slow loop, every five seconds.** Rolls up the previous minute's per-second counters into one
Postgres row per policy, because the per-second counters expire after an hour and analytics need
history. If the worker was down, it backfills up to 55 minutes on restart, which is as far back
as the per-second data goes. Rollups are upserts, so re-running one is safe. Once a minute it
also deletes old logs, expired refresh tokens, and trims the top-clients sets.

### Startup waits for dependencies

Both the API and the worker call `wait_for_dependencies()` before doing anything, retrying Redis
and Postgres for up to 60 seconds. Found the hard way: after a Docker restart Postgres spent a
few seconds replaying its log, the API crashed on its first query, and Docker's restart backoff
grew until the API stayed down for minutes. Waiting is quicker and quieter than crash-looping.

---

## 12. Storage

| Data | Where | Why |
|---|---|---|
| Limiter state, live counters, multipliers, pauses | Redis | Must be fast and shared; losing it resets usage and nothing else |
| Policies, users, API keys, settings | Postgres | Must survive; small; changes rarely |
| Audit log | Postgres | Must survive; append-only |
| Decision log | Postgres | Searchable history; bounded by age (7 days) and rows (1,000,000) |
| Minute rollups | Postgres | History for analytics after the per-second counters expire |

The dividing line: if losing it is an inconvenience, Redis. If losing it is an incident,
Postgres.

### Why the decision log is sampled by default

Six hours of demo traffic at 120 requests a second wrote 3.9 million rows, 1.2 GB. Logging every
allowed request does not scale and nobody reads them. The default now keeps every blocked request
and one in ten allowed ones, with a hard cap on rows. Blocked requests are the ones people
search for.

### Schema changes

Alembic migrations, applied by a one-shot `migrate` container before the API starts. CI runs
`alembic check` to fail the build if the models and migrations disagree.

---

## 13. Authentication and access control

Two kinds of caller: people using the dashboard, and services using the SDK.

### Dashboard sessions

A short-lived JWT access token (15 minutes) held **in JavaScript memory only**, and a long-lived
refresh token (7 days) in an **HttpOnly, SameSite=Strict cookie** scoped to `/api/v1/auth`.

- The access token is not in `localStorage`, so a cross-site scripting bug cannot steal a
  long-lived credential.
- The refresh token is HttpOnly, so script cannot read it at all.
- Every state-changing endpoint requires the access token in an `Authorization` header, which a
  cross-site form post cannot set. That, plus SameSite=Strict on the cookie, is the CSRF defence.
- The JWT algorithm is pinned to HS256 when verifying. The classic JWT attack is to send a token
  claiming `alg: none`; a verifier that trusts the header accepts it. There is a test for that.

**Refresh tokens rotate.** Each refresh issues a new token and marks the old one used. If a used
token turns up again later, that is evidence it was stolen, so every session for that user is
ended.

**A ten-second grace window** on that rule, added after review: two browser tabs whose access
tokens expire together both send the same cookie. Without the grace window the second tab looked
like a thief and signed the user out everywhere.

Adding the grace window introduced a hole that a test caught immediately: a token revoked by
*logout* was also "recently revoked", so it could be reused for ten seconds. The fix was to stop
using one flag for two meanings. Rotation sets `revoked_at`; logout and admin revocation delete
the row. A deleted token cannot fall into any window.

**Passwords** are hashed with argon2. When an account does not exist, a dummy hash is verified
anyway so response time does not reveal which emails are registered, and the error message is
identical either way.

**Sign-in is rate limited using the limiter itself**: 8 attempts per account and 40 per address
every five minutes. The per-address limit is looser so one office behind a NAT is not locked out
together.

### API keys

`ar_` plus 256 random bits. Stored as a SHA-256 hash. Each key has scopes (`evaluate`, `report`,
`read`, `manage`), an optional expiry, and optionally a list of policies it is restricted to.

- A key cannot create a key with scopes it does not hold.
- A key restricted to certain policies cannot create a wider key, cannot manage other policies,
  and cannot pause other tenants' policies by reporting provider 429s. All three were holes
  found in review.

### Roles

Admin, developer, viewer, mapped to scopes in `deps.py`. The last active admin cannot be demoted,
disabled or deleted.

### Audit log

Every management action writes an audit row **in the same database transaction** as the change.
They commit or roll back together, so there can be no change without a record or record without
a change.

### Smaller things

- **CORS** is off unless configured, and never a wildcard with credentials (which the original
  scaffold had; that combination lets any website act as the signed-in user).
- **`X-Forwarded-For` is believed only from configured proxy addresses.** Otherwise anyone could
  spoof their address and dodge the sign-in throttle.
- **CSV export** prefixes cells starting with `=`, `+`, `-` or `@` with a quote. Subjects are
  caller-controlled text, and a subject like `=cmd|...` would execute when someone opens the
  export in a spreadsheet.
- **Search input** has LIKE wildcards escaped, so `%` matches a literal percent sign.

---

## 14. The live stream

`GET /api/v1/stream` sends server-sent events: a metrics snapshot every second, plus any new
decisions.

**SSE rather than WebSockets** because the data flows one way, SSE is plain HTTP, and it passes
through proxies without special handling.

**Read with `fetch`, not `EventSource`**, because `EventSource` cannot send an `Authorization`
header and I did not want the token in the URL, where it ends up in logs.

**The stream ends when the token that opened it expires.** Authentication is checked once, at
connect. Without a deadline, a user whose access was revoked would keep receiving data for as
long as the connection stayed open. The dashboard reconnects with a fresh token automatically.

---

## 15. The dashboard

`dashboard/`. React, TypeScript, Vite, Tailwind, Recharts.

**No component library.** The spec mentioned shadcn/ui. shadcn is copy-in components built on
Radix, which is a sizeable dependency for a toggle, a modal and a select. `components/ui.tsx`
has small hand-written versions with the accessibility basics: focus management and Escape in
the modal, `role="switch"`, labels tied to inputs.

**One shared live connection.** `lib/live.tsx` opens one stream and every page reads from it,
rather than each page opening its own.

**Everything is real data.** The original scaffold's dashboard showed `Math.random()` and a
hard-coded CPU of 45. Nothing here is mocked. Empty states say what is missing and why.

**Appearance.** It began dark with gradients and glass panels, per the original brief. You asked
for white and professional, so it is now white cards on light grey with one accent colour, flat
chart fills, no gradients, no glow, no blur. A later pass removed icon tiles on stat cards,
entrance animations, pulsing dots and page subtitles, which add visual noise without information.

**Served by Nginx, which also proxies `/api`.** Same origin means the refresh cookie is
first-party and no CORS setup is needed.

A gotcha worth recording: in Nginx, an `add_header` inside a `location` block replaces every
`add_header` from the enclosing `server` block. The first config set a cache header in the
location and silently lost the Content-Security-Policy on the page. Caching now uses `expires`,
which does not have that effect.

---

## 16. The SDKs

Python, Node, Java. Same design in each: a core of transport-independent logic, a client, and
framework integrations.

### Fail modes

What should your service do if AdaptiRate is unreachable?

- **open**: allow everything. Default, because a rate limiter outage should not become your
  outage.
- **closed**: deny everything. For when the limit protects something that truly must not be
  exceeded.
- **local**: keep enforcing the last limit the server reported, with an in-process token bucket.

`local` needs to know the policy's window. Originally it guessed from the largest
`reset_after_ms` it had seen, which for a token bucket after one request is `window / limit`, so
during an outage it refilled about a hundred times too fast. The server now includes
`window_ms` in every response.

### The circuit breaker

After a failed call, the SDK skips the server for one second and goes straight to the fail mode.
Without this, an outage adds a one-second timeout to every request your service handles, turning
"the limiter is down" into "everything is slow".

### Configuration errors are loud

A 401 (bad key), 404 (no such policy) or 422 (missing identity) raises an exception. Only
network failures and 5xx go through the fail mode.

This is the opposite of the original scaffold, where *every* failure failed open, including one
caused by its own bug (it posted to `/evaluate`, the route was `/evaluate/`, the redirect raised,
and it returned "allowed"). The Python SDK never limited anything and nobody would have known.
A misconfigured limiter should be impossible to miss.

### Middleware does not read `X-Forwarded-For`

The default identity is the connection's address. Behind a proxy that is the proxy. The
tempting fix is to read `X-Forwarded-For`, but any client can set that header to anything.
Whether to trust it depends on your proxy setup, which the SDK cannot know. So each middleware
documents how to make your framework do it correctly and takes an `identify` function.

### In-flight accounting

Each middleware marks a request as started and guarantees it is marked finished exactly once,
whether it succeeds, throws, or the client disconnects. An in-flight count that leaks upward
makes a healthy service look overloaded. Two bugs here, both fixed: the Spring interceptor ran
twice for async handlers, and the WSGI middleware never closed the response iterable (which also
broke Django's request cleanup and buffered streaming responses in memory).

### Other details

- **Lease requests are single-flighted per bucket.** A burst of 100 concurrent checks makes one
  lease request, not 100.
- **Per-bucket maps are bounded** at 10,000 entries. A per-IP policy on a public API would
  otherwise grow memory with every distinct caller.
- **The Java client is pinned to HTTP/1.1.** Java's HTTP client defaults to attempting an HTTP/2
  upgrade over plain HTTP, which uvicorn does not support and which lost the request body. This
  cost a confusing 422 during the first live test.
- **The Node SDK has no runtime dependencies.** Node 18 has `fetch` built in.

---

## 17. Docker and deployment layout

- **Separate containers for API and worker** from one image. They scale differently: many API
  processes, one active worker.
- **A one-shot `migrate` container** runs migrations and exits; the API waits for it. Running
  migrations inside the API at startup races when several API containers start together.
- **Ports are bound to 127.0.0.1.** Redis, Postgres and the API are not reachable from the
  network by default. You expose what you mean to.
- **Compose profiles** keep optional things out of the default `up`: `demo`, `dev` (the test
  image), `acceptance`, `observability` (Prometheus).
- **Secrets come from `.env`**, which is gitignored. Compose refuses to start with a clear
  message if a required one is missing, rather than falling back to a default password.
- **The API image runs as a non-root user.**
- **Python 3.13 in the image.** The host has Python 3.14, for which several dependencies had no
  wheels yet. Running everything in Docker also means the host needs nothing installed.

---

## 18. Testing

### Real dependencies, not mocks

Server tests run against a real Redis and a real Postgres. The thing most likely to be wrong in a
rate limiter is the concurrency and the Lua, and a mock of Redis tests neither. The tests use a
separate Redis database and a separate Postgres database, and the test fixture refuses to run if
the database name does not end in `_test`.

### What the suites cover

| Suite | Count | Focus |
|---|---|---|
| Server | 185 | Algorithms under concurrency, policy resolution, the control law with simulated load, provider parsing, every API endpoint and role, the worker's jobs |
| Python SDK | 47 | Against a fake server, plus a live one |
| Node SDK | 30 | Same |
| Java SDK | 32 | Same, plus a real Spring MVC application |
| Acceptance | 7 scenarios | Real services in containers over real HTTP |

`tests/test_regressions.py` holds one test per defect found in review. Each fails on the code as
it was.

### The acceptance suite

`acceptance/`. Three real services (a FastAPI app run as three replicas, an Express gateway, a
fake rate-limited provider) driven with real traffic, with assertions like "300 concurrent
requests across three replicas: exactly 50 succeed" and "provider rejections go from 32 to 0".

It found two real bugs that 150 unit tests had missed, both in provider pacing (section 10). The
unit tests checked that the pacing formula computed what I intended. The acceptance test checked
whether what I intended was right. It was not.

### CI

`.github/workflows/ci.yml` runs on every push: server lint, strict type checking, migration
check and tests; dashboard type check and build; all three SDKs against a live server. It passed
on the first push from a clean checkout on GitHub's machines, which is independent evidence that
nothing depends on this laptop.

### Bug review by a second reader

After the main build, a separate read-only review was run over the whole codebase looking for
real defects. It produced 15, of which the important ones are described in the sections above.
The lesson is the ordinary one: the author is the worst person to find the author's mistakes.

---

## 19. Performance: what was measured and what it taught

On a 4-core laptop, stock Compose, two API workers, load generator on the same machine:

| Mode | Throughput |
|---|---|
| Single check | about 1,450 per second, p99 18 ms |
| Batch of 100 | about 36,000 per second |
| Leased | about 42,500 per second per SDK process |

The 50,000 target was not met by any single mode on this machine. It has not been measured on
server hardware. The README says so.

Two things learned measuring it:

**The first benchmark measured the wrong thing.** A Python load generator reported 35 decisions
per second at 64 connections. That looked like a catastrophic server bug. It was the client:
ApacheBench against the same server showed 3,500 requests per second on the health endpoint. A
Python HTTP client saturates long before the server does. Benchmark with a tool faster than the
thing being measured.

**One decorator cost 30%.** Security headers were added with FastAPI's `@app.middleware("http")`,
which wraps every request in a task and a stream. Replacing it with a plain ASGI middleware took
single-check throughput from about 1,100 to 1,450 per second.

---

## 20. Mistakes made along the way

Collected in one place, because they are more instructive than the things that went right.

| Mistake | How it was found | Lesson |
|---|---|---|
| Counted the app's own 429s as ill health, so the limiter cut limits because it was limiting | Watching the demo | Define precisely what a signal means |
| Paced continuous-refill providers as if they were fixed-window | Acceptance suite | Test the intent, not just the formula |
| Lease larger than capacity refused forever | Code review | Check the boundary where a request can never succeed |
| Grace window on refresh also applied to logged-out tokens | A test, minutes after writing it | One flag with two meanings is a bug waiting |
| Fixed-window key `subject:index` let a subject named `victim:123` write to the victim's counter | Code review | Never build a key by joining caller-controlled text with a separator |
| Leaky bucket lost precision above 10,000 per second | Code review | Know how many digits your number format keeps |
| Pub/sub listener timed out every 5 seconds when idle and reconnected | Reading the worker's logs after first boot | Tests passed; the real process did not. Run the real thing |
| API crash-looped when Postgres was slow to start | A Docker restart | Wait for dependencies; do not rely on restart policies |
| Logged every request: 1.2 GB in six hours | Leaving the demo running | Leave things running |
| Strict email validation rejected `.local` addresses, which would have locked out the generated admin | A test | Validate what you need, not what a library offers |
| Benchmark client was the bottleneck | A number too bad to be true | Distrust surprising measurements |
| Nginx `add_header` in a location dropped the CSP | Checking response headers by hand | Verify security headers on the real response |

Most were found by running the real system rather than by unit tests. The unit tests were
necessary and caught plenty, but each confirmed the code did what I meant. The demo, the
acceptance suite and the review asked whether what I meant was right.

---

## 21. What I would do next

How this compares with existing tools, and where it is and is not different, is surveyed with
sources in `docs/landscape.md`. In short: each idea exists elsewhere; the combination of a shared
limit that adapts to service health, and pacing from a provider's own quota headers, did not
turn up in any maintained open-source project.

In rough order of value:

1. **Publish the SDKs** to PyPI, npm and Maven Central. Installing from a path is the biggest
   barrier to anyone trying this.
2. **Run it on server hardware and benchmark properly**, with the load generator on a separate
   machine. The current numbers are a laptop's.
3. **Redis high availability.** Redis is a single point of failure. Sentinel would work with the
   current scripts. Cluster needs the per-second counter keys redesigned so every key can be
   declared up front.
4. **Make leasing a single Lua call.** A lease currently makes up to three round trips to find
   what fits. A "grant up to N" mode in the scripts would make it one and remove a small race.
5. **Give decision-log rows a unique stream id** so the at-least-once delivery cannot produce
   duplicates.
6. **A Go or Rust evaluate path.** The single-check ceiling is Python request handling. A small
   compiled service running the same Lua scripts would raise it by an order of magnitude without
   touching anything else.
7. **Per-policy host-signal setting.** It is global today, which is wrong when some protected
   services share the limiter's machine and some do not.
8. **Dashboard:** rule testing ("which rule would match this request?"), and a view of which
   signal drove each limit change over time.

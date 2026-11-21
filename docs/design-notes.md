# Design notes

What I decided while building AdaptiRate, why, and the bugs that tested each decision. The
[architecture page](architecture.md) describes how the system works; this page is about why it
works that way.

## The problem

Most rate limiters enforce a number someone picked once. That number is too high on the day a
database is slow, too low on the day it is idle, and blind to the fact that ten workers are
spending the same OpenAI quota. I wanted three things a fixed limit cannot give:

1. One limit shared exactly across every process, not one limit per process.
2. A limit that tightens when the protected service struggles and loosens when it recovers.
3. Calls to an upstream API paced against the quota that API reports, so the 429 never arrives.

## Decision 1: one atomic script per decision

Every check is a single Redis Lua script. It reads the clock, applies the adaptive multiplier,
checks for a provider pause, runs the algorithm and updates the counters, all in one atomic step.

The alternatives were worse. Read-then-write from application code races: two processes read
"49 used", both write "50", and the limit of 50 admits 51. Redis transactions with `WATCH` fix
the race but retry under contention, which is exactly when a rate limiter is busiest. A Lua
script cannot interleave with anything, so concurrency is correct by construction. The tests
fire 400 concurrent checks at every algorithm and assert the exact admitted count.

Two details mattered:

- **The clock comes from Redis.** If each API process used its own clock, a server a few
  hundred milliseconds ahead would refill buckets early. Asking Redis for `TIME` inside the
  script gives every caller one clock.
- **Postgres is never on the decision path.** Policies and API keys live in an in-memory cache in
  each API process, refreshed by a pub/sub message on every change, with a reload every 30
  seconds as a safety net for a missed message.

**What tested it.** The fixed-window script first stored each window's counter under the key
`<subject>:<window number>`. A caller whose subject was literally `victim:2941` could therefore
write into another user's counter for window 2941. The window number now lives inside the
hash, not the key name. The bug was found in review, not by a test: no test thought to send a
subject containing a colon.

**And again.** The leaky bucket (GCRA) stored its next-allowed time as absolute epoch
milliseconds. Lua's `tostring` keeps 14 significant digits, which at today's epoch is 0.1 ms of
precision. For any rate above about 10,000 requests per second the spacing between requests is
smaller than that, so it rounded to zero and the limit silently stopped applying. It now stores
time relative to a recent fixed epoch, in microseconds.

## Decision 2: the control loop

Once a second the worker scores each policy's health from 0 to 100 and moves its limit. The shape
is borrowed from TCP congestion control: cut multiplicatively when unhealthy, grow additively
when healthy. Cutting by a fraction backs off fast in a crisis; adding a fixed step probes
upward gently.

Three choices keep it stable:

- **The score is 70% the worst signal and 30% the average.** My first version weighted the two
  equally, and a test with latency at eight times its target only cut the limit from 100 to 52,
  because the healthy signals diluted the one that mattered. Weighting the worst signal means
  one real problem is enough to act on, while the average still lets several mild ones add up.
- **There is a dead band.** Below 60 the limit falls, above 80 it rises, and in between it holds.
  The gap stops the limit flapping up and down around a single threshold, and cuts are spaced by
  a cooldown so each one has time to show its effect.
- **The multiplier expires.** The worker writes it to Redis with a 30-second expiry. If the
  worker dies, limits return to their configured values instead of staying stuck wherever the
  last tick left them.

The controller is a pure function, so it is tested against simulated services, including a
closed-loop test where latency rises once load exceeds a fixed capacity: the limit has to settle
near that capacity without collapsing or oscillating. In the recorded demo, a 450 ms slowdown
takes the limit from 20 to 2 requests a second in 15 seconds, and recovery takes it back to 20
in about 25.

**What tested it.** The first time I ran the demo, the limit fell during the *steady* phase,
when nothing was wrong. The cause was a feedback loop: the SDK counted every 429 the service
returned as an "upstream throttle", and the service was returning 429s because the limiter was
limiting. The limiter was cutting the limit because it was limiting. The fix was a rule, now
documented: only 5xx responses count as errors, a 429 the service sends itself is deliberate
load shedding, and throttling by an upstream API has to be reported explicitly.

## Decision 3: leasing, and what it costs

A check is a network round trip, a few milliseconds. For hot paths the SDK can lease a block of
tokens in one call and spend them locally. On a laptop that is 42,500 decisions a second per
process, against about 1,450 for single checks.

The cost is precision, so I bounded it: leases expire after at most two seconds, unspent tokens
are thrown away rather than returned, and the server never grants more than the limit allows. A
lease can under-admit but never over-admit, and an adaptive change reaches every client within
two seconds.

**What tested it.** A Python SDK configured with `lease_size=1000` against a policy of 20 per
second was refused forever. The server tried to debit 1,000 tokens, the algorithm answered "this
can never fit", and the SDK took that as a denial. Leases are now clamped to what the policy can
grant, including when the adaptive engine has temporarily reduced the limit.

## Decision 4: pacing against a provider's quota

When a policy is bound to a provider such as OpenAI, its limit is capped at the provider's
remaining quota spread over the time until that quota resets, and a 429 with `Retry-After`
pauses the policy for every caller at once.

**What tested it.** The end-to-end suite runs a gateway against a fake provider with a quota of
60 a minute, once without AdaptiRate and once with it. The first version passed the headline
check, zero rejections, but a closer look showed the protected gateway was barely doing any
work. Pacing by "remaining" alone meant that once a provider reported 0 remaining, the cap
stayed near zero long after the provider's own `Retry-After` had passed. Providers like OpenAI
and Anthropic refill continuously, so the right rate is the whole quota over the reset window.
After the fix the same run completes 73 calls with no rejections, against 88 completed and 32
rejected without AdaptiRate.

## Smaller lessons

- **Middleware cost 30% of throughput.** FastAPI's `@app.middleware` decorator wraps every
  request in an extra task and stream. Rewriting the security-headers middleware as plain ASGI
  took single-check throughput from about 1,100 to about 1,450 a second.
- **Java's HTTP client lost request bodies.** By default it tries an HTTP/2 upgrade over plain
  `http://`, which uvicorn does not support, and the server received an empty body. The SDK now
  pins HTTP/1.1. Only the tests against a live server caught it; the tests against a stub server
  passed.
- **Spring calls interceptors twice for async handlers**, once per dispatch, so an
  `@AdaptiveRateLimit` on a `Callable` handler was charged twice and leaked an in-flight count.
- **An idle Redis subscription timed out** every five seconds and reconnected, because the
  socket timeout applied to a blocking listen. It was harmless but filled the logs; polling with
  a shorter timeout fixed it.
- **IP addresses have more than one spelling.** `::ffff:10.0.0.1` is `10.0.0.1`. Without
  canonicalising them, a caller could dodge an IP rule by respelling its address.

## How it is tested

| Layer | What it proves |
|---|---|
| Algorithm tests against real Redis | Exact counts under 400 concurrent requests, honest `Retry-After`, no edge bursts |
| Controller simulations | The limit falls, holds and recovers as intended, and settles near a service's capacity |
| API tests against real Redis and Postgres | Every endpoint, every role, token rotation, audit trail |
| SDK tests, against a stub and against a live server | Each SDK behaves the same, and fails safely |
| End-to-end suite | Real services in containers, seven scenarios, measured pass or fail |

Each layer caught something the layer below missed. The unit tests did not find the provider
pacing bug or the Java body bug; the end-to-end suite and the live-server tests did.

## What I would do next

- **A faster decision path.** Single checks are bound by Python request handling. A small Go or
  Rust service doing only `/evaluate`, sharing the same Lua scripts, would raise that ceiling
  without touching the rest.
- **Redis Cluster support.** The scripts build key names at run time, which Cluster forbids.
  Passing every key explicitly, with hash tags per policy, would allow it.
- **Rollouts for policy changes.** Today a change applies to everyone at once. A percentage
  rollout, or a dry-run mode that logs what a new limit *would* have blocked, would make changes
  safer.

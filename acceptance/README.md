# Acceptance suite

Unit tests show the pieces work. This shows the whole thing works: three small services are
started as real containers, wired to a running AdaptiRate through the SDKs, and driven with real
HTTP traffic. Each scenario makes one claim, measures it and prints PASS or FAIL.

```bash
docker compose up -d          # the normal stack, if it is not already running
bash acceptance/run.sh        # about four minutes
```

The script creates its own policies and API key, starts the test services, runs the scenarios,
removes everything it created and exits non-zero if anything failed. It never stops the main
stack. The last run's results are in [REPORT.md](REPORT.md).

## The services

| Service | What it is | SDK |
|---|---|---|
| `orders-api` | A FastAPI order service, run as three replicas | Python (ASGI middleware) |
| `orders-adaptive` | The same service under an adaptive policy, with a switch to make it slow | Python |
| `llm-gateway` | An Express service that calls an upstream "LLM" API | Node |
| `fake-provider` | That upstream API: 60 requests a minute, real rate-limit headers, 429 with `Retry-After` when exhausted | none |

## The scenarios

| | Claim | How it is checked |
|---|---|---|
| S1 | Three replicas share one exact limit | 300 concurrent requests from one user across all replicas: exactly 50 succeed |
| S2 | Users are isolated; rules change limits without code | A second user is unaffected; a premium role gets exactly its higher limit; a banned user is refused |
| S3 | The limit falls when the service slows and recovers afterwards | 600 ms of latency is injected; the limit must halve within 25 s and return within 45 s |
| S4 | Upstream calls are paced so the provider stops rejecting them | Same load with and without AdaptiRate; count the provider's 429s |
| S5 | A provider's `Retry-After` pauses every caller, then lifts | Pause appears, lifts on schedule, and lifts at once when the provider state is deleted |
| S6 | Limits and algorithms change live | PATCH the policy mid-run; the next burst obeys the new limit; no process restarts |
| S7 | Services keep limiting if AdaptiRate is down | The API container is paused; services still answer and still refuse excess traffic |

S4 is the one to look at first. Typical numbers from this machine: without AdaptiRate the
gateway collected 32 rejections from the provider out of 120 calls; with it, none.

## What it has caught

This suite found two real defects that 150 unit tests had not, both in provider pacing: a
provider reporting "0 remaining" kept a policy near zero long after its own `Retry-After` had
expired, and deleting a provider's state left the reduced limit in place for a second. Both are
fixed and covered by tests now. It is worth running after any change to the limiter, the worker
or an SDK.

## Showing it to someone

Five minutes, two windows.

1. Open the dashboard at http://localhost:8080 on the Dashboard page.
2. Run `bash acceptance/run.sh` in a terminal next to it.
3. S1 and S2 finish in seconds. Point at the line `200=50 429=250, served by 3 replicas`: three
   separate processes, one shared limit, not one request over.
4. During S3 (about 40 seconds), watch the policy named `acc-...-adaptive` on the dashboard. Its
   limit drops while the service is slow and climbs back when it recovers. Nobody touched it.
5. S4 prints the before and after counts of provider rejections. That is the case for using this
   in front of a paid API.
6. S7 pauses the AdaptiRate API on purpose. The services keep answering and keep limiting from
   the last limit they were told.
7. Open `acceptance/REPORT.md` for the table to send afterwards.

For a longer, free-running demo with a traffic generator instead of assertions, use
`docker compose --profile demo up -d --build`.

## Notes

- Offered load is kept modest (under about 150 requests a second) so the suite is reliable on a
  laptop. It is a correctness test, not a benchmark; see `bench/` for throughput.
- S3 turns off the "use host CPU and memory" fallback for the duration of the run, so a busy
  laptop does not influence the result, and restores the setting afterwards.
- The fixed-window scenarios wait for a fresh window when one is about to end, so counts are exact.

# Benchmarks

## Results

Measured on a 4-core laptop (Windows 11, Docker Desktop on WSL2, 8 GB given to Docker), with
the stock `docker compose up` configuration: two API worker processes, one Redis, everything
on the same machine as the load generator. Zero failed requests in every run.

| Mode | Throughput | Latency | How it was measured |
|---|---|---|---|
| Single check, `POST /evaluate` | about 1,450 decisions/sec | p50 11 ms, p99 18 ms at 16 connections | ApacheBench, keep-alive |
| Batch, `POST /evaluate/batch` (100 per call) | about 36,000 decisions/sec | p99 43 ms per call at 8 connections | ApacheBench, keep-alive |
| Leased (SDK decides locally) | about 42,500 decisions/sec per SDK process | in-process, no network on most calls | `bench.py`, Python SDK, `lease_size=1000` |

Reading these honestly:

- **Single check** is one HTTP request, one Redis round trip. It is bound by Python request
  handling, so it scales with API worker processes and cores (`AR_API_WORKERS`).
- **Batch** pays the HTTP cost once per 100 decisions; Redis does the 100 checks in one pipeline.
- **Leased** is a client-side number: the SDK reserves a block of tokens and spends them in
  process. Each service process gets this rate independently, so total capacity grows with the
  number of processes. The cost is precision: unused leased tokens are discarded, and a limit
  change takes up to one lease (at most 2 seconds) to reach every client.
- The 50,000/sec target from the original brief was **not** reached by any single mode on this
  machine. Batch and leased modes are within reach of it on server hardware; that has not been
  measured.

## Scaling with API workers

Same machine, `AR_API_WORKERS` set to 1, 2, 4 and 8, ApacheBench with 32 connections (64 for 8
workers), 15,000 to 20,000 requests per run after a warm-up, zero failures.

| API workers | Checks per second | p50 | p99 |
|---|---|---|---|
| 1 | 723 | 44 ms | 49 ms |
| 2 | 1,540 | 21 ms | 33 ms |
| 4 | 2,909 | 10 ms | 26 ms |
| 8 | 2,379 | 24 ms | 74 ms |

Throughput scales almost linearly up to one worker per core, then falls once processes compete
for the 4 cores (the load generator shares them too). During the 8-worker run Redis used 22% of
one core and the API processes 334%, so Redis is not the limit; adding API processes or machines
is.

```bash
AR_API_WORKERS=4 docker compose up -d api
```

## Reproduce

Start the stack (`docker compose up -d`), create a policy named `bench` with a very high limit
and a key with the Evaluate permission, then:

```bash
# Single check
echo '{"policy":"bench","user":"u1"}' > body.json
docker run --rm --network adaptirate_default -v "$PWD/body.json:/b.json" alpine:3.20 sh -c \
  'apk add -q apache2-utils && ab -k -c 16 -n 8000 -p /b.json -T application/json \
   -H "X-API-Key: ar_..." http://api:8000/api/v1/evaluate'

# Batch and leased (creates and removes its own policy and key)
ADAPTIRATE_URL=http://localhost:8000 \
ADAPTIRATE_ADMIN_EMAIL=... ADAPTIRATE_ADMIN_PASSWORD=... \
python bench.py --seconds 15
```

`bench.py` uses a Python HTTP client, which saturates well before the server does. Its batch
figure is therefore a lower bound; use ApacheBench with a 100-check body for the real one.

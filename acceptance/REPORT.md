# AdaptiRate acceptance report

- Run at: 2026-11-15 11:40:21 UTC
- Machine CPUs (as seen by Docker): 4
- Result: 7 of 7 scenarios passed (ACCEPTED)

| Scenario | Claim | Measured | Result | Seconds |
|---|---|---|---|---|
| S1 Exact shared limit across replicas | Three replicas of a service share one exact per-user limit. | 300 concurrent -> 200=50 429=250, served by 3 replicas [16, 17, 17] | PASS | 2.6 |
| S2 Isolation and rules | One user cannot use up another's budget, and rules change limits per user or role without code. | default user 50/80, other user 20/20, premium 120/150, banned 0/6 (rule_deny), Retry-After=46s | PASS | 2.4 |
| S3 Adaptive limit under latency | When the service gets slow the limit falls on its own, and it recovers when the service does. | limit 40 -> 13 (cut in 4 s) -> 36 (recovered in 22 s); client p50 latency 19 ms -> 616 ms during slowdown | PASS | 39.5 |
| S4 Provider pacing, before and after | Calls to a rate-limited upstream API are paced so the gateway collects far fewer 429s. | 120 requests at 4/s against a 60/min provider: control upstream 429s=32 (200s=88); with AdaptiRate upstream 429s=0 (200s=73, paced locally=47) | PASS | 64.8 |
| S5 Retry-After pause | After an upstream 429 every caller is told to wait, and the pause lifts on schedule or on demand. | upstream 429 -> paused (retry_after 5990 ms), lifted after 6.2 s; DELETE provider lifted a 60 s pause at once | PASS | 6.4 |
| S6 Live reconfiguration | Changing a policy's limit and algorithm takes effect on the next burst with no restart. | 50/min fixed -> 25 sliding: 25 allowed; then 10: 10 allowed; replicas not restarted | PASS | 1.3 |
| S7 Outage behaviour (fail_mode=local) | If AdaptiRate goes down the service keeps answering and keeps limiting, then resumes exact limits. | AdaptiRate paused: 240 requests -> 200=90 429=150 (local fallback); after unpause: attempt 1: 200=30 429=70 reasons ['limit'] | PASS | 3.7 |

## Details

### S1 Exact shared limit across replicas - PASS

- ok: service resolves to all replicas (3 addresses)
- ok: exactly 50 requests returned 200 (got 50)
- ok: exactly 250 requests returned 429 (got 250)
- ok: no other outcome (statuses {200: 50, 429: 250})
- ok: at least 2 distinct replicas served 200s (3 replicas: [16, 17, 17])

### S2 Isolation and rules - PASS

- ok: greedy user limited to exactly 50 (200=50 429=30)
- ok: a different user is unaffected (20 of 20 allowed) (200=20 429=0)
- ok: greedy user is still blocked afterwards (statuses {429: 5})
- ok: role=premium rule gives exactly 120 (200=120 429=30)
- ok: deny rule blocks the user with reason rule_deny (statuses {429: 6} reasons ['rule_deny'])
- ok: 429 carries Retry-After (Retry-After='46')
- ok: 429 carries X-RateLimit-Limit/Remaining/Reset (limit=50 remaining=0 reset=46)

### S3 Adaptive limit under latency - PASS

- ok: baseline limit is near the configured 40 under healthy traffic (baseline=40)
- ok: chaos (+600 ms) applied to all replicas (3 replicas)
- ok: limit fell to <= 50% of baseline within 25 s (cut after 4 s, now 19)
- ok: chaos cleared on all replicas (3 replicas)
- ok: limit returned to >= 90% of baseline within 45 s (recovered after 22 s, now 36)
- note: limit timeline (seconds:limit) 1:40 2:40 3:40 4:40 5:40 6:40 7:40 8:40 9:40 10:40 11:40 12:27 13:27 14:19 15:19 16:19 17:13 18:13 19:9 20:9 21:6 22:6 23:4 24:6 25:8 26:10 27:12 28:14 29:16 30:18 31:20 32:22 33:24 34:26 35:28 36:30 37:32 38:34 39:36

### S4 Provider pacing, before and after - PASS

- ok: control run hit the provider's limit (> 10 upstream 429s) (control upstream_429=32)
- ok: protected run had <= 20% of the control's upstream 429s (protected upstream_429=0 vs control 32)
- ok: protected run still completed useful work (>= 30 successful calls) (protected 200s=73, held back by AdaptiRate=47)
- ok: gateway counters agree with the provider's own counters (provider rejected control=32 protected=0)

### S5 Retry-After pause - PASS

- ok: provider rejected the call with 429 and the gateway reported it (gateway answered 429)
- ok: next check is denied with reason paused and retry_after_ms > 0 (allowed=False reason=paused retry_after_ms=5990)
- ok: gateway sheds the call locally while paused (no upstream call) (status 429 source gateway)
- ok: pause lifts by itself after the provider's Retry-After (cleared after 6.2 s (advertised 6.0 s))
- ok: long pause is in force (reason=paused)
- ok: DELETE provider lifts the pause immediately (allowed=True reason=ok)

### S6 Live reconfiguration - PASS

- ok: PATCH accepted (algorithm=sliding_window, limit=25) (sliding_window 25)
- ok: next burst is held to exactly 25 (200=25 429=75)
- ok: a second change (limit=10) applies to the following burst (200=10 429=50)
- ok: no service restarted (same process ids before and after) (3 replicas unchanged)

### S7 Outage behaviour (fail_mode=local) - PASS

- ok: service kept answering during the outage (no errors or timeouts) (statuses {200: 90, 429: 150})
- ok: some requests were allowed (200=90)
- ok: some requests were still limited (429=150)
- ok: local limiting stayed near the policy (3 replicas x 30), not unlimited (200=90, ceiling 105)
- ok: limits came from the local fallback (429 reasons ['fail_local'])
- ok: after unpause the exact server-side limit is back (30 of 100, reason limit) (attempt 1: 200=30 429=70 reasons ['limit'])

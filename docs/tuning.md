# Tuning

## Choosing an algorithm

| You want | Use | Set `burst` to |
|---|---|---|
| A steady rate that tolerates short bursts | Token bucket | How far above the limit a burst may go |
| A strict quota per window ("1,000 per hour, no more") | Sliding window | (unused) |
| Coarse daily or monthly allowances, lowest cost | Fixed window | (unused) |
| Evenly spaced calls to a fragile downstream | Leaky bucket | How many calls may arrive together |
| An adaptive policy that should stay smooth under pressure | Adaptive hybrid | Bucket size; defaults to a fifth of the limit |

You can switch a policy's algorithm at any time from the Algorithms page. Each algorithm keeps
separate counters, so a switch starts clean.

## Choosing how to check

| Mode | Cost per decision | Precision | Use for |
|---|---|---|---|
| Single check | One network round trip (a few ms) | Exact | Most request handlers |
| Batch | One round trip per batch | Exact | Queue consumers, fan-out jobs |
| Leased (`lease_size`) | Almost none | Limit changes reach the client within 2 s; unused leased tokens are lost | Very hot paths |

For leasing, a lease of about a tenth of a second's worth of traffic is a good start: for a
1,000/sec limit, `lease_size=100`. Larger leases mean fewer round trips but more tokens stranded
in idle processes.

## Tuning the adaptive engine

All of these are on the Settings page and apply within a second.

**Start with the target latency.** Set `target_latency_ms` on each adaptive policy to the p95 you
consider healthy for that service. Latency at the target scores no penalty; at three times the
target (the critical factor) it scores the maximum.

**If limits drop too eagerly:**

- Raise the policy's `min_limit`, so there is a floor it never goes below.
- Lower *Decrease below health* (default 60), or lower *Largest cut* (default 0.5).
- Lengthen the *Signal window* (default 5 s) so brief spikes are averaged out.
- If AdaptiRate is not on the same machine as the service, turn off *Use host signals*: this
  host's CPU says nothing about that service.

**If limits recover too slowly:** raise the *Recovery step* (default 5% of the base limit per
second). At the default, recovering from the floor to the full limit takes about 20 seconds.

**If limits oscillate:** widen the gap between the decrease and increase thresholds, or lengthen
the *Cooldown between cuts* so each cut has time to show its effect before the next.

**Priority** (0–100 per policy) decides who is cut hardest. Under the same stress, a priority-100
policy is cut half as hard as a priority-50 one, and a priority-0 policy 1.5 times as hard. Give
checkout a higher priority than search.

## Provider quotas

Bind a policy to a provider (`provider: "openai"`) and report that provider's responses from the
SDK (`report_provider`, or the `httpx_hook` helper in Python). AdaptiRate then:

- limits the policy to the provider's remaining quota spread over the time until it resets,
  keeping a safety margin (default 10%) unspent, and
- pauses the policy for the provider's `Retry-After` when a 429 arrives.

For an API that is not built in, define which headers carry its quota with
`PUT /providers/{name}`.

## The decision log

Counters, analytics and the live charts are always exact. The searchable log is a sample, because
writing a row for every request does not scale: by default it keeps every blocked request and one
in ten allowed ones, up to 100 events per second per API process, and at most a million rows.
Raise *Share of allowed requests logged* on the Settings page if you need more detail and have
the disk for it.

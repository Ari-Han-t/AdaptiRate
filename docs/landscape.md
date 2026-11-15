# Related tools

How AdaptiRate compares with what people already use. Surveyed on 2026-11-15 from primary
sources: GitHub (release tags, last push, licence), vendor documentation, and each AI provider's
rate-limit documentation. Star counts are approximate. Anything not confirmed from a source is
marked unverified. This is a survey, not an exhaustive census; newer and smaller projects are the
least checked.

## Summary

AdaptiRate combines four ideas. Each exists elsewhere:

| Idea | Who already does it |
|---|---|
| A limit shared by every server, held in Redis | Almost every library and gateway below |
| A central service that answers yes or no | Envoy's ratelimit service, Upstash, Unkey |
| Limits that adapt to how the service is doing | Netflix concurrency-limits, Envoy adaptive concurrency, Alibaba Sentinel, Uber Cinnamon |
| Pacing calls to an upstream AI provider | LiteLLM, Kong AI Gateway, Envoy AI Gateway, Azure API Management |

What I did not find in any maintained open-source project:

1. **A shared limit that adapts to service health.** The adaptive tools adapt each server on its
   own; the shared limiters do not adapt. The one project that did both, FluxNinja Aperture, has
   been effectively dormant since CodeRabbit acquired FluxNinja: its last release was January
   2024.
2. **Pacing from the provider's own quota headers.** Every AI gateway checked counts usage with
   its own counters against limits you configure, then reacts to 429s. None documents reading a
   provider's `remaining` and `reset` headers to slow down before the rejection.

Where AdaptiRate is weaker:

- It is not a proxy, so unlike LiteLLM or Portkey it cannot retry on another provider or model,
  cache, or route.
- It needs a single Redis, with no Redis Cluster support.
- It has never run in production.
- Its SDKs are not published to package registries.

## Libraries

Run inside your application. All are self-hosted, have no dashboard, and use limits you set.

| Library | Language | Licence | Shared across servers | Algorithms | Adapts to health | Latest release |
|---|---|---|---|---|---|---|
| slowapi | Python | MIT | Through `limits` storage (Redis, Memcached) | From `limits` | No | 0.1.10, Jun 2026 |
| limits | Python | MIT | Redis, Memcached, MongoDB | Fixed, sliding and moving window (unverified) | No | 5.8.0, Feb 2026 |
| express-rate-limit | Node | MIT | Through external stores | Fixed window (unverified) | No | 8.7.4, Nov 2026 |
| rate-limiter-flexible | Node | ISC | Redis, Mongo, Memcached, DynamoDB and others | Several (unverified) | No | 11.4.1, Nov 2026 |
| Bucket4j | Java | Apache-2.0 | JCache, Hazelcast, Redis and others | Token bucket | No | 8.22.0, Nov 2026 |
| Resilience4j RateLimiter | Java | Apache-2.0 | No, one JVM | Permits per refresh period | No (limits can be changed by hand at run time) | 2.4.0, Mar 2026 |
| redis-cell | Redis module | MIT | Yes, runs inside Redis | GCRA | No | 0.5.0, Nov 2025 |

## Hosted services

| Service | What it is | Open source | Adapts to health | Dashboard | Pricing (as published) |
|---|---|---|---|---|---|
| Upstash Ratelimit | MIT SDK over Upstash's hosted Redis; fixed window, sliding window, token bucket | SDK yes, backend no | No (limits can be set dynamically by your code) | Yes | Redis pay-as-you-go $0.20 per 100k commands; fixed plans $10 to $1,500 a month |
| Unkey | API keys with a rate-limit API on any identifier | Licence unverified (one fork's text says AGPL) | No | Yes | Free tier; Pro $25 to $1,000 a month; rate-limited requests not billed |

Unkey has broadened into deployments and a gateway during 2026. A few very small hosted
rate-limiting launches appeared in 2026 (for example Limitly); none has visible traction.

## Gateways and proxies

Sit in front of your services. Proven at very large scale. None adapts limits to backend health.

| Gateway | Licence | Shared across instances | Notes |
|---|---|---|---|
| Envoy global rate limit (`envoyproxy/ratelimit`) | Apache-2.0 | Redis or Memcached | The closest analogue to AdaptiRate's decision service. Ships as images rather than tagged releases. No dashboard. |
| Kong `rate-limiting` | Apache-2.0 | Local, cluster or Redis | Fixed windows. |
| Kong `rate-limiting-advanced` | Enterprise | Redis, Sentinel, Cluster; falls back to local counting | Sliding windows; since 3.16 the limit can be a per-request expression. Dashboard in Konnect. |
| Nginx `limit_req` | BSD-style | No; cross-instance sync is Nginx Plus only | Leaky bucket. |
| Traefik | MIT (unverified) | In memory, or Redis when configured | Token bucket. Some distributed features are in the commercial Traefik Hub. |
| Cloudflare Rate Limiting | Hosted | Yes | Free plan: 1 rule, 10-second periods. Enterprise: 100 rules, many counting fields. |
| AWS API Gateway throttling | Hosted | Yes | Token bucket; AWS describes limits as best-effort targets. |
| AWS WAF rate-based rules | Hosted | Yes | Aggregates on custom keys over an evaluation window. |

## Adaptive tools

These change their limits based on how the service behaves. Most adapt **concurrency** (how many
requests in flight) rather than a **rate**, and most act on each server separately.

| Tool | Adapts | Signals | Shared across servers | Dashboard | Status |
|---|---|---|---|---|---|
| Netflix concurrency-limits | Concurrency, per instance | Latency (Vegas, Gradient2), drops (AIMD) | No | No | Active; 0.5.4, Dec 2025 |
| Envoy adaptive concurrency filter | Concurrency, per proxy | Latency against a sampled minimum round-trip time | No | Stats only | Part of Envoy |
| Alibaba Sentinel | QPS and concurrency; "system adaptive protection" | Machine load, CPU, response time, threads | Cluster mode with a token server | Yes | Active; 1.8.10, May 2026; about 23k stars |
| Uber Cinnamon | Concurrency, with priority-based shedding | Modified TCP Vegas, PID controller | No | Internal | Not open source |
| Google SRE adaptive throttling | Client-side rejection probability | Accepted versus attempted requests | No | No | A published technique, not a product |
| go-zero adaptive shedding | Load shedding, per instance | CPU and in-flight requests (unverified) | No | No | Framework very active |
| FluxNinja Aperture | Rates, concurrency, scheduling | Configurable signals in a policy model | Yes, through its agents | Yes (cloud) | Dormant: acquired by CodeRabbit; last release Jan 2024 |

Sentinel is the closest actively maintained project. Its adaptive mode protects the whole machine
from load rather than adjusting each policy from its own health, and it has no notion of upstream
provider quotas.

## AI gateways and LLM rate-limit tools

All of these are proxies: your AI traffic flows through them. That lets them do things
AdaptiRate cannot (retries on another provider, fallbacks, caching, cost tracking) at the price of
routing every call through them.

| Tool | Licence | Rate limits | Shared across instances | Paces from provider headers | Status |
|---|---|---|---|---|---|
| LiteLLM | Open-source core; enterprise features licensed | Requests and tokens per key, team, user, model | Redis | No; uses configured limits, cools a deployment down on 429 | Very active; about 63k stars |
| Portkey Gateway | MIT (one source says Apache-2.0); hosted platform proprietary | Rate-limit features unverified; retries and fallbacks | Unverified | Unverified | Acquired by Palo Alto Networks, May 2026 |
| Helicone | Open source | Not checked | Not checked | Not checked | Acquired by Mintlify, Mar 2026; maintenance mode |
| OpenRouter | Closed, hosted | Provider 429s handled by fallback routing | Hosted | Unverified | Active |
| Cloudflare AI Gateway | Hosted | Requests per window (not tokens), spend limits | Hosted | Unverified | Active; core features free |
| Kong AI Rate Limiting Advanced | Enterprise | Tokens, per provider | Redis | No | Active |
| Envoy AI Gateway | Open source | Tokens, through Envoy's global rate limit | Redis | No | Active |
| Azure API Management `llm-token-limit` | Proprietary | Tokens per minute and quotas per key | Per gateway, not aggregated across regions | No | Active; renamed from `azure-openai-token-limit` |
| Bifrost (Maxim) | Apache-2.0 | Listed under governance | Clustering in enterprise tier | Unverified | Active |
| TensorZero | Apache-2.0 | Custom limits by tag | Unverified | Unverified | Archived, Jun 2026 |

## How the providers refill their quotas

This decides how pacing should work, so it was checked against each provider's own documentation.

| Provider | Headers | Refill | Notes |
|---|---|---|---|
| Anthropic | `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}` | Token bucket, continuously replenished (documented) | Some 429s have no `retry-after`, notably the monthly spend cap, which clears on the 1st. A newer "ramp limit" 429 does carry `retry-after`. |
| OpenAI | `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` | Not documented | `reset` is "time until the rate limit resets to its initial state". AdaptiRate assumes continuous refill; override per provider if that proves wrong. |
| Groq | `x-ratelimit-*`, same names as OpenAI | Not stated | Request headers are per day; token headers are per minute. |
| Gemini | No rate-limit headers documented | Per project; daily quota resets at midnight Pacific | Only the 429 itself is usable. |
| GitHub | `x-ratelimit-{limit,remaining,reset}` | Fixed hourly window | Exhaustion is a 403 with remaining 0. |

Changes made to AdaptiRate as a result of this check:

- Repeated 429s with no `Retry-After` now back off up to an hour instead of a minute, so a spend
  cap that lasts weeks is not probed every minute.
- Pacing of Groq's daily request quota is covered by a test (an empty daily bucket paces at its
  refill rate).
- The OpenAI refill model is stated as an assumption in the code and docs.

## Sources

Libraries and gateways:
- https://api.github.com/repos/laurentS/slowapi
- https://api.github.com/repos/alisaifee/limits
- https://api.github.com/repos/express-rate-limit/express-rate-limit
- https://api.github.com/repos/animir/node-rate-limiter-flexible
- https://api.github.com/repos/bucket4j/bucket4j
- https://api.github.com/repos/resilience4j/resilience4j
- https://api.github.com/repos/brandur/redis-cell
- https://api.github.com/repos/envoyproxy/ratelimit
- https://api.github.com/repos/Kong/kong
- https://developer.konghq.com/plugins/rate-limiting-advanced/
- https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
- https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ratelimit/
- https://developers.cloudflare.com/waf/rate-limiting-rules/
- https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html
- https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-rate-based.html

Hosted services:
- https://upstash.com/docs/redis/sdks/ratelimit-ts/overview
- https://upstash.com/pricing/redis
- https://www.unkey.com/pricing
- https://unkey.com/api/changelog/2026-07-10

Adaptive tools:
- https://github.com/Netflix/concurrency-limits
- https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter
- https://github.com/alibaba/Sentinel
- https://sentinelguard.io/en-us/docs/system-adaptive-protection.html
- https://github.com/fluxninja/aperture
- https://www.uber.com/blog/cinnamon-using-century-old-tech-to-build-a-mean-load-shedder/
- https://www.uber.com/blog/cinnamon-auto-tuner-adaptive-concurrency-in-the-wild
- https://sre.google/sre-book/handling-overload/
- https://api.github.com/repos/zeromicro/go-zero

AI gateways and providers:
- https://docs.litellm.ai/docs/proxy/dynamic_rate_limit
- https://docs.litellm.ai/docs/routing
- https://docs.litellm.ai/docs/proxy/redis_requirements
- https://github.com/BerriAI/litellm
- https://github.com/Portkey-AI/gateway
- https://www.helicone.ai/blog/joining-mintlify
- https://openrouter.ai/docs/api/reference/limits
- https://developers.cloudflare.com/ai-gateway/features/
- https://developer.konghq.com/ai-gateway/policies/ai-rate-limiting-advanced/
- https://aigateway.envoyproxy.io/docs/capabilities/usage-based-ratelimiting
- https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy
- https://github.com/maximhq/bifrost
- https://github.com/tensorzero/tensorzero
- https://platform.claude.com/docs/en/api/rate-limits
- https://developers.openai.com/api/docs/guides/rate-limits
- https://console.groq.com/docs/rate-limits
- https://aistudio.google.com/docs/rate-limits

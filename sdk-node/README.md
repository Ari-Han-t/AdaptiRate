# @adaptirate/sdk

Node.js SDK for [AdaptiRate](https://github.com/Ari-Han-t/AdaptiRate), a self-hosted rate limiter
whose limits are shared across every process and adapt to latency, errors and upstream API
quotas.

You need a running AdaptiRate server; see the
[main repository](https://github.com/Ari-Han-t/AdaptiRate) to start one with Docker Compose.

```bash
npm install @adaptirate/sdk
```

Set the server address and an API key (created on the dashboard's API Keys page):

```bash
export ADAPTIRATE_URL=http://localhost:8080
export ADAPTIRATE_API_KEY=ar_...
```

## Express

```js
const { AdaptiveLimiter } = require('@adaptirate/sdk')
const { adaptiveLimiter } = require('@adaptirate/sdk/express')

const limiter = new AdaptiveLimiter()
app.use(adaptiveLimiter({ limiter, policy: 'api', identify: (req) => ({ user: req.user?.id, ip: req.ip }) }))
```

Blocked requests get `429` with `Retry-After` and `X-RateLimit-*` headers, and every request's
latency and status is reported so the limit can adapt.

## NestJS

```ts
import { APP_GUARD, APP_INTERCEPTOR } from '@nestjs/core'
import { AdaptiveLimiter } from '@adaptirate/sdk'
import { ADAPTIVE_LIMITER, AdaptiveRateLimit, AdaptiveRateLimitGuard, AdaptiveRateLimitInterceptor } from '@adaptirate/sdk/nest'

@Module({
  providers: [
    { provide: ADAPTIVE_LIMITER, useFactory: () => new AdaptiveLimiter() },
    { provide: APP_GUARD, useClass: AdaptiveRateLimitGuard },
    { provide: APP_INTERCEPTOR, useClass: AdaptiveRateLimitInterceptor },
  ],
})
export class AppModule {}

@Controller('orders')
export class OrdersController {
  @Post()
  @AdaptiveRateLimit('orders')
  create() {}
}
```

## One operation

```js
const decision = await limiter.check('send-email', { user: userId })
if (!decision.allowed) {
  // decision.retryAfterMs says when to try again
}

await limiter.acquire('send-email', { user: userId }) // throws RateLimitExceededError
await limiter.wait('send-email', { user: userId })    // waits until allowed

const send = limiter.wrap('emails', sendEmail, (userId) => ({ user: userId }))
```

## Pace calls to an upstream API

```js
await limiter.wait('openai-calls', { key: 'my-service' })
const response = await fetch('https://api.openai.com/v1/chat/completions', { ... })
await limiter.reportProvider('openai', response.status, response.headers)
```

AdaptiRate reads the provider's rate-limit headers, spreads the remaining quota across every
process using the policy, and pauses them all when the provider answers 429.

## Options

| Option | Default | Meaning |
|---|---|---|
| `failMode` | `'open'` | When the server is unreachable: `'open'` allows, `'closed'` denies, `'local'` keeps enforcing the last known limit in process |
| `leaseSize` | `0` | Fetch this many permits at once and decide locally; for very hot paths |
| `timeoutMs` | `1000` | Milliseconds per request to the server |
| `reportIntervalMs` | `1000` | Milliseconds between latency and error reports; `0` disables the timer |

Requires Node.js 18 or later. NestJS and RxJS are optional peer dependencies, needed only for
`@adaptirate/sdk/nest`.

## License

MIT

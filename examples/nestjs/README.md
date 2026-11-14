# NestJS

```bash
npm install /path/to/AdaptiRate/sdk-node
```

**app.module.ts**

```ts
import { Module } from '@nestjs/common'
import { APP_GUARD, APP_INTERCEPTOR } from '@nestjs/core'
import { AdaptiveLimiter } from '@adaptirate/sdk'
import { ADAPTIVE_LIMITER, AdaptiveRateLimitGuard, AdaptiveRateLimitInterceptor } from '@adaptirate/sdk/nest'

@Module({
  providers: [
    // Reads ADAPTIRATE_API_KEY and ADAPTIRATE_URL.
    { provide: ADAPTIVE_LIMITER, useFactory: () => new AdaptiveLimiter({ failMode: 'local' }) },
    { provide: APP_GUARD, useClass: AdaptiveRateLimitGuard },             // enforces the limit
    { provide: APP_INTERCEPTOR, useClass: AdaptiveRateLimitInterceptor }, // reports latency and errors
  ],
})
export class AppModule {}
```

**orders.controller.ts**

```ts
import { Controller, Post, Req } from '@nestjs/common'
import { AdaptiveRateLimit } from '@adaptirate/sdk/nest'

@Controller('orders')
@AdaptiveRateLimit('orders', { identify: (req) => ({ user: req.user.id, ip: req.ip }) })
export class OrdersController {
  @Post()
  create() { ... }

  @Post('bulk')
  @AdaptiveRateLimit('orders', { cost: 10, identify: (req) => ({ user: req.user.id }) }) // overrides the class
  bulk() { ... }
}
```

Handlers without the decorator are not limited. Without `identify`, the client address is used.
Blocked requests get `429` with `Retry-After` and `X-RateLimit-*` headers.

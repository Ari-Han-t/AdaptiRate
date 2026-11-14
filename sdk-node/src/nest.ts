// NestJS integration: a decorator plus a guard and interceptor.
//
//     @Module({
//       providers: [
//         { provide: ADAPTIVE_LIMITER, useValue: new AdaptiveLimiter() },
//         { provide: APP_GUARD, useClass: AdaptiveRateLimitGuard },
//         { provide: APP_INTERCEPTOR, useClass: AdaptiveRateLimitInterceptor },
//       ],
//     })
//     export class AppModule {}
//
//     @Controller('orders')
//     export class OrdersController {
//       @Post()
//       @AdaptiveRateLimit('orders')
//       create() { ... }
//     }

import { HttpException, Inject, Injectable, SetMetadata } from '@nestjs/common'
import type { CallHandler, CanActivate, ExecutionContext, NestInterceptor } from '@nestjs/common'
import { Reflector } from '@nestjs/core'
import { Observable } from 'rxjs'
import { decisionHeaders } from './core'
import type { Subject } from './core'
import type { AdaptiveLimiter } from './index'

export const ADAPTIVE_LIMITER = Symbol('ADAPTIVE_LIMITER')
const METADATA_KEY = 'adaptirate:limit'

interface HttpRequest {
  ip?: string
  socket?: { remoteAddress?: string }
}
interface HttpResponse {
  statusCode: number
  setHeader?(name: string, value: string): void
  header?(name: string, value: string): void
}

export interface RateLimitOptions {
  /** Who is calling. Defaults to the client address. */
  identify?: (request: any) => Subject
  cost?: number
}

interface RateLimitMetadata extends RateLimitOptions {
  policy: string
}

/** Rate-limit a route handler or a whole controller against a policy. */
export const AdaptiveRateLimit = (policy: string, options: RateLimitOptions = {}) =>
  SetMetadata(METADATA_KEY, { policy, ...options } satisfies RateLimitMetadata)

function metadata(reflector: Reflector, context: ExecutionContext): RateLimitMetadata | undefined {
  return reflector.getAllAndOverride<RateLimitMetadata | undefined>(METADATA_KEY, [context.getHandler(), context.getClass()])
}

function setHeader(response: HttpResponse, name: string, value: string): void {
  // Express exposes setHeader; Fastify replies expose header().
  if (typeof response.setHeader === 'function') response.setHeader(name, value)
  else response.header?.(name, value)
}

@Injectable()
export class AdaptiveRateLimitGuard implements CanActivate {
  constructor(
    private readonly reflector: Reflector,
    @Inject(ADAPTIVE_LIMITER) private readonly limiter: AdaptiveLimiter,
  ) {}

  async canActivate(context: ExecutionContext): Promise<boolean> {
    const config = metadata(this.reflector, context)
    if (!config || context.getType() !== 'http') return true

    const http = context.switchToHttp()
    const request = http.getRequest<HttpRequest>()
    const response = http.getResponse<HttpResponse>()
    const subject = config.identify ? config.identify(request) : { ip: request.ip ?? request.socket?.remoteAddress ?? 'unknown' }
    const decision = await this.limiter.check(config.policy, { ...subject, cost: config.cost ?? 1 })

    for (const [name, value] of Object.entries(decisionHeaders(decision))) setHeader(response, name, value)
    if (!decision.allowed) {
      throw new HttpException(
        { detail: 'Rate limit exceeded', policy: decision.policy, reason: decision.reason, retry_after_ms: decision.retryAfterMs },
        429,
      )
    }
    return true
  }
}

/** Reports each limited handler's latency and outcome to the adaptive engine. */
@Injectable()
export class AdaptiveRateLimitInterceptor implements NestInterceptor {
  constructor(
    private readonly reflector: Reflector,
    @Inject(ADAPTIVE_LIMITER) private readonly limiter: AdaptiveLimiter,
  ) {}

  intercept(context: ExecutionContext, next: CallHandler): Observable<unknown> {
    const config = metadata(this.reflector, context)
    if (!config || context.getType() !== 'http') return next.handle()

    const limiter = this.limiter
    return new Observable((subscriber) => {
      limiter.started(config.policy)
      const start = performance.now()
      let settled = false
      const settle = (status: number) => {
        if (settled) return
        settled = true
        limiter.finished(config.policy, performance.now() - start, { status })
      }
      const subscription = next.handle().subscribe({
        next: (value) => subscriber.next(value),
        error: (error: unknown) => {
          settle(error instanceof HttpException ? error.getStatus() : 500)
          subscriber.error(error)
        },
        complete: () => {
          settle(context.switchToHttp().getResponse<HttpResponse>().statusCode)
          subscriber.complete()
        },
      })
      return () => {
        settle(499) // unsubscribed before completing: the client disconnected
        subscription.unsubscribe()
      }
    })
  }
}

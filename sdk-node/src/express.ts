// Express (and Connect-style) middleware.
//
//     import { AdaptiveLimiter } from '@adaptirate/sdk'
//     import { adaptiveLimiter } from '@adaptirate/sdk/express'
//
//     const limiter = new AdaptiveLimiter()
//     app.use(adaptiveLimiter({ limiter, policy: 'api' }))

import type { IncomingMessage, ServerResponse } from 'node:http'
import { decisionHeaders } from './core'
import type { Subject } from './core'
import type { AdaptiveLimiter } from './index'

type Request = IncomingMessage & { ip?: string; path?: string; user?: { id?: unknown } }
type Next = (error?: unknown) => void

export interface MiddlewareOptions {
  limiter: AdaptiveLimiter
  policy: string
  /** Who is calling. Defaults to the client address. */
  identify?: (request: Request) => Subject
  /** Path prefixes that are neither limited nor reported. */
  exclude?: string[]
  cost?: number
}

/**
 * Default identity: the client address.
 *
 * `request.ip` honours Express's "trust proxy" setting, so configure that for your
 * deployment. X-Forwarded-For is deliberately not read here: without knowing which
 * proxies are trusted, any caller could choose its own identity.
 */
export function clientAddress(request: Request): Subject {
  return { ip: request.ip ?? request.socket.remoteAddress ?? 'unknown' }
}

export function adaptiveLimiter(options: MiddlewareOptions) {
  const { limiter, policy, identify = clientAddress, exclude = [], cost = 1 } = options

  return async function adaptiRateMiddleware(request: Request, response: ServerResponse, next: Next): Promise<void> {
    const path = request.path ?? (request.url ?? '/').split('?')[0] ?? '/'
    if (exclude.some((prefix) => path.startsWith(prefix))) return next()

    let decision
    try {
      decision = await limiter.check(policy, { ...identify(request), cost })
    } catch (error) {
      // A misconfiguration (bad key, unknown policy) should surface, not silently pass traffic.
      return next(error)
    }

    for (const [name, value] of Object.entries(decisionHeaders(decision))) response.setHeader(name, value)
    if (!decision.allowed) {
      response.statusCode = 429
      response.setHeader('Content-Type', 'application/json')
      response.end(
        JSON.stringify({ detail: 'Rate limit exceeded', policy: decision.policy, reason: decision.reason, retry_after_ms: decision.retryAfterMs }),
      )
      return
    }

    limiter.started(policy)
    const start = performance.now()
    let reported = false
    const done = (status: number) => {
      if (reported) return
      reported = true
      limiter.finished(policy, performance.now() - start, { status })
    }
    response.once('finish', () => done(response.statusCode))
    // The client went away before a response was written: count it, or in-flight leaks.
    response.once('close', () => done(response.writableFinished ? response.statusCode : 499))
    next()
  }
}

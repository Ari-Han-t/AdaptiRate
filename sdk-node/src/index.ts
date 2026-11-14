import { hostname } from 'node:os'
import { randomBytes } from 'node:crypto'
import {
  API_PREFIX,
  AdaptiRateError,
  Breaker,
  DEFAULT_URL,
  LeasePool,
  LocalFallback,
  RateLimitExceededError,
  Reporter,
  bucketId,
  fromWire,
  subjectPayload,
} from './core'
import type { Decision, FailMode, Outcome, Subject, WireDecision } from './core'

export { AdaptiRateError, RateLimitExceededError, decisionHeaders } from './core'
export type { Decision, FailMode, Subject } from './core'

export interface LimiterOptions {
  /** Defaults to the ADAPTIRATE_API_KEY environment variable. */
  apiKey?: string
  /** Defaults to ADAPTIRATE_URL, then http://localhost:8080. */
  baseUrl?: string
  /** Per-request timeout in milliseconds. Default 1000. */
  timeoutMs?: number
  /** What to do when the server is unreachable. Default "open". */
  failMode?: FailMode
  /** Tokens to lease per round trip; 0 asks the server on every check. Default 0. */
  leaseSize?: number
  /** How often to send latency and error reports, in milliseconds. 0 disables the timer. Default 1000. */
  reportIntervalMs?: number
  /** After a failed call, skip the server for this long. Default 1000. */
  cooldownMs?: number
  /** Replace the HTTP implementation (for tests or custom agents). */
  fetch?: typeof fetch
}

export interface TrackOptions {
  /** Treat this thrown error as an upstream 429 rather than a plain failure. */
  isThrottle?: (error: unknown) => boolean
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * Talks to an AdaptiRate server.
 *
 *     const limiter = new AdaptiveLimiter({ apiKey: 'ar_...' })
 *     const decision = await limiter.check('api', { user: userId })
 *     if (!decision.allowed) return res.status(429).end()
 */
export class AdaptiveLimiter {
  readonly failMode: FailMode
  private readonly apiKey: string
  private readonly baseUrl: string
  private readonly timeoutMs: number
  private readonly leaseSize: number
  private readonly reportIntervalMs: number
  private readonly fetchImpl: typeof fetch
  private readonly leases = new LeasePool()
  private readonly fallback: LocalFallback
  private readonly breaker: Breaker
  private readonly reporter: Reporter
  /** One lease request per bucket at a time, so a burst does not stampede the server. */
  private readonly leaseInFlight = new Map<string, Promise<unknown>>()
  private timer: NodeJS.Timeout | null = null
  private closed = false

  constructor(options: LimiterOptions = {}) {
    const apiKey = options.apiKey ?? process.env.ADAPTIRATE_API_KEY
    if (!apiKey) throw new Error('An API key is required: pass apiKey or set ADAPTIRATE_API_KEY')
    let url = (options.baseUrl ?? process.env.ADAPTIRATE_URL ?? DEFAULT_URL).replace(/\/+$/, '')
    if (url.endsWith(API_PREFIX)) url = url.slice(0, -API_PREFIX.length)

    this.failMode = options.failMode ?? 'open'
    if (!['open', 'closed', 'local'].includes(this.failMode)) throw new Error("failMode must be 'open', 'closed' or 'local'")
    this.leaseSize = options.leaseSize ?? 0
    if (this.leaseSize < 0) throw new Error('leaseSize cannot be negative')

    this.apiKey = apiKey
    this.baseUrl = url + API_PREFIX
    this.timeoutMs = options.timeoutMs ?? 1000
    this.reportIntervalMs = options.reportIntervalMs ?? 1000
    this.fetchImpl = options.fetch ?? fetch
    this.fallback = new LocalFallback(this.failMode)
    this.breaker = new Breaker((options.cooldownMs ?? 1000) / 1000)
    this.reporter = new Reporter(`${hostname()}:${process.pid}:${randomBytes(3).toString('hex')}`)
  }

  // ---- decisions ----------------------------------------------------------

  /** The JSON body, or null when the server could not be reached or failed. */
  private async post<T>(path: string, payload: unknown): Promise<T | null> {
    if (this.breaker.isOpen) return null
    let response: Response
    try {
      response = await this.fetchImpl(this.baseUrl + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': this.apiKey, 'User-Agent': 'adaptirate-node/0.1.0' },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(this.timeoutMs),
      })
    } catch {
      this.breaker.failure()
      return null
    }
    if (response.status >= 500) {
      this.breaker.failure()
      return null
    }
    this.breaker.success()
    const body: unknown = await response.json().catch(() => null)
    if (response.status >= 400) {
      const detail = body && typeof body === 'object' && 'detail' in body ? (body as { detail: unknown }).detail : body
      throw new AdaptiRateError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
    }
    return body as T
  }

  /** Ask whether one request may proceed. Never throws for a denied request. */
  async check(policy: string, subject: Subject = {}): Promise<Decision> {
    const cost = subject.cost ?? 1
    const payload = subjectPayload(policy, subject)
    const bucket = bucketId(payload)

    if (this.leaseSize > 0) {
      for (;;) {
        const local = this.leases.trySpend(bucket, cost, policy)
        if (local) return local
        const pending = this.leaseInFlight.get(bucket)
        if (!pending) break
        // Another caller is already fetching a lease for this bucket; wait and re-check.
        await pending.catch(() => undefined)
      }
      const request = this.post<WireDecision & { granted: number; ttl_ms: number }>('/lease', {
        ...payload,
        tokens: Math.max(this.leaseSize, cost),
      })
      this.leaseInFlight.set(bucket, request)
      try {
        const body = await request
        if (!body) return this.fallback.decide(policy, bucket, cost)
        const decision = fromWire(body)
        this.fallback.learn(decision)
        return this.leases.store(bucket, Number(body.granted), cost, Number(body.ttl_ms), decision)
      } finally {
        this.leaseInFlight.delete(bucket)
      }
    }

    const body = await this.post<WireDecision>('/evaluate', { ...payload, cost })
    if (!body) return this.fallback.decide(policy, bucket, cost)
    const decision = fromWire(body)
    this.fallback.learn(decision)
    return decision
  }

  /** Decide several requests in one round trip. */
  async checkMany(checks: Array<Subject & { policy: string }>): Promise<Decision[]> {
    const subjects = checks.map((item) => ({ payload: subjectPayload(item.policy, item), cost: item.cost ?? 1, policy: item.policy }))
    const body = await this.post<{ results: WireDecision[] }>('/evaluate/batch', {
      checks: subjects.map(({ payload, cost }) => ({ ...payload, cost })),
    })
    if (!body) return subjects.map(({ payload, cost, policy }) => this.fallback.decide(policy, bucketId(payload), cost))
    const decisions = body.results.map((item) => fromWire(item))
    decisions.forEach((decision) => this.fallback.learn(decision))
    return decisions
  }

  /** Like check(), but throws RateLimitExceededError when denied. */
  async acquire(policy: string, subject: Subject = {}): Promise<Decision> {
    const decision = await this.check(policy, subject)
    if (!decision.allowed) throw new RateLimitExceededError(decision)
    return decision
  }

  /** Wait until allowed, sleeping for the server's retry-after between attempts. */
  async wait(policy: string, subject: Subject = {}, maxWaitMs = 30_000): Promise<Decision> {
    const deadline = Date.now() + maxWaitMs
    for (;;) {
      const decision = await this.check(policy, subject)
      if (decision.allowed) return decision
      if (decision.retryAfterMs < 0 || Date.now() + decision.retryAfterMs > deadline) throw new RateLimitExceededError(decision)
      await sleep(Math.max(1, decision.retryAfterMs))
    }
  }

  /** Wrap a function so every call is rate-limited and its latency and failures are reported. */
  wrap<A extends unknown[], R>(policy: string, fn: (...args: A) => Promise<R> | R, subject?: (...args: A) => Subject): (...args: A) => Promise<R> {
    return async (...args: A) => {
      await this.acquire(policy, subject ? subject(...args) : { key: fn.name || 'anonymous' })
      return this.track(policy, () => fn(...args))
    }
  }

  // ---- signals ------------------------------------------------------------

  /** Time a unit of work and report it, so the adaptive engine can see how the service is doing. */
  async track<R>(policy: string, work: () => Promise<R> | R, options: TrackOptions = {}): Promise<R> {
    this.started(policy)
    const start = performance.now()
    try {
      const result = await work()
      this.finished(policy, performance.now() - start)
      return result
    } catch (error) {
      const timeout = error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')
      const throttled = options.isThrottle?.(error) ?? false
      this.finished(policy, performance.now() - start, { error: !timeout && !throttled, timeout, throttled })
      throw error
    }
  }

  /** Mark a request as in flight. Pair with finished(). */
  started(policy: string): void {
    this.ensureTimer()
    this.reporter.started(policy)
  }

  /**
   * Complete a request marked with started(). Pass the response status this service
   * produced: only 5xx counts as an error. A 429 the service chose to send is load
   * shedding, not ill health.
   */
  finished(policy: string, latencyMs: number, outcome: Outcome & { status?: number } = {}): void {
    const { status, ...rest } = outcome
    this.reporter.finished(policy, latencyMs, { ...rest, error: rest.error || (status !== undefined && status >= 500) })
  }

  setQueueDepth(policy: string, depth: number): void {
    this.ensureTimer()
    this.reporter.setQueueDepth(policy, depth)
  }

  /** Tell AdaptiRate what an upstream provider answered, so it can pace against that provider's quota. */
  async reportProvider(provider: string, status: number, headers: Headers | Record<string, string | string[] | undefined>): Promise<void> {
    const entries: Array<[string, string]> =
      headers instanceof Headers
        ? [...headers.entries()]
        : Object.entries(headers).flatMap(([name, value]) =>
            value === undefined ? [] : [[name, Array.isArray(value) ? value.join(', ') : String(value)] as [string, string]],
          )
    const trimmed = Object.fromEntries(entries.slice(0, 100).map(([name, value]) => [name.slice(0, 128), value.slice(0, 1024)]))
    try {
      await this.post('/providers/report', { provider, status, headers: trimmed })
    } catch {
      // Reporting is best effort; never let it break the caller.
    }
  }

  /** Send pending reports now. */
  async flush(): Promise<void> {
    for (const payload of this.reporter.drain()) {
      try {
        await this.post('/report', payload)
      } catch {
        // A rejected report (for example an unknown policy) must not break the service.
      }
    }
  }

  private ensureTimer(): void {
    if (this.timer || this.closed || this.reportIntervalMs <= 0) return
    this.timer = setInterval(() => void this.flush(), this.reportIntervalMs)
    // Do not keep the process alive just to send reports.
    this.timer.unref()
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    await this.flush()
  }
}

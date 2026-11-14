// Transport-independent logic: decisions, leases, fallback, signal aggregation.

export const API_PREFIX = '/api/v1'
export const DEFAULT_URL = 'http://localhost:8080'

/** Upper bounds (ms) of the latency histogram; must match the server. */
export const LATENCY_BOUNDS_MS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000] as const

export type FailMode = 'open' | 'closed' | 'local'

/**
 * Per-bucket maps are capped so a per-IP policy on a public API cannot grow them forever.
 * Over the cap, idle entries go first, then the oldest half (Maps keep insertion order).
 */
export const MAX_BUCKETS = 10_000

export function evictToBound<V>(entries: Map<string, V>, isIdle: (key: string, value: V) => boolean): void {
  if (entries.size <= MAX_BUCKETS) return
  for (const [key, value] of entries) if (isIdle(key, value)) entries.delete(key)
  if (entries.size > MAX_BUCKETS) {
    let drop = Math.floor(entries.size / 2)
    for (const key of entries.keys()) {
      if (drop-- <= 0) break
      entries.delete(key)
    }
  }
}

export interface Subject {
  key?: string
  user?: string
  ip?: string
  apiKey?: string
  role?: string
  cost?: number
}

export interface Decision {
  allowed: boolean
  limit: number
  remaining: number
  retryAfterMs: number
  resetAfterMs: number
  reason: string
  policy: string
  /** "server", "lease" (decided locally from leased tokens) or "fallback" (server unreachable). */
  source: 'server' | 'lease' | 'fallback'
  /** Policy window reported by the server; absent from older servers. */
  windowMs?: number
}

/** Standard rate-limit response headers for a decision. */
export function decisionHeaders(decision: Decision): Record<string, string> {
  const headers: Record<string, string> = {
    'X-RateLimit-Limit': String(decision.limit),
    'X-RateLimit-Remaining': String(Math.max(0, decision.remaining)),
    'X-RateLimit-Reset': String(Math.max(0, Math.ceil(decision.resetAfterMs / 1000))),
  }
  if (!decision.allowed && decision.retryAfterMs > 0) {
    headers['Retry-After'] = String(Math.max(1, Math.ceil(decision.retryAfterMs / 1000)))
  }
  return headers
}

/** The server rejected the request itself: bad key, unknown policy, invalid input. */
export class AdaptiRateError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`AdaptiRate returned ${status}: ${detail}`)
    this.name = 'AdaptiRateError'
  }
}

export class RateLimitExceededError extends Error {
  readonly retryAfterSeconds: number
  constructor(readonly decision: Decision) {
    super(`Rate limit exceeded for policy '${decision.policy}' (${decision.reason})`)
    this.name = 'RateLimitExceededError'
    this.retryAfterSeconds = Math.max(0, decision.retryAfterMs / 1000)
  }
}

export interface WireDecision {
  allowed: boolean
  limit: number
  remaining: number
  retry_after_ms: number
  reset_after_ms: number
  reason: string
  policy: string
  window_ms?: number
}

export function fromWire(body: WireDecision, source: Decision['source'] = 'server'): Decision {
  const decision: Decision = {
    allowed: Boolean(body.allowed),
    limit: Number(body.limit),
    remaining: Number(body.remaining),
    retryAfterMs: Number(body.retry_after_ms),
    resetAfterMs: Number(body.reset_after_ms),
    reason: String(body.reason),
    policy: String(body.policy),
    source,
  }
  const windowMs = Number(body.window_ms)
  if (windowMs > 0) decision.windowMs = windowMs
  return decision
}

export function subjectPayload(policy: string, subject: Subject): Record<string, string> {
  const payload: Record<string, string> = { policy }
  if (subject.key !== undefined) payload.key = String(subject.key)
  if (subject.user !== undefined) payload.user = String(subject.user)
  if (subject.ip !== undefined) payload.ip = String(subject.ip)
  if (subject.apiKey !== undefined) payload.api_key = String(subject.apiKey)
  if (subject.role !== undefined) payload.role = String(subject.role)
  return payload
}

/** Identity of the server-side bucket a payload maps to. */
export function bucketId(payload: Record<string, string>): string {
  return ['policy', 'key', 'user', 'ip', 'api_key', 'role'].map((name) => payload[name] ?? '').join('\u0000')
}

type Clock = () => number
const monotonic: Clock = () => performance.now() / 1000

interface Lease {
  tokens: number
  expiresAt: number
  blockedUntil: number
  limit: number
  last: Decision | null
}

/**
 * Tokens leased from the server and spent locally. Expired tokens are discarded, never
 * carried over, so local decisions lag the server's limit by at most one lease.
 */
export class LeasePool {
  static readonly MAX_LOCAL_BLOCK_SECONDS = 1
  private readonly leases = new Map<string, Lease>()

  constructor(private readonly clock: Clock = monotonic) {}

  /** A local decision, or null when the server must be asked. */
  trySpend(bucket: string, cost: number, policy: string): Decision | null {
    const lease = this.leases.get(bucket)
    if (!lease) return null
    const now = this.clock()
    if (now < lease.expiresAt && lease.tokens >= cost) {
      lease.tokens -= cost
      return { allowed: true, limit: lease.limit, remaining: lease.tokens, retryAfterMs: 0, resetAfterMs: 0, reason: 'ok', policy, source: 'lease' }
    }
    if (now < lease.blockedUntil && lease.last) {
      return {
        allowed: false,
        limit: lease.last.limit,
        remaining: 0,
        retryAfterMs: Math.max(1, Math.round((lease.blockedUntil - now) * 1000)),
        resetAfterMs: lease.last.resetAfterMs,
        reason: lease.last.reason,
        policy,
        source: 'lease',
      }
    }
    return null
  }

  /** Record a lease response and decide the request that triggered it. */
  store(bucket: string, granted: number, cost: number, ttlMs: number, decision: Decision): Decision {
    const now = this.clock()
    let lease = this.leases.get(bucket)
    if (!lease) {
      lease = { tokens: 0, expiresAt: 0, blockedUntil: 0, limit: 0, last: null }
      this.leases.set(bucket, lease)
    }
    const result = this.apply(lease, granted, cost, ttlMs, decision, now)
    // Dropping a lease only costs an extra server round trip. Evicting after the update keeps
    // the lease just stored out of the idle set.
    evictToBound(this.leases, (_, entry) => now >= entry.expiresAt && now >= entry.blockedUntil)
    return result
  }

  private apply(lease: Lease, granted: number, cost: number, ttlMs: number, decision: Decision, now: number): Decision {
    lease.limit = decision.limit
    lease.last = decision
    lease.expiresAt = now + ttlMs / 1000
    if (granted >= cost) {
      lease.tokens = granted - cost
      lease.blockedUntil = 0
      return { ...decision, allowed: true, remaining: lease.tokens, retryAfterMs: 0, reason: 'ok', source: 'server' }
    }
    // Not enough for this request. Keep what was granted for cheaper requests, and stop
    // asking the server until it could plausibly say yes.
    lease.tokens = granted
    const wait = decision.retryAfterMs > 0 ? decision.retryAfterMs / 1000 : LeasePool.MAX_LOCAL_BLOCK_SECONDS
    lease.blockedUntil = now + Math.min(wait, LeasePool.MAX_LOCAL_BLOCK_SECONDS)
    return {
      ...decision,
      allowed: false,
      remaining: granted,
      retryAfterMs: Math.max(1, decision.retryAfterMs),
      reason: decision.reason === 'ok' ? 'limit' : decision.reason,
      source: 'server',
    }
  }
}

/**
 * What to do when AdaptiRate cannot be reached. "open" allows, "closed" denies, and
 * "local" enforces the last limit the server reported with an in-process token bucket.
 */
export class LocalFallback {
  private readonly known = new Map<string, { limit: number; window: number }>()
  private readonly buckets = new Map<string, { tokens: number; stamp: number }>()

  constructor(
    private readonly mode: FailMode,
    private readonly clock: Clock = monotonic,
  ) {}

  learn(decision: Decision): void {
    if (decision.source !== 'server' || decision.limit <= 0) return
    if (decision.windowMs !== undefined && decision.windowMs > 0) {
      // The server states the window, so no guessing.
      this.known.set(decision.policy, { limit: decision.limit, window: decision.windowMs / 1000 })
      return
    }
    const window = decision.resetAfterMs > 0 ? decision.resetAfterMs / 1000 : 0
    const previous = this.known.get(decision.policy)
    // Older servers: reset_after shrinks as a window elapses; the largest seen approximates the window.
    this.known.set(decision.policy, { limit: decision.limit, window: Math.max(window, previous?.window ?? 0) || 1 })
  }

  decide(policy: string, bucket: string, cost: number): Decision {
    const base = { limit: 0, remaining: 0, resetAfterMs: 0, policy, source: 'fallback' as const }
    if (this.mode === 'closed') return { ...base, allowed: false, retryAfterMs: 1000, resetAfterMs: 1000, reason: 'fail_closed' }
    const known = this.known.get(policy)
    if (this.mode === 'open' || !known) return { ...base, allowed: true, retryAfterMs: 0, reason: 'fail_open' }

    const rate = known.limit / known.window
    const now = this.clock()
    const state = this.buckets.get(bucket) ?? { tokens: known.limit, stamp: now }
    const tokens = Math.min(known.limit, state.tokens + (now - state.stamp) * rate)
    if (tokens >= cost) {
      this.buckets.set(bucket, { tokens: tokens - cost, stamp: now })
      this.evict(now)
      return { ...base, allowed: true, limit: known.limit, remaining: Math.floor(tokens - cost), retryAfterMs: 0, reason: 'fail_local' }
    }
    this.buckets.set(bucket, { tokens, stamp: now })
    this.evict(now)
    const waitMs = Math.floor(((cost - tokens) / rate) * 1000) + 1
    return { ...base, allowed: false, limit: known.limit, retryAfterMs: waitMs, resetAfterMs: waitMs, reason: 'fail_local' }
  }

  /** A dropped bucket restarts full, which errs toward availability: an outage never denies more than it would have. */
  private evict(now: number): void {
    evictToBound(this.buckets, (bucket, state) => {
      const known = this.known.get(bucket.split('\u0000', 1)[0] ?? '')
      return !known || state.tokens + ((now - state.stamp) * known.limit) / known.window >= known.limit
    })
  }
}

/** After a failure, short-circuit to the fallback briefly rather than making every request wait out a timeout. */
export class Breaker {
  private openUntil = 0
  constructor(
    private readonly cooldownSeconds: number,
    private readonly clock: Clock = monotonic,
  ) {}
  get isOpen(): boolean {
    return this.clock() < this.openUntil
  }
  failure(): void {
    this.openUntil = this.clock() + this.cooldownSeconds
  }
  success(): void {
    this.openUntil = 0
  }
}

interface Window {
  requests: number
  errors: number
  throttled: number
  timeouts: number
  latencySumMs: number
  buckets: number[]
}

const emptyWindow = (): Window => ({
  requests: 0,
  errors: 0,
  throttled: 0,
  timeouts: 0,
  latencySumMs: 0,
  buckets: new Array<number>(LATENCY_BOUNDS_MS.length + 1).fill(0),
})

export interface Outcome {
  error?: boolean
  throttled?: boolean
  timeout?: boolean
}

export interface ReportPayload {
  policy: string
  instance_id: string
  requests: number
  errors: number
  throttled: number
  timeouts: number
  latency_sum_ms: number
  latency_buckets: number[]
  in_flight: number
  queue_depth?: number
}

/** Aggregates what the protected service observes; drained once per report interval. */
export class Reporter {
  private windows = new Map<string, Window>()
  private readonly inFlight = new Map<string, number>()
  private readonly queueDepth = new Map<string, number>()
  private readonly seen = new Set<string>()

  constructor(private readonly instanceId: string) {}

  started(policy: string): void {
    this.inFlight.set(policy, (this.inFlight.get(policy) ?? 0) + 1)
    this.seen.add(policy)
  }

  finished(policy: string, latencyMs: number, outcome: Outcome = {}): void {
    this.inFlight.set(policy, Math.max(0, (this.inFlight.get(policy) ?? 0) - 1))
    let window = this.windows.get(policy)
    if (!window) {
      window = emptyWindow()
      this.windows.set(policy, window)
    }
    window.requests += 1
    if (outcome.error) window.errors += 1
    if (outcome.throttled) window.throttled += 1
    if (outcome.timeout) window.timeouts += 1
    window.latencySumMs += latencyMs
    let index = LATENCY_BOUNDS_MS.findIndex((bound) => latencyMs <= bound)
    if (index === -1) index = LATENCY_BOUNDS_MS.length
    window.buckets[index] = (window.buckets[index] ?? 0) + 1
  }

  setQueueDepth(policy: string, depth: number): void {
    this.queueDepth.set(policy, Math.max(0, depth))
    this.seen.add(policy)
  }

  /**
   * Reports to send. Policies with in-flight work report even when nothing finished, so
   * a stalled service still shows up as a growing in-flight count.
   */
  drain(): ReportPayload[] {
    const windows = this.windows
    this.windows = new Map()
    const policies = new Set(windows.keys())
    for (const policy of this.seen) {
      if (this.inFlight.get(policy) || this.queueDepth.has(policy)) policies.add(policy)
    }
    return [...policies].sort().map((policy) => {
      const window = windows.get(policy) ?? emptyWindow()
      const payload: ReportPayload = {
        policy,
        instance_id: this.instanceId,
        requests: window.requests,
        errors: window.errors,
        throttled: window.throttled,
        timeouts: window.timeouts,
        latency_sum_ms: Math.round(window.latencySumMs * 1000) / 1000,
        latency_buckets: window.buckets,
        in_flight: this.inFlight.get(policy) ?? 0,
      }
      const depth = this.queueDepth.get(policy)
      if (depth !== undefined) payload.queue_depth = depth
      return payload
    })
  }
}

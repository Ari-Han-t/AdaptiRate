// SDK behaviour against a fake server. No network.
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { test } from 'node:test'
import { LATENCY_BOUNDS_MS, LeasePool, LocalFallback, MAX_BUCKETS, Reporter, fromWire } from '../src/core'
import { adaptiveLimiter } from '../src/express'
import { AdaptiRateError, AdaptiveLimiter, RateLimitExceededError, decisionHeaders } from '../src/index'
import type { LimiterOptions } from '../src/index'

type Wire = Record<string, unknown>

function wire(allowed = true, overrides: Wire = {}): Wire {
  return {
    allowed,
    limit: 10,
    remaining: allowed ? 9 : 0,
    retry_after_ms: allowed ? 0 : 500,
    reset_after_ms: 60000,
    reason: allowed ? 'ok' : 'limit',
    policy: 'api',
    ...overrides,
  }
}

class FakeServer {
  requests: Array<{ path: string; body: any; headers: Record<string, string> }> = []
  responses = new Map<string, Wire | ((body: any) => Wire) | { status: number; body: Wire }>()
  fail = false

  fetch: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname
    const body = JSON.parse(String(init?.body))
    this.requests.push({ path, body, headers: init?.headers as Record<string, string> })
    if (this.fail) throw new TypeError('fetch failed')
    let answer = this.responses.get(path) ?? wire()
    if (typeof answer === 'function') answer = answer(body)
    if ('status' in answer && typeof answer.status === 'number' && 'body' in answer) {
      return new Response(JSON.stringify(answer.body), { status: answer.status })
    }
    return new Response(JSON.stringify(answer), { status: 200 })
  }

  paths(): string[] {
    return this.requests.map((request) => request.path)
  }
}

function limiterFor(server: FakeServer, options: LimiterOptions = {}): AdaptiveLimiter {
  return new AdaptiveLimiter({ apiKey: 'ar_test', baseUrl: 'http://ar.test', reportIntervalMs: 0, fetch: server.fetch, ...options })
}

test('requires an API key and normalises the base URL', () => {
  const saved = process.env.ADAPTIRATE_API_KEY
  delete process.env.ADAPTIRATE_API_KEY
  assert.throws(() => new AdaptiveLimiter(), /API key/)
  if (saved) process.env.ADAPTIRATE_API_KEY = saved
  assert.throws(() => new AdaptiveLimiter({ apiKey: 'k', failMode: 'maybe' as never }), /failMode/)
})

test('check sends the subject and key to the evaluate endpoint', async () => {
  const server = new FakeServer()
  const limiter = limiterFor(server, { baseUrl: 'http://ar.test/api/v1/' })
  const decision = await limiter.check('api', { user: 'u1', role: 'premium', apiKey: 'k1', cost: 3 })
  const [request] = server.requests
  assert.equal(request?.path, '/api/v1/evaluate')
  assert.deepEqual(request?.body, { policy: 'api', user: 'u1', api_key: 'k1', role: 'premium', cost: 3 })
  assert.equal(request?.headers['X-API-Key'], 'ar_test')
  assert.equal(decision.allowed, true)
  assert.equal(decision.remaining, 9)
  assert.equal(decision.source, 'server')
})

test('denied decisions expose retry headers and acquire throws', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/evaluate', wire(false, { retry_after_ms: 1500, reset_after_ms: 2500 }))
  const limiter = limiterFor(server)
  const decision = await limiter.check('api', { user: 'u1' })
  assert.equal(decision.allowed, false)
  assert.deepEqual(decisionHeaders(decision), {
    'X-RateLimit-Limit': '10',
    'X-RateLimit-Remaining': '0',
    'X-RateLimit-Reset': '3',
    'Retry-After': '2',
  })
  await assert.rejects(limiter.acquire('api', { user: 'u1' }), (error: unknown) => {
    assert.ok(error instanceof RateLimitExceededError)
    assert.equal(error.retryAfterSeconds, 1.5)
    return true
  })
})

test('configuration errors are thrown, not swallowed', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/evaluate', { status: 404, body: { detail: "Policy 'nope' does not exist" } })
  await assert.rejects(limiterFor(server).check('nope', { user: 'u' }), (error: unknown) => {
    assert.ok(error instanceof AdaptiRateError)
    assert.equal(error.status, 404)
    assert.match(error.detail, /does not exist/)
    return true
  })
})

test('fail modes when the server is unreachable or erroring', async () => {
  const server = new FakeServer()
  server.fail = true
  const open = await limiterFor(server, { failMode: 'open' }).check('api', { user: 'u' })
  assert.deepEqual([open.allowed, open.reason, open.source], [true, 'fail_open', 'fallback'])
  const closed = await limiterFor(server, { failMode: 'closed' }).check('api', { user: 'u' })
  assert.deepEqual([closed.allowed, closed.reason], [false, 'fail_closed'])

  const erroring = new FakeServer()
  erroring.responses.set('/api/v1/evaluate', { status: 503, body: { detail: 'down' } })
  assert.equal((await limiterFor(erroring, { failMode: 'closed' }).check('api', { user: 'u' })).allowed, false)
})

test('the breaker stops hammering a dead server', async () => {
  const server = new FakeServer()
  server.fail = true
  const limiter = limiterFor(server, { cooldownMs: 60_000 })
  for (let index = 0; index < 20; index++) await limiter.check('api', { user: 'u' })
  assert.equal(server.requests.length, 1)
})

test('local fail mode enforces the last known limit per subject', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/evaluate', wire(true, { limit: 5 }))
  const limiter = limiterFor(server, { failMode: 'local', cooldownMs: 60_000 })
  assert.equal((await limiter.check('api', { user: 'u1' })).allowed, true)
  server.fail = true
  const results = []
  for (let index = 0; index < 8; index++) results.push(await limiter.check('api', { user: 'u1' }))
  assert.deepEqual(
    results.map((result) => result.allowed),
    [true, true, true, true, true, false, false, false],
  )
  assert.equal(results.at(-1)?.reason, 'fail_local')
  assert.equal((await limiter.check('api', { user: 'u2' })).allowed, true)
  assert.equal((await limiter.check('other', { user: 'u1' })).reason, 'fail_open')
})

test('leasing spends locally and refills when empty', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/lease', (body) => ({ ...wire(), granted: body.tokens, ttl_ms: 2000 }))
  const limiter = limiterFor(server, { leaseSize: 50 })
  const results = []
  for (let index = 0; index < 120; index++) results.push(await limiter.check('api', { user: 'u1' }))
  assert.ok(results.every((result) => result.allowed))
  assert.deepEqual(server.paths(), ['/api/v1/lease', '/api/v1/lease', '/api/v1/lease'])
  assert.deepEqual(server.requests[0]?.body, { policy: 'api', user: 'u1', tokens: 50 })
})

test('a concurrent burst shares one lease request', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/lease', (body) => ({ ...wire(), granted: body.tokens, ttl_ms: 2000 }))
  const limiter = limiterFor(server, { leaseSize: 100 })
  const results = await Promise.all(Array.from({ length: 100 }, () => limiter.check('api', { user: 'u1' })))
  assert.ok(results.every((result) => result.allowed))
  assert.equal(server.requests.length, 1)
})

test('partial and empty leases', async () => {
  const server = new FakeServer()
  const grants = [4, 0]
  server.responses.set('/api/v1/lease', () => ({ ...wire(false, { retry_after_ms: 800 }), granted: grants.shift() ?? 0, ttl_ms: 2000 }))
  const limiter = limiterFor(server, { leaseSize: 10 })
  const results = []
  for (let index = 0; index < 8; index++) results.push(await limiter.check('api', { user: 'u1' }))
  assert.deepEqual(
    results.map((result) => result.allowed),
    [true, true, true, true, false, false, false, false],
  )
  assert.equal(server.requests.length, 2, 'after an empty grant the SDK denies locally instead of asking each time')
})

test('expired lease tokens are discarded', () => {
  let now = 0
  const pool = new LeasePool(() => now)
  const decision = fromWire(wire() as never)
  assert.equal(pool.store('b', 10, 1, 1000, decision).allowed, true)
  assert.notEqual(pool.trySpend('b', 1, 'api'), null)
  now = 1.5
  assert.equal(pool.trySpend('b', 1, 'api'), null)
})

test('local fallback refills over time', () => {
  let now = 0
  const fallback = new LocalFallback('local', () => now)
  fallback.learn(fromWire(wire(true, { limit: 2, reset_after_ms: 1000 }) as never))
  const allowed = () => fallback.decide('api', 'b', 1).allowed
  assert.deepEqual([allowed(), allowed(), allowed()], [true, true, false])
  now = 0.5
  assert.deepEqual([allowed(), allowed()], [true, false])
})

test('checkMany uses the batch endpoint', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/evaluate/batch', { results: [wire(), wire(false)] })
  const results = await limiterFor(server).checkMany([
    { policy: 'api', user: 'a' },
    { policy: 'api', user: 'b', cost: 2 },
  ])
  assert.deepEqual(
    results.map((result) => result.allowed),
    [true, false],
  )
  assert.deepEqual(server.requests[0]?.body, {
    checks: [
      { policy: 'api', user: 'a', cost: 1 },
      { policy: 'api', user: 'b', cost: 2 },
    ],
  })
})

test('wait retries after the advertised delay and gives up past maxWait', async () => {
  const server = new FakeServer()
  const answers = [wire(false, { retry_after_ms: 20 }), wire(false, { retry_after_ms: 10 }), wire()]
  server.responses.set('/api/v1/evaluate', () => answers.shift() ?? wire())
  const started = Date.now()
  assert.equal((await limiterFor(server).wait('api', { user: 'u' })).allowed, true)
  assert.ok(Date.now() - started >= 25)

  const blocked = new FakeServer()
  blocked.responses.set('/api/v1/evaluate', wire(false, { retry_after_ms: 60_000 }))
  await assert.rejects(limiterFor(blocked).wait('api', { user: 'u' }, 100), RateLimitExceededError)
})

test('wrap limits a function and reports its outcomes', async () => {
  const server = new FakeServer()
  const limiter = limiterFor(server)
  const send = limiter.wrap(
    'emails',
    async (userId: string) => {
      if (userId === 'boom') throw new Error('smtp down')
      return `sent to ${userId}`
    },
    (userId) => ({ user: userId }),
  )
  assert.equal(await send('u1'), 'sent to u1')
  await assert.rejects(send('boom'), /smtp down/)
  await limiter.flush()
  const report = server.requests.find((request) => request.path === '/api/v1/report')?.body
  assert.deepEqual([report.policy, report.requests, report.errors, report.in_flight], ['emails', 2, 1, 0])
  assert.equal(report.latency_buckets.length, LATENCY_BOUNDS_MS.length + 1)
})

test('only 5xx counts as an error; upstream throttles must be flagged', async () => {
  const server = new FakeServer()
  const limiter = limiterFor(server)
  for (const status of [200, 503, 429, 404]) {
    limiter.started('api')
    limiter.finished('api', 10, { status })
  }
  limiter.started('api')
  limiter.finished('api', 10, { status: 502, throttled: true })
  await limiter.flush()
  const report = server.requests.find((request) => request.path === '/api/v1/report')?.body
  assert.deepEqual([report.requests, report.errors, report.throttled], [5, 2, 1])
})

test('the reporter keeps reporting stalled in-flight work', () => {
  const reporter = new Reporter('instance-1')
  reporter.started('api')
  reporter.started('api')
  reporter.finished('api', 3)
  const [first] = reporter.drain()
  assert.equal(first?.in_flight, 1)
  assert.equal(first?.latency_buckets[2], 1)
  const [stalled] = reporter.drain()
  assert.deepEqual([stalled?.requests, stalled?.in_flight], [0, 1])
  reporter.finished('api', 99_999, { error: true })
  assert.equal(reporter.drain()[0]?.latency_buckets.at(-1), 1)
  assert.deepEqual(reporter.drain(), [])
})

test('provider responses are reported with their headers', async () => {
  const server = new FakeServer()
  const limiter = limiterFor(server)
  await limiter.reportProvider('openai', 429, { 'Retry-After': '7', 'x-multi': ['a', 'b'] })
  await limiter.reportProvider('anthropic', 200, new Headers({ 'anthropic-ratelimit-requests-remaining': '40' }))
  assert.deepEqual(server.requests[0]?.body, { provider: 'openai', status: 429, headers: { 'Retry-After': '7', 'x-multi': 'a, b' } })
  assert.equal(server.requests[1]?.body.headers['anthropic-ratelimit-requests-remaining'], '40')
})

// ---- Express middleware ---------------------------------------------------

class FakeResponse extends EventEmitter {
  statusCode = 200
  headers: Record<string, string> = {}
  body = ''
  writableFinished = false
  setHeader(name: string, value: string) {
    this.headers[name] = value
  }
  end(body = '') {
    this.body = body
    this.writableFinished = true
    this.emit('finish')
    this.emit('close')
  }
}

test('express middleware allows, blocks, excludes and reports', async () => {
  const server = new FakeServer()
  const limiter = limiterFor(server)
  const middleware = adaptiveLimiter({ limiter, policy: 'api', exclude: ['/health'] })
  const request = (path: string) => ({ path, ip: '203.0.113.9', socket: {} }) as never

  const allowed = new FakeResponse()
  let nextCalled = 0
  await middleware(request('/'), allowed as never, () => {
    nextCalled += 1
    allowed.statusCode = 500
    allowed.end('broken')
  })
  assert.equal(nextCalled, 1)
  assert.equal(allowed.headers['X-RateLimit-Limit'], '10')
  assert.deepEqual(server.requests[0]?.body, { policy: 'api', ip: '203.0.113.9', cost: 1 })

  await middleware(request('/health'), new FakeResponse() as never, () => {
    nextCalled += 1
  })
  assert.equal(server.paths().filter((path) => path.endsWith('/evaluate')).length, 1, '/health is excluded')

  server.responses.set('/api/v1/evaluate', wire(false, { retry_after_ms: 2000 }))
  const blocked = new FakeResponse()
  await middleware(request('/'), blocked as never, () => assert.fail('a blocked request must not reach the handler'))
  assert.equal(blocked.statusCode, 429)
  assert.equal(blocked.headers['Retry-After'], '2')
  assert.equal(JSON.parse(blocked.body).reason, 'limit')

  await limiter.flush()
  const report = server.requests.find((entry) => entry.path === '/api/v1/report')?.body
  assert.deepEqual([report.requests, report.errors, report.in_flight], [1, 1, 0])
})

test('express middleware surfaces configuration errors', async () => {
  const server = new FakeServer()
  server.responses.set('/api/v1/evaluate', { status: 401, body: { detail: 'Missing or invalid credentials' } })
  const middleware = adaptiveLimiter({ limiter: limiterFor(server), policy: 'api' })
  let received: unknown
  await middleware({ path: '/', ip: '1.2.3.4', socket: {} } as never, new FakeResponse() as never, (error) => {
    received = error
  })
  assert.ok(received instanceof AdaptiRateError)
})

test('local fallback uses the window the server reports', () => {
  let now = 0
  const fallback = new LocalFallback('local', () => now)
  // After one allowed request reset_after is window/limit (600 ms), which the old heuristic
  // mistook for the window and refilled 100x too fast.
  fallback.learn(fromWire(wire(true, { limit: 100, reset_after_ms: 600, window_ms: 60000 }) as never))
  const results = Array.from({ length: 101 }, () => fallback.decide('api', 'b', 1))
  assert.deepEqual(results.map((result) => result.allowed), [...Array<boolean>(100).fill(true), false])
  const retry = results.at(-1)?.retryAfterMs ?? 0
  assert.ok(retry >= 550 && retry <= 650, `100 per 60 s is one token per 600 ms, got ${retry}`)
  assert.equal(fromWire(wire() as never).windowMs, undefined)
})

test('lease pool and fallback buckets are bounded and still serve new buckets', () => {
  let now = 0
  const pool = new LeasePool(() => now)
  const decision = fromWire(wire() as never)
  for (let index = 0; index < MAX_BUCKETS + 50; index++) pool.store(`u${index}`, 5, 1, 60_000, decision)
  assert.ok((pool as unknown as { leases: Map<string, unknown> }).leases.size <= MAX_BUCKETS)
  assert.equal(pool.store('fresh', 5, 1, 1000, decision).allowed, true)
  assert.notEqual(pool.trySpend('fresh', 1, 'api'), null)

  const fallback = new LocalFallback('local', () => now)
  fallback.learn(fromWire(wire(true, { limit: 5, window_ms: 60000 }) as never))
  for (let index = 0; index < MAX_BUCKETS + 50; index++) fallback.decide('api', `api\u0000k${index}`, 1)
  assert.ok((fallback as unknown as { buckets: Map<string, unknown> }).buckets.size <= MAX_BUCKETS)
  assert.equal(fallback.decide('api', 'api\u0000fresh', 1).allowed, true)
})

function htmlFetch(status: number): typeof fetch {
  return async () => new Response('<html>Bad Gateway</html>', { status, headers: { 'content-type': 'text/html' } })
}

test('a non-JSON error body becomes an AdaptiRateError', async () => {
  for (const status of [400, 404, 429]) {
    await assert.rejects(limiterFor(new FakeServer(), { fetch: htmlFetch(status) }).check('api', { user: 'u' }), (error: unknown) => {
      assert.ok(error instanceof AdaptiRateError)
      assert.deepEqual([error.status, error.detail], [status, 'unreadable response body'])
      return true
    })
  }
})

test('a non-JSON 2xx or 5xx body goes through the fail mode', async () => {
  for (const status of [200, 502]) {
    for (const leaseSize of [0, 10]) {
      const limiter = limiterFor(new FakeServer(), { fetch: htmlFetch(status), failMode: 'closed', cooldownMs: 60_000, leaseSize })
      const decision = await limiter.check('api', { user: 'u' })
      assert.deepEqual([decision.allowed, decision.source], [false, 'fallback'])
    }
  }
})

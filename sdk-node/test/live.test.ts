// SDK against a running AdaptiRate server. Skipped unless ADAPTIRATE_TEST_URL,
// ADAPTIRATE_TEST_ADMIN_EMAIL and ADAPTIRATE_TEST_ADMIN_PASSWORD are set.
import 'reflect-metadata'
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { after, before, test } from 'node:test'
import { Reflector } from '@nestjs/core'
import { firstValueFrom, of } from 'rxjs'
import { AdaptiRateError, AdaptiveLimiter } from '../src/index'
import { AdaptiveRateLimit, AdaptiveRateLimitGuard, AdaptiveRateLimitInterceptor } from '../src/nest'

const URL_ = process.env.ADAPTIRATE_TEST_URL
const EMAIL = process.env.ADAPTIRATE_TEST_ADMIN_EMAIL
const PASSWORD = process.env.ADAPTIRATE_TEST_ADMIN_PASSWORD
const skip = !(URL_ && EMAIL && PASSWORD)

let token = ''
let secret = ''
let keyId = ''
const created: string[] = []

async function admin(path: string, method = 'GET', body?: unknown): Promise<any> {
  const response = await fetch(`${URL_}/api/v1${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok && response.status !== 204) throw new Error(`${method} ${path} -> ${response.status} ${await response.text()}`)
  return response.status === 204 ? null : response.json()
}

async function policy(overrides: Record<string, unknown> = {}): Promise<string> {
  const name = `node-${randomBytes(5).toString('hex')}`
  const row = await admin('/policies', 'POST', {
    name,
    limit: 5,
    window_seconds: 60,
    scope: 'user',
    algorithm: 'fixed_window',
    adaptive: false,
    ...overrides,
  })
  created.push(row.id)
  return name
}

const limiter = (options = {}) => new AdaptiveLimiter({ apiKey: secret, baseUrl: URL_, reportIntervalMs: 0, ...options })

before(async () => {
  if (skip) return
  token = (await admin('/auth/login', 'POST', { email: EMAIL, password: PASSWORD })).access_token
  const key = await admin('/keys', 'POST', { name: `node-sdk-tests-${randomBytes(3).toString('hex')}`, scopes: ['evaluate', 'report'] })
  secret = key.secret
  keyId = key.id
})

after(async () => {
  if (skip) return
  for (const id of created) await admin(`/policies/${id}`, 'DELETE')
  await admin(`/keys/${keyId}/revoke`, 'POST')
})

test('enforces a real limit per subject', { skip }, async () => {
  const name = await policy({ limit: 3 })
  const sdk = limiter()
  const results = []
  for (let index = 0; index < 5; index++) results.push(await sdk.check(name, { user: 'alice' }))
  assert.deepEqual(
    results.map((result) => result.allowed),
    [true, true, true, false, false],
  )
  assert.equal(results[3]?.reason, 'limit')
  assert.ok((results[3]?.retryAfterMs ?? 0) > 0)
  assert.equal((await sdk.check(name, { user: 'bob' })).allowed, true)
  await assert.rejects(sdk.check('no-such-policy', { user: 'alice' }), (error: unknown) => error instanceof AdaptiRateError && error.status === 404)
  await assert.rejects(sdk.check(name, { ip: '10.0.0.1' }), (error: unknown) => error instanceof AdaptiRateError && error.status === 422)
})

test('a bad API key is a loud error, not fail-open', { skip }, async () => {
  const name = await policy()
  await assert.rejects(limiter({ apiKey: 'ar_wrong-key' }).check(name, { user: 'a' }), (error: unknown) => error instanceof AdaptiRateError && error.status === 401)
})

test('concurrent checks never over-admit, across two SDK instances', { skip }, async () => {
  const name = await policy({ limit: 20, algorithm: 'token_bucket' })
  const first = limiter()
  const second = limiter()
  const results = await Promise.all([
    ...Array.from({ length: 30 }, () => first.check(name, { user: 'shared' })),
    ...Array.from({ length: 30 }, () => second.check(name, { user: 'shared' })),
  ])
  assert.equal(results.filter((result) => result.allowed).length, 20)
})

test('leasing never admits more than the limit', { skip }, async () => {
  const name = await policy({ limit: 100, algorithm: 'token_bucket' })
  const sdk = limiter({ leaseSize: 30 })
  const results = []
  for (let index = 0; index < 150; index++) results.push(await sdk.check(name, { user: 'worker' }))
  assert.equal(results.filter((result) => result.allowed).length, 100)
  assert.ok(results.filter((result) => result.source === 'lease').length > 90)
})

test('reports and provider feedback reach the server', { skip }, async () => {
  const provider = `node-${randomBytes(4).toString('hex')}`
  const name = await policy({ limit: 50, provider })
  const sdk = limiter()
  await sdk.track(name, async () => undefined)
  await sdk.flush()
  assert.equal((await sdk.check(name, { user: 'a' })).allowed, true)
  await sdk.reportProvider(provider, 429, { 'Retry-After': '30' })
  const paused = await sdk.check(name, { user: 'a' })
  assert.deepEqual([paused.allowed, paused.reason], [false, 'paused'])
  assert.ok(paused.retryAfterMs > 25_000 && paused.retryAfterMs <= 30_000)
  await admin(`/providers/${provider}`, 'DELETE')
})

test('NestJS guard and interceptor limit a decorated handler', { skip }, async () => {
  const name = await policy({ limit: 2, scope: 'ip' })
  const sdk = limiter()

  class OrdersController {
    @AdaptiveRateLimit(name)
    create() {
      return 'created'
    }
    open() {
      return 'open'
    }
  }
  const reflector = new Reflector()
  const guard = new AdaptiveRateLimitGuard(reflector, sdk)
  const interceptor = new AdaptiveRateLimitInterceptor(reflector, sdk)
  const headers: Record<string, string> = {}
  const context = (handler: () => string) =>
    ({
      getHandler: () => handler,
      getClass: () => OrdersController,
      getType: () => 'http',
      switchToHttp: () => ({
        getRequest: () => ({ ip: '198.51.100.7' }),
        getResponse: () => ({ statusCode: 201, setHeader: (key: string, value: string) => (headers[key] = value) }),
      }),
    }) as never

  const limited = context(OrdersController.prototype.create)
  assert.equal(await guard.canActivate(limited), true)
  assert.equal(headers['X-RateLimit-Limit'], '2')
  assert.equal(await firstValueFrom(interceptor.intercept(limited, { handle: () => of('created') })), 'created')
  assert.equal(await guard.canActivate(limited), true)
  await assert.rejects(guard.canActivate(limited), (error: any) => error.getStatus() === 429 && error.getResponse().reason === 'limit')
  // Handlers without the decorator are untouched.
  assert.equal(await guard.canActivate(context(OrdersController.prototype.open)), true)
  await sdk.close()
})

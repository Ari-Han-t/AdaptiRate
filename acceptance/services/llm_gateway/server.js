'use strict'
// LLM gateway protected by AdaptiRate through the Node SDK.
//
// POST /complete asks AdaptiRate whether a call may go out, calls the provider, then
// reports the provider's answer (status and rate-limit headers) back so AdaptiRate can
// pace every gateway instance against the provider's real quota.
// USE_ADAPTIRATE=false (or POST /_mode) skips the limiter entirely: the control run.

const express = require('express')
const { AdaptiveLimiter } = require('@adaptirate/sdk')

const required = (name) => {
  const value = process.env[name]
  if (!value) throw new Error(`${name} must be set`)
  return value
}

const policy = required('LLM_POLICY')
const providerName = required('PROVIDER_NAME')
const providerUrl = required('PROVIDER_URL').replace(/\/+$/, '')
let useAdaptirate = (process.env.USE_ADAPTIRATE ?? 'true').toLowerCase() !== 'false'

const limiter = new AdaptiveLimiter({ failMode: 'open', timeoutMs: 2000 })
const stats = { upstream_calls: 0, upstream_429: 0, limited_locally: 0 }

const app = express()
app.use(express.json())

app.get('/health', (_req, res) => res.json({ status: 'ok' }))

app.get('/stats', (_req, res) => res.json({ ...stats, use_adaptirate: useAdaptirate }))

app.post('/_stats/reset', (_req, res) => {
  stats.upstream_calls = 0
  stats.upstream_429 = 0
  stats.limited_locally = 0
  res.json({ ...stats })
})

app.post('/_mode', (req, res) => {
  if (!req.body || typeof req.body.use_adaptirate !== 'boolean') {
    return res.status(422).json({ error: 'use_adaptirate (boolean) is required' })
  }
  useAdaptirate = req.body.use_adaptirate
  return res.json({ use_adaptirate: useAdaptirate })
})

app.post('/complete', async (_req, res) => {
  if (useAdaptirate) {
    let decision
    try {
      decision = await limiter.check(policy, { key: 'gateway' })
    } catch (error) {
      return res.status(500).json({ error: 'limiter_error', detail: String(error) })
    }
    if (!decision.allowed) {
      stats.limited_locally += 1
      res.set('Retry-After', String(Math.max(1, Math.ceil(decision.retryAfterMs / 1000))))
      return res.status(429).json({ error: 'rate_limited', source: 'gateway', reason: decision.reason, retry_after_ms: decision.retryAfterMs })
    }
  }

  let upstream
  try {
    upstream = await fetch(`${providerUrl}/v1/complete`, { method: 'POST', signal: AbortSignal.timeout(10_000) })
  } catch (error) {
    return res.status(502).json({ error: 'provider_unreachable', detail: String(error) })
  }
  stats.upstream_calls += 1
  if (upstream.status === 429) stats.upstream_429 += 1
  if (useAdaptirate) await limiter.reportProvider(providerName, upstream.status, upstream.headers)
  const body = await upstream.json().catch(() => ({}))
  return res.status(upstream.status).json({ ...body, source: 'provider' })
})

const port = Number(process.env.PORT ?? 3000)
const server = app.listen(port, '0.0.0.0', () => console.log(`llm-gateway listening on ${port}`))
process.on('SIGTERM', () => {
  server.close(() => void limiter.close().finally(() => process.exit(0)))
})

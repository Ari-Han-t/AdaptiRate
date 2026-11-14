// An existing Express service with AdaptiRate added.
//
//   cd ../../sdk-node && npm install && npm run build
//   cd ../examples/express && npm install
//   ADAPTIRATE_API_KEY=ar_... ADAPTIRATE_URL=http://localhost:8080 node server.js
//
// Needs a policy named "express-api" scoped "Per user" (create it in the dashboard).

const express = require('express')
const { AdaptiveLimiter } = require('@adaptirate/sdk')
const { adaptiveLimiter } = require('@adaptirate/sdk/express')

const app = express()
// Behind a proxy, tell Express which hops to trust so req.ip is the real client.
app.set('trust proxy', process.env.TRUST_PROXY ?? false)

const limiter = new AdaptiveLimiter({ failMode: 'local' })

// Your existing authentication runs first and sets req.user.
app.use((req, _res, next) => {
  const userId = req.get('x-user')
  if (userId) req.user = { id: userId }
  next()
})

// The integration: every request except /health is limited per user (or per IP when anonymous).
app.use(
  adaptiveLimiter({
    limiter,
    policy: 'express-api',
    exclude: ['/health'],
    identify: (req) => (req.user ? { user: req.user.id, ip: req.ip } : { user: `ip:${req.ip}`, ip: req.ip }),
  }),
)

app.get('/health', (_req, res) => res.json({ status: 'ok' }))
app.get('/products', (_req, res) => res.json([{ id: 1, name: 'Keyboard' }]))

const server = app.listen(Number(process.env.PORT ?? 3000), () => console.log(`listening on ${server.address().port}`))

process.on('SIGTERM', async () => {
  await limiter.close() // send pending latency reports
  server.close()
})

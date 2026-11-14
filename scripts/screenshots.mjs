// Retakes the README screenshots by driving a headless Chromium over its debugging
// protocol. No dependencies; needs Node 22+ for the built-in WebSocket.
//
// Run through scripts/screenshots.sh, which starts the browser in a container.
//
//   CDP=http://<browser>:9222 BASE=http://dashboard EMAIL=... PASSWORD=... node screenshots.mjs <out-dir>

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const outDir = process.argv[2]
const { CDP, BASE, EMAIL, PASSWORD } = process.env
if (!outDir || !CDP || !BASE || !EMAIL || !PASSWORD) {
  console.error('usage: CDP=... BASE=... EMAIL=... PASSWORD=... node screenshots.mjs <out-dir>')
  process.exit(2)
}
const WIDTH = Number(process.env.WIDTH ?? 1440)
mkdirSync(outDir, { recursive: true })

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

let target
for (let attempt = 0; attempt < 50 && !target; attempt++) {
  try {
    const pages = await (await fetch(`${CDP}/json`)).json()
    target = pages.find((page) => page.type === 'page') ?? (await (await fetch(`${CDP}/json/new?about:blank`, { method: 'PUT' })).json())
  } catch {
    await sleep(200)
  }
}
if (!target) throw new Error(`no browser at ${CDP}`)

const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve) => socket.addEventListener('open', resolve))
const pending = new Map()
const problems = []
let nextId = 1
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message)
    pending.delete(message.id)
  } else if (message.method === 'Runtime.exceptionThrown') {
    problems.push(message.params.exceptionDetails.exception?.description ?? message.params.exceptionDetails.text)
  }
})
const send = (method, params = {}) =>
  new Promise((resolve) => {
    const id = nextId++
    pending.set(id, resolve)
    socket.send(JSON.stringify({ id, method, params }))
  })
const evaluate = async (expression) =>
  (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value
const viewport = (height) => send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height, deviceScaleFactor: 1, mobile: false })

await send('Runtime.enable')
await send('Page.enable')
await viewport(1000)

async function capture(name) {
  const height = await evaluate('Math.max(document.documentElement.scrollHeight, 1000)')
  await viewport(Math.min(height, 4000))
  await sleep(400)
  const shot = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(outDir, `${name}.png`), Buffer.from(shot.result.data, 'base64'))
  await viewport(1000)
}

await send('Page.navigate', { url: `${BASE}/` })
await sleep(2500)
// React controls its inputs, so set values through the native setter and fire an input event.
await evaluate(`(() => {
  const set = (element, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  }
  set(document.querySelector('input[type=email]'), ${JSON.stringify(EMAIL)})
  set(document.querySelector('input[type=password]'), ${JSON.stringify(PASSWORD)})
  document.querySelector('form').requestSubmit()
})()`)
await sleep(6000)

const pages = [
  ['/', '01-dashboard', 5000],
  ['/policies', '02-policies', 2500],
  ['/algorithms', '03-algorithms', 2000],
  ['/analytics', '04-analytics', 3500],
  ['/monitoring', '05-monitoring', 6000],
]
for (const [path, name, wait] of pages) {
  // Navigate inside the app so the in-memory session survives.
  await evaluate(`(() => { history.pushState({}, '', ${JSON.stringify(path)}); dispatchEvent(new PopStateEvent('popstate')) })()`)
  await sleep(wait)
  const heading = await evaluate(`document.querySelector('h1')?.textContent ?? '(no heading)'`)
  const overflow = await evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
  console.log(`${name}: "${heading}", horizontal overflow ${overflow}px`)
  await capture(name)
}

if (problems.length) console.log('page errors:\n' + [...new Set(problems)].join('\n'))
socket.close()
process.exit(problems.length ? 1 : 0)

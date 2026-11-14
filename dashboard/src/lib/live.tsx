// Live data: one server-sent event stream shared by every page.
//
// EventSource cannot send an Authorization header, so the stream is read with fetch.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api, getAccessToken, restoreSession } from './api'
import type { DecisionEvent, LiveSnapshot, SeriesPoint } from './api'

const HISTORY_SECONDS = 300
const MAX_EVENTS = 200

interface LiveState {
  snapshot: LiveSnapshot | null
  /** Per-second traffic for the last five minutes, oldest first. */
  history: SeriesPoint[]
  events: DecisionEvent[]
  connected: boolean
}

const LiveContext = createContext<LiveState>({ snapshot: null, history: [], events: [], connected: false })

async function* frames(signal: AbortSignal): AsyncGenerator<{ event: string; data: string }> {
  const token = getAccessToken()
  const response = await fetch('/api/v1/stream', {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: 'same-origin',
    signal,
  })
  if (response.status === 401) throw new Error('unauthorized')
  if (!response.ok || !response.body) throw new Error(`stream failed: ${response.status}`)

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buffer += value
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      let event = 'message'
      const data: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      if (data.length > 0) yield { event, data: data.join('\n') }
      boundary = buffer.indexOf('\n\n')
    }
  }
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null)
  const [history, setHistory] = useState<SeriesPoint[]>([])
  const [events, setEvents] = useState<DecisionEvent[]>([])
  const [connected, setConnected] = useState(false)
  const lastSecond = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    let stopped = false

    async function backfill() {
      try {
        const { points } = await api.series(HISTORY_SECONDS, 1)
        if (stopped) return
        setHistory(points)
        lastSecond.current = points.at(-1)?.ts ?? 0
      } catch {
        // The stream fills the chart in anyway.
      }
    }

    async function run() {
      await backfill()
      let delay = 1000
      while (!stopped) {
        try {
          for await (const frame of frames(controller.signal)) {
            delay = 1000
            setConnected(true)
            if (frame.event === 'metrics') {
              const next = JSON.parse(frame.data) as LiveSnapshot
              setSnapshot(next)
              // Each frame reports a five-second average; chart the newest complete second instead.
              const second = Math.floor(next.ts) - 1
              if (second > lastSecond.current) {
                const gap = second - lastSecond.current
                lastSecond.current = second
                void api
                  .series(Math.max(10, Math.min(gap, HISTORY_SECONDS)), 1)
                  .then(({ points }) => {
                    if (stopped) return
                    setHistory((current) => {
                      const known = new Set(current.map((point) => point.ts))
                      const merged = [...current, ...points.filter((point) => !known.has(point.ts))]
                      // Later reads of a second are more complete; prefer them.
                      const latest = new Map(merged.map((point) => [point.ts, point]))
                      for (const point of points) latest.set(point.ts, point)
                      return [...latest.values()].sort((a, b) => a.ts - b.ts).slice(-HISTORY_SECONDS)
                    })
                  })
                  .catch(() => undefined)
              }
            } else if (frame.event === 'decisions') {
              const incoming = JSON.parse(frame.data) as DecisionEvent[]
              setEvents((current) => [...incoming.reverse(), ...current].slice(0, MAX_EVENTS))
            }
          }
        } catch (error) {
          if (stopped) return
          if (error instanceof Error && error.message === 'unauthorized') await restoreSession()
        }
        setConnected(false)
        await new Promise((resolve) => setTimeout(resolve, delay))
        delay = Math.min(delay * 2, 15000)
      }
    }

    void run()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [])

  const value = useMemo(() => ({ snapshot, history, events, connected }), [snapshot, history, events, connected])
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}

export function useLive(): LiveState {
  return useContext(LiveContext)
}

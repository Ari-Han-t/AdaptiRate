import { useCallback, useEffect, useRef, useState } from 'react'

export interface Loaded<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Load data, reload when `deps` change, and optionally poll. Stale responses are ignored. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[], pollMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const sequence = useRef(0)
  const loader = useRef(load)
  loader.current = load

  const run = useCallback(() => {
    const id = ++sequence.current
    loader
      .current()
      .then((result) => {
        if (id !== sequence.current) return
        setData(result)
        setError(null)
      })
      .catch((reason: unknown) => {
        if (id !== sequence.current) return
        setError(reason instanceof Error ? reason.message : 'Something went wrong')
      })
      .finally(() => {
        if (id === sequence.current) setLoading(false)
      })
  }, [])

  useEffect(() => {
    setLoading(true)
    run()
    if (pollMs <= 0) return
    const timer = setInterval(run, pollMs)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, pollMs, run])

  return { data, error, loading, reload: run }
}

export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}

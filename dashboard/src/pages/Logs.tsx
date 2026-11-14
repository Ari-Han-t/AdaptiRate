import { Download, Pause, Play, ScrollText, Search } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge, Button, Card, Empty, ErrorNote, Input, PageHeader, Select, Spinner, cn, useToast } from '../components/ui'
import { api } from '../lib/api'
import type { DecisionEvent, LogFilters } from '../lib/api'
import { REASON_LABEL, num } from '../lib/format'
import { useDebounced, useLoad } from '../lib/hooks'
import { useLive } from '../lib/live'

function timeOf(event: DecisionEvent): Date {
  return typeof event.ts === 'number' ? new Date(event.ts * 1000) : new Date(event.ts)
}

function LogRow({ event }: { event: DecisionEvent }) {
  const at = timeOf(event)
  return (
    <li className="grid grid-cols-[auto_1fr_auto] items-center gap-x-4 gap-y-1 border-b border-line px-4 py-2.5 text-sm last:border-b-0 hover:bg-canvas md:grid-cols-[150px_110px_minmax(0,1.4fr)_minmax(0,1fr)_130px_110px]">
      <time className="font-mono text-xs tabular-nums text-ink-faint" dateTime={at.toISOString()} title={at.toISOString()}>
        {at.toLocaleDateString([], { month: 'short', day: '2-digit' })} {at.toLocaleTimeString([], { hour12: false })}
      </time>
      <span className="justify-self-start md:col-auto">
        <Badge tone={event.allowed ? 'good' : 'bad'}>{event.allowed ? 'Allowed' : 'Blocked'}</Badge>
      </span>
      <span className="col-span-3 truncate font-mono text-xs text-ink md:col-auto" title={event.subject}>
        {event.subject}
      </span>
      <span className="col-span-2 truncate text-ink-soft md:col-auto">{event.policy}</span>
      <span className={cn('text-xs', event.allowed ? 'text-ink-faint' : 'text-bad')}>{REASON_LABEL[event.reason] ?? event.reason}</span>
      <span className="hidden text-right text-xs tabular-nums text-ink-faint md:block">
        {num(event.remaining)} / {num(event.limit)}
      </span>
    </li>
  )
}

export function LogsPage() {
  const toast = useToast()
  const live = useLive()
  const policies = useLoad(api.policies, [])

  const [search, setSearch] = useState('')
  const [policy, setPolicy] = useState('')
  const [outcome, setOutcome] = useState('')
  const [reason, setReason] = useState('')
  const [since, setSince] = useState('')
  const [tail, setTail] = useState(true)
  const debouncedSearch = useDebounced(search, 300)

  const filters = useMemo<LogFilters>(
    () => ({
      q: debouncedSearch.trim() || undefined,
      policy: policy || undefined,
      allowed: outcome === '' ? null : outcome === 'allowed',
      reason: reason || undefined,
      since: since ? new Date(since).toISOString() : undefined,
    }),
    [debouncedSearch, policy, outcome, reason, since],
  )

  const [items, setItems] = useState<DecisionEvent[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (after: number | null) => {
      const page = await api.logs(filters, after)
      setItems((current) => (after === null ? page.items : [...current, ...page.items]))
      setCursor(page.next_cursor)
    },
    [filters],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .logs(filters, null)
      .then((page) => {
        if (cancelled) return
        setItems(page.items)
        setCursor(page.next_cursor)
        setError(null)
      })
      .catch((reasonCaught: unknown) => {
        if (!cancelled) setError(reasonCaught instanceof Error ? reasonCaught.message : 'Could not load logs')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [filters])

  // Live tail: stream events that match the current filters, shown above the stored page.
  const tailed = useMemo(() => {
    if (!tail) return []
    const needle = filters.q?.toLowerCase()
    const sinceMs = filters.since ? new Date(filters.since).getTime() : 0
    return live.events.filter(
      (event) =>
        (!needle || event.subject.toLowerCase().includes(needle)) &&
        (!filters.policy || event.policy === filters.policy) &&
        (filters.allowed === null || filters.allowed === undefined || event.allowed === filters.allowed) &&
        (!filters.reason || event.reason === filters.reason) &&
        timeOf(event).getTime() >= sinceMs,
    )
  }, [tail, live.events, filters])

  // Stored rows use database ids and streamed rows use stream ids, so de-duplicate on content.
  const merged = useMemo(() => {
    const signature = (event: DecisionEvent) => `${Math.round(timeOf(event).getTime())}|${event.policy}|${event.subject}|${event.allowed}|${event.remaining}`
    const stored = new Set(items.map(signature))
    return [...tailed.filter((event) => !stored.has(signature(event))), ...items]
  }, [tailed, items])

  async function download(format: 'csv' | 'ndjson') {
    try {
      const blob = await api.exportLogs(filters, format)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `adaptirate-logs.${format}`
      link.click()
      URL.revokeObjectURL(url)
    } catch (reasonCaught) {
      toast.error(reasonCaught)
    }
  }

  return (
    <>
      <PageHeader
        title="Logs"
        subtitle="Every recorded rate-limit decision"
        action={
          <div className="flex gap-2">
            <Button onClick={() => setTail((current) => !current)} aria-pressed={tail}>
              {tail ? <Pause size={15} /> : <Play size={15} />} {tail ? 'Pause live tail' : 'Resume live tail'}
            </Button>
            <Button onClick={() => void download('csv')}>
              <Download size={15} /> CSV
            </Button>
            <Button onClick={() => void download('ndjson')}>
              <Download size={15} /> JSON
            </Button>
          </div>
        }
      />

      <Card className="mb-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr_1fr_1.3fr]">
          <div className="relative">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
            <Input aria-label="Search subject" placeholder="Search by user, IP or key" className="pl-9" value={search} onChange={(event) => setSearch(event.target.value)} />
          </div>
          <Select aria-label="Policy" value={policy} onChange={(event) => setPolicy(event.target.value)}>
            <option value="">All policies</option>
            {policies.data?.map((item) => (
              <option key={item.id} value={item.name}>
                {item.name}
              </option>
            ))}
          </Select>
          <Select aria-label="Outcome" value={outcome} onChange={(event) => setOutcome(event.target.value)}>
            <option value="">Any outcome</option>
            <option value="allowed">Allowed</option>
            <option value="blocked">Blocked</option>
          </Select>
          <Select aria-label="Reason" value={reason} onChange={(event) => setReason(event.target.value)}>
            <option value="">Any reason</option>
            {Object.entries(REASON_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
          <Input aria-label="Since" type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} />
        </div>
      </Card>

      {error && <ErrorNote message={error} onRetry={() => void load(null)} />}

      <Card className="overflow-hidden p-0">
        <div className="hidden border-b border-line bg-canvas px-4 py-2.5 text-[11px] font-medium uppercase tracking-wider text-ink-faint md:grid md:grid-cols-[150px_110px_minmax(0,1.4fr)_minmax(0,1fr)_130px_110px] md:gap-x-4">
          <span>Time</span>
          <span>Outcome</span>
          <span>Subject</span>
          <span>Policy</span>
          <span>Reason</span>
          <span className="text-right">Remaining</span>
        </div>
        {loading ? (
          <Spinner />
        ) : merged.length === 0 ? (
          <Empty icon={<ScrollText size={22} />} title="No matching decisions">
            Change the filters, or send some traffic. Under heavy load only a sample of decisions is stored.
          </Empty>
        ) : (
          <ul>
            {merged.map((event) => (
              <LogRow key={`${typeof event.id}-${event.id}`} event={event} />
            ))}
          </ul>
        )}
      </Card>

      {cursor !== null && !loading && (
        <div className="mt-4 flex justify-center">
          <Button
            busy={loadingMore}
            onClick={async () => {
              setLoadingMore(true)
              try {
                await load(cursor)
              } catch (reasonCaught) {
                toast.error(reasonCaught)
              } finally {
                setLoadingMore(false)
              }
            }}
          >
            Load older
          </Button>
        </div>
      )}
    </>
  )
}

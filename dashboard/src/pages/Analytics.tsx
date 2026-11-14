import { BarChart3 } from 'lucide-react'
import { useState } from 'react'
import { COLORS, Heatmap, LineSeries, TrafficChart } from '../components/charts'
import { Card, CardTitle, Empty, ErrorNote, PageHeader, Select, Spinner } from '../components/ui'
import { api } from '../lib/api'
import type { TopClient } from '../lib/api'
import { dateTime, ms, num, percent } from '../lib/format'
import { useLoad } from '../lib/hooks'

const RANGES = [
  { hours: 1, label: 'Last hour' },
  { hours: 6, label: 'Last 6 hours' },
  { hours: 24, label: 'Last 24 hours' },
  { hours: 168, label: 'Last 7 days' },
  { hours: 720, label: 'Last 30 days' },
]

function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card p-4">
      <p className="text-xs font-medium text-ink-soft">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{value}</p>
      {hint && <p className="mt-1 truncate text-xs text-ink-faint">{hint}</p>}
    </div>
  )
}

function Ranking({ rows, color, empty }: { rows: TopClient[]; color: string; empty: string }) {
  if (rows.length === 0) return <p className="py-8 text-center text-sm text-ink-faint">{empty}</p>
  const max = Math.max(...rows.map((row) => row.requests))
  return (
    <ol className="space-y-2.5">
      {rows.map((row, index) => (
        <li key={`${row.policy}|${row.subject}`}>
          <div className="flex items-baseline gap-2 text-sm">
            <span className="w-5 text-xs tabular-nums text-ink-faint">{index + 1}</span>
            <span className="truncate font-medium text-ink" title={row.subject}>
              {row.subject}
            </span>
            <span className="shrink-0 text-xs text-ink-faint">{row.policy}</span>
            <span className="ml-auto shrink-0 tabular-nums text-ink-soft">{num(row.requests)}</span>
          </div>
          <div className="ml-7 mt-1 h-1.5 overflow-hidden rounded-full bg-raised">
            <div className="h-full rounded-full" style={{ width: `${(row.requests / max) * 100}%`, background: color }} />
          </div>
        </li>
      ))}
    </ol>
  )
}

export function AnalyticsPage() {
  const [hours, setHours] = useState(24)
  const [policy, setPolicy] = useState('')
  const selected = policy || undefined

  const policies = useLoad(api.policies, [])
  const overview = useLoad(() => api.overview(hours, selected), [hours, policy], 30000)
  const series = useLoad(() => api.timeseries(hours, selected), [hours, policy], 30000)
  const heat = useLoad(() => api.heatmap(Math.max(7, Math.ceil(hours / 24)), selected), [hours, policy], 60000)
  const top = useLoad(() => api.top(Math.min(hours, 24)), [hours], 30000)

  const points = series.data?.points ?? []
  const formatTime = hours <= 24 ? (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : (ts: number) => dateTime(ts)
  const hasData = (overview.data?.total ?? 0) > 0
  const error = overview.error ?? series.error ?? heat.error ?? top.error

  return (
    <>
      <PageHeader
        title="Analytics"
        subtitle="Traffic, outcomes and adaptation over time"
        action={
          <div className="flex gap-2">
            <Select aria-label="Policy" className="w-48" value={policy} onChange={(event) => setPolicy(event.target.value)}>
              <option value="">All policies</option>
              {policies.data?.map((item) => (
                <option key={item.id} value={item.name}>
                  {item.name}
                </option>
              ))}
            </Select>
            <Select aria-label="Time range" className="w-40" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
              {RANGES.map((range) => (
                <option key={range.hours} value={range.hours}>
                  {range.label}
                </option>
              ))}
            </Select>
          </div>
        }
      />
      {error && <ErrorNote message={error} />}
      {!overview.data ? (
        <Spinner />
      ) : !hasData ? (
        <Card>
          <Empty icon={<BarChart3 size={22} />} title="No traffic in this range">
            Analytics are built from one-minute rollups, so new traffic appears here about a minute after it happens.
          </Empty>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <Figure label="Requests" value={num(overview.data.total)} hint={`${num(overview.data.blocked)} blocked`} />
            <Figure label="Success rate" value={percent(overview.data.success_rate, 1)} />
            <Figure label="Peak" value={`${num(overview.data.peak_requests_per_minute)}/min`} hint={overview.data.peak_minute ? dateTime(overview.data.peak_minute) : undefined} />
            <Figure label="Average upstream latency" value={ms(overview.data.avg_latency_ms)} hint={overview.data.worst_p95_latency_ms !== null ? `worst p95 ${ms(overview.data.worst_p95_latency_ms)}` : 'no SDK reports'} />
            <Figure label="Average health" value={overview.data.avg_health === null ? '–' : String(Math.round(overview.data.avg_health))} hint={overview.data.upstream_error_rate !== null ? `upstream errors ${percent(overview.data.upstream_error_rate, 1)}` : undefined} />
          </div>

          <Card className="mt-4">
            <CardTitle hint={`Requests per ${series.data && series.data.bucket_seconds >= 3600 ? `${series.data.bucket_seconds / 3600} h` : `${(series.data?.bucket_seconds ?? 60) / 60} min`} bucket`}>Traffic volume</CardTitle>
            <TrafficChart data={points} height={280} formatTime={formatTime} unit="requests per bucket" />
          </Card>

          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <Card>
              <CardTitle hint={policy ? 'Average effective limit' : 'Select one policy to see its limit'}>Adaptive limit over time</CardTitle>
              {policy && points.some((point) => point.limit !== null) ? (
                <LineSeries data={points} dataKey="limit" name="Limit" color={COLORS.limit} formatTime={formatTime} />
              ) : (
                <p className="py-16 text-center text-sm text-ink-faint">{policy ? 'No adaptive data in this range.' : 'Choose a policy above.'}</p>
              )}
            </Card>
            <Card>
              <CardTitle hint="Worst p95 reported by protected services">Upstream latency</CardTitle>
              {points.some((point) => point.latency_p95_ms !== null) ? (
                <LineSeries data={points} dataKey="latency_p95_ms" name="p95 ms" color={COLORS.latency} formatTime={formatTime} />
              ) : (
                <p className="py-16 text-center text-sm text-ink-faint">No latency reports. Wrap your handlers with the SDK middleware to send them.</p>
              )}
            </Card>
          </div>

          <Card className="mt-4">
            <CardTitle
              hint={
                heat.data && heat.data.peak_hours.length > 0
                  ? `Busiest hours: ${heat.data.peak_hours.map((hour) => `${String(hour).padStart(2, '0')}:00`).join(', ')} (your local time)`
                  : 'By weekday and hour, in your local time'
              }
            >
              Traffic heatmap
            </CardTitle>
            {heat.data ? <Heatmap cells={heat.data.cells} /> : <Spinner />}
          </Card>

          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <Card>
              <CardTitle hint={`Allowed requests, last ${Math.min(hours, 24)} h`}>Top clients</CardTitle>
              <Ranking rows={top.data?.allowed ?? []} color={COLORS.allowed} empty="No allowed requests yet." />
            </Card>
            <Card>
              <CardTitle hint={`Blocked requests, last ${Math.min(hours, 24)} h`}>Most blocked</CardTitle>
              <Ranking rows={top.data?.blocked ?? []} color={COLORS.blocked} empty="Nothing has been blocked." />
            </Card>
          </div>
        </>
      )}
    </>
  )
}

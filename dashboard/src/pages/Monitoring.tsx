import { CheckCircle2, Database, HardDrive, Server, Workflow, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { COLORS, LineSeries } from '../components/charts'
import { Badge, Card, CardTitle, ErrorNote, Meter, PageHeader, Spinner } from '../components/ui'
import { api } from '../lib/api'
import type { Monitoring } from '../lib/api'
import { bytes, duration, ms, num, percent } from '../lib/format'
import { useLoad } from '../lib/hooks'

interface Sample {
  ts: number
  cpu: number | null
  memory: number | null
  redis: number | null
  postgres: number | null
}

function Rows({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="space-y-2 text-sm">
      {items.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4">
          <dt className="text-ink-soft">{label}</dt>
          <dd className="truncate text-right font-medium tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Usage({ label, value }: { label: string; value: number | null }) {
  const tone = value === null ? 'idle' : value >= 90 ? 'bad' : value >= 70 ? 'warn' : 'good'
  return (
    <div>
      <div className="mb-1.5 flex justify-between text-sm">
        <span className="text-ink-soft">{label}</span>
        <span className="font-medium tabular-nums">{value === null ? '–' : `${value.toFixed(0)}%`}</span>
      </div>
      <Meter value={value} tone={tone} />
    </div>
  )
}

export function MonitoringPage() {
  const { data, error, reload } = useLoad<Monitoring>(api.monitoring, [], 2000)
  const [samples, setSamples] = useState<Sample[]>([])
  const lastTs = useRef(0)

  useEffect(() => {
    if (!data || data.ts === lastTs.current) return
    lastTs.current = data.ts
    setSamples((current) =>
      [
        ...current,
        {
          ts: Math.floor(data.ts),
          cpu: data.host.cpu_percent,
          memory: data.host.memory_percent,
          redis: data.redis.latency_ms ?? null,
          postgres: data.postgres.latency_ms ?? null,
        },
      ].slice(-150),
    )
  }, [data])

  if (!data) return error ? <ErrorNote message={error} onRetry={reload} /> : <Spinner />

  return (
    <>
      <PageHeader
        title="Monitoring"
        subtitle="The health of AdaptiRate itself"
        action={<Badge tone={data.status === 'ok' ? 'good' : 'bad'}>{data.status === 'ok' ? 'All systems operational' : 'Degraded'}</Badge>}
      />
      {error && <ErrorNote message={error} />}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {data.checks.map((check) => (
          <div key={check.name} className="card flex items-center gap-3 p-4">
            {check.ok ? <CheckCircle2 size={22} className="shrink-0 text-good" /> : <XCircle size={22} className="shrink-0 text-bad" />}
            <div className="min-w-0">
              <p className="text-sm font-medium">{check.name}</p>
              <p className="truncate text-xs text-ink-faint">{check.detail}</p>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Card>
          <CardTitle hint={`${data.host.cpu_count} cores${data.host.load_1m != null ? ` · load ${data.host.load_1m.toFixed(2)}` : ''}`}>
            <span className="flex items-center gap-2">
              <Server size={16} className="text-accent-strong" /> Host
            </span>
          </CardTitle>
          <div className="space-y-4">
            <Usage label="CPU" value={data.host.cpu_percent} />
            <Usage label="Memory" value={data.host.memory_percent} />
          </div>
          <p className="mt-4 text-xs text-ink-faint">Sampled by the worker each second. Used as a fallback signal when SDKs do not report their own.</p>
        </Card>

        <Card className="xl:col-span-2">
          <CardTitle hint="While this page is open">CPU</CardTitle>
          <LineSeries data={samples} dataKey="cpu" name="CPU %" color={COLORS.latency} height={170} domain={[0, 100]} />
        </Card>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <Card>
          <CardTitle hint={data.redis.version ? `Redis ${data.redis.version}` : undefined} action={<Badge tone={data.redis.ok ? 'good' : 'bad'}>{data.redis.ok ? 'Up' : 'Down'}</Badge>}>
            <span className="flex items-center gap-2">
              <HardDrive size={16} className="text-bad" /> Redis
            </span>
          </CardTitle>
          <Rows
            items={[
              ['Round-trip latency', ms(data.redis.latency_ms)],
              ['Commands per second', num(data.redis.ops_per_second)],
              ['Memory used', bytes(data.redis.used_memory_bytes)],
              ['Keys', num(data.redis.keys)],
              ['Connected clients', num(data.redis.connected_clients)],
              ['Cache hit rate', percent(data.redis.cache_hit_rate, 1)],
              ['Uptime', duration(data.redis.uptime_seconds)],
            ]}
          />
          <div className="mt-4">
            <LineSeries data={samples} dataKey="redis" name="Redis ms" color={COLORS.blocked} height={120} />
          </div>
        </Card>

        <Card>
          <CardTitle hint={data.postgres.version ? `PostgreSQL ${data.postgres.version}` : undefined} action={<Badge tone={data.postgres.ok ? 'good' : 'bad'}>{data.postgres.ok ? 'Up' : 'Down'}</Badge>}>
            <span className="flex items-center gap-2">
              <Database size={16} className="text-accent-strong" /> PostgreSQL
            </span>
          </CardTitle>
          <Rows
            items={[
              ['Round-trip latency', ms(data.postgres.latency_ms)],
              ['Database size', bytes(data.postgres.size_bytes)],
              ['Connections', data.postgres.connections !== undefined ? `${data.postgres.connections} of ${data.postgres.max_connections}` : '–'],
              ['Decision log rows', num(data.postgres.rows?.decision_logs)],
              ['Rollup rows', num(data.postgres.rows?.metric_rollups)],
              ['Audit rows', num(data.postgres.rows?.audit_logs)],
            ]}
          />
          <div className="mt-4">
            <LineSeries data={samples} dataKey="postgres" name="Postgres ms" color={COLORS.allowed} height={120} />
          </div>
        </Card>
      </div>

      <Card className="mt-4">
        <CardTitle hint="The worker runs the adaptive loop, writes logs and rollups. API processes answer evaluate calls.">
          <span className="flex items-center gap-2">
            <Workflow size={16} className="text-violet" /> Processes
          </span>
        </CardTitle>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <div className="rounded-xl border border-line bg-canvas p-4">
            <div className="mb-3 flex items-center justify-between">
              <span className="font-medium">Adaptive worker</span>
              <Badge tone={data.worker.ok ? 'good' : 'bad'}>{data.worker.ok ? 'Leading' : 'No heartbeat'}</Badge>
            </div>
            <Rows
              items={[
                ['Instance', data.worker.instance ?? '–'],
                ['Last seen', data.worker.last_seen_seconds === null ? 'never' : `${data.worker.last_seen_seconds.toFixed(1)}s ago`],
                ['Tick duration', ms(data.worker.tick_ms)],
                ['Log rows written', num(data.worker.logs_written)],
              ]}
            />
          </div>
          {data.api_processes.map((process) => (
            <div key={process.instance} className="rounded-xl border border-line bg-canvas p-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="font-medium">API process</span>
                <Badge tone={process.last_seen_seconds < 15 ? 'good' : 'warn'}>{process.last_seen_seconds < 15 ? 'Serving' : 'Stale'}</Badge>
              </div>
              <Rows
                items={[
                  ['Instance', process.instance],
                  ['Uptime', duration(data.ts - process.started)],
                  ['Cached policies / keys', `${process.policies} / ${process.api_keys}`],
                  ['Log events dropped', num(process.events_dropped)],
                ]}
              />
            </div>
          ))}
        </div>
      </Card>
    </>
  )
}

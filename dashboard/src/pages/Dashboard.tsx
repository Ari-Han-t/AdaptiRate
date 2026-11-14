import { Gauge, Hourglass, PauseCircle } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { HealthRing, Sparkline, TrafficChart, COLORS } from '../components/charts'
import { Badge, Card, CardTitle, Empty, Meter, PageHeader, Spinner, cn } from '../components/ui'
import type { PolicyState, ProviderState } from '../lib/api'
import { ALGORITHM_LABEL, REASON_LABEL, clock, duration, healthTone, ms, num, percent, windowLabel } from '../lib/format'
import { useLive } from '../lib/live'

function Stat({ label, value, unit, spark, sparkColor }: { label: string; value: string; unit?: string; spark?: number[]; sparkColor?: string }) {
  return (
    <div className="card flex flex-col justify-between p-4">
      <p className="text-xs font-medium text-ink-soft">{label}</p>
      <p className="mt-2 text-[26px] font-semibold leading-none tracking-tight tabular-nums">
        {value}
        {unit && <span className="ml-1 text-sm font-normal text-ink-faint">{unit}</span>}
      </p>
      <div className="mt-2 h-9">{spark && spark.length > 1 && <Sparkline values={spark} color={sparkColor} />}</div>
    </div>
  )
}

const PENALTY_LABEL: Record<string, string> = {
  latency: 'Latency',
  errors: 'Errors',
  throttled: 'Upstream 429s',
  timeouts: 'Timeouts',
  queue: 'Queue',
  cpu: 'CPU',
  memory: 'Memory',
}

function PolicyRow({ policy }: { policy: PolicyState }) {
  const tone = healthTone(policy.health)
  const share = policy.effective_limit / policy.base_limit
  return (
    <div className="rounded-xl border border-line bg-canvas p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{policy.name}</span>
        <Badge tone="idle">{ALGORITHM_LABEL[policy.algorithm]}</Badge>
        {!policy.enabled && <Badge tone="warn">Disabled</Badge>}
        {policy.adaptive ? <Badge tone="accent">Adaptive</Badge> : <Badge tone="idle">Fixed</Badge>}
        {policy.paused_ms > 0 && (
          <Badge tone="bad">
            <PauseCircle size={11} /> Paused {duration(policy.paused_ms / 1000)}
          </Badge>
        )}
        {policy.action === 'decrease' && <Badge tone="bad">Shedding load</Badge>}
        {policy.action === 'increase' && <Badge tone="good">Recovering</Badge>}
        <span className="ml-auto text-sm tabular-nums">
          <span className={cn('font-semibold', share < 0.999 ? 'text-warn' : 'text-ink')}>{num(policy.effective_limit)}</span>
          <span className="text-ink-faint">
            {' '}
            / {num(policy.base_limit)} per {windowLabel(policy.window_seconds)}
          </span>
        </span>
      </div>
      <div className="mt-2.5">
        <Meter value={policy.effective_limit} max={Math.max(policy.base_limit, policy.effective_limit)} tone={share < 0.6 ? 'bad' : share < 0.999 ? 'warn' : 'accent'} />
      </div>
      {(policy.health !== null || Object.keys(policy.signals).length > 0) && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-faint">
          {policy.health !== null && (
            <span>
              Health <span className={cn('font-medium', { good: 'text-good', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-soft' }[tone])}>{Math.round(policy.health)}</span>
            </span>
          )}
          {policy.signals.latency_p95_ms !== undefined && <span>p95 {ms(policy.signals.latency_p95_ms)}</span>}
          {policy.signals.error_rate !== undefined && <span>errors {percent(policy.signals.error_rate, 1)}</span>}
          {policy.signals.in_flight !== undefined && <span>in flight {num(policy.signals.in_flight)}</span>}
          {Object.entries(policy.penalties)
            .filter(([, value]) => value > 0.05)
            .map(([name, value]) => (
              <Badge key={name} tone={value > 0.6 ? 'bad' : 'warn'}>
                {PENALTY_LABEL[name] ?? name} {Math.round(value * 100)}%
              </Badge>
            ))}
        </div>
      )}
    </div>
  )
}

function ProviderRow({ provider }: { provider: ProviderState }) {
  const hasRequests = provider.remaining_requests !== null && provider.limit_requests !== null && provider.limit_requests > 0
  const hasTokens = provider.remaining_tokens !== null && provider.limit_tokens !== null && provider.limit_tokens > 0
  const share = hasRequests ? provider.remaining_requests! / provider.limit_requests! : null
  return (
    <div className="rounded-xl border border-line bg-canvas p-3.5">
      <div className="flex items-center gap-2">
        <span className="font-medium capitalize text-ink">{provider.name}</span>
        {provider.paused_ms > 0 ? <Badge tone="bad">Backing off {duration(provider.paused_ms / 1000)}</Badge> : provider.updated_at ? <Badge tone="good">OK</Badge> : <Badge tone="idle">No data</Badge>}
        {provider.throttled > 0 && <span className="ml-auto text-xs text-ink-faint">{num(provider.throttled)} throttles</span>}
      </div>
      {hasRequests && (
        <div className="mt-2.5">
          <div className="mb-1 flex justify-between text-xs text-ink-faint">
            <span>Requests</span>
            <span className="tabular-nums">
              {num(provider.remaining_requests)} / {num(provider.limit_requests)}
              {provider.reset_requests_in !== null && ` · resets in ${duration(provider.reset_requests_in)}`}
            </span>
          </div>
          <Meter value={provider.remaining_requests} max={provider.limit_requests!} tone={share !== null && share < 0.15 ? 'bad' : share !== null && share < 0.4 ? 'warn' : 'good'} />
        </div>
      )}
      {hasTokens && (
        <div className="mt-2.5">
          <div className="mb-1 flex justify-between text-xs text-ink-faint">
            <span>Tokens</span>
            <span className="tabular-nums">
              {num(provider.remaining_tokens)} / {num(provider.limit_tokens)}
            </span>
          </div>
          <Meter value={provider.remaining_tokens} max={provider.limit_tokens!} tone="accent" />
        </div>
      )}
      {!hasRequests && !hasTokens && provider.remaining_requests !== null && (
        <p className="mt-2 text-xs text-ink-faint">
          {num(provider.remaining_requests)} requests left
          {provider.reset_requests_in !== null && ` · resets in ${duration(provider.reset_requests_in)}`}
        </p>
      )}
    </div>
  )
}

export function DashboardPage() {
  const { snapshot, history, events } = useLive()

  const sparks = useMemo(() => {
    const recent = history.slice(-60)
    return {
      total: recent.map((point) => point.allowed + point.blocked),
      allowed: recent.map((point) => point.allowed),
      blocked: recent.map((point) => point.blocked),
    }
  }, [history])

  if (!snapshot) return <Spinner label="Connecting to live metrics" />

  const adaptive = snapshot.policies.filter((policy) => policy.enabled && policy.adaptive)
  const lead = [...snapshot.policies].sort((a, b) => (a.health ?? 101) - (b.health ?? 101))[0]
  const algorithms = [...new Set(snapshot.policies.filter((p) => p.enabled).map((p) => ALGORITHM_LABEL[p.algorithm]))]
  const providers = snapshot.providers.filter((provider) => provider.updated_at !== null || provider.paused_ms > 0)

  return (
    <>
      <PageHeader title="Dashboard" subtitle="Live traffic, adaptive limits and system health" />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4 2xl:grid-cols-8">
        <Stat label="Requests / sec" value={num(snapshot.requests_per_second, 1)} spark={sparks.total} sparkColor={COLORS.allowed} />
        <Stat label="Allowed / sec" value={num(snapshot.allowed_per_second, 1)} spark={sparks.allowed} sparkColor={COLORS.good} />
        <Stat label="Blocked / sec" value={num(snapshot.blocked_per_second, 1)} spark={sparks.blocked} sparkColor={COLORS.blocked} />
        <Stat label="Success rate (1m)" value={percent(snapshot.success_rate, 1)} />
        <Stat label="CPU" value={snapshot.cpu_percent === null ? '–' : snapshot.cpu_percent.toFixed(0)} unit="%" />
        <Stat label="Memory" value={snapshot.memory_percent === null ? '–' : snapshot.memory_percent.toFixed(0)} unit="%" />
        <Stat label="Upstream p95" value={ms(snapshot.latency_p95_ms)} />
        <Stat label="Queue depth" value={num(snapshot.queue_depth)} />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardTitle hint="Per second, last five minutes">Traffic</CardTitle>
          <TrafficChart data={history} height={300} />
          <div className="mt-3 flex gap-5 text-xs text-ink-soft">
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-accent" /> Allowed
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-bad" /> Blocked
            </span>
          </div>
        </Card>

        <Card>
          <CardTitle hint="Lowest score across enabled policies">Adaptive engine</CardTitle>
          <div className="flex flex-col items-center">
            <HealthRing health={snapshot.health} />
            <dl className="mt-5 w-full space-y-2.5 text-sm">
              <div className="flex justify-between">
                <dt className="text-ink-soft">Adaptive policies</dt>
                <dd className="font-medium tabular-nums">
                  {adaptive.length} of {snapshot.policies.length}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="shrink-0 text-ink-soft">Algorithms in use</dt>
                <dd className="truncate text-right font-medium" title={algorithms.join(', ')}>
                  {algorithms.length ? algorithms.join(', ') : '–'}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-ink-soft">Tightest limit</dt>
                <dd className="font-medium tabular-nums">{lead ? `${lead.name} · ${Math.round(lead.multiplier * 100)}%` : '–'}</dd>
              </div>
            </dl>
          </div>
        </Card>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardTitle hint="Current limit against the configured limit" action={<Link to="/policies" className="text-xs font-medium text-accent-strong hover:underline">Manage</Link>}>
            Policies
          </CardTitle>
          {snapshot.policies.length === 0 ? (
            <Empty icon={<Gauge size={20} />} title="No policies yet" action={<Link to="/policies" className="text-sm font-medium text-accent-strong hover:underline">Create your first policy</Link>}>
              A policy defines a limit and which requests share it.
            </Empty>
          ) : (
            <div className="space-y-2.5">
              {snapshot.policies.map((policy) => (
                <PolicyRow key={policy.id} policy={policy} />
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-4">
          <Card>
            <CardTitle hint="Remaining quota reported by upstream APIs">Provider quotas</CardTitle>
            {providers.length === 0 ? (
              <Empty icon={<Hourglass size={20} />} title="No provider traffic yet">
                Report provider responses from the SDK to pace against their quotas.
              </Empty>
            ) : (
              <div className="space-y-2.5">
                {providers.map((provider) => (
                  <ProviderRow key={provider.name} provider={provider} />
                ))}
              </div>
            )}
          </Card>

          <Card>
            <CardTitle hint="Newest first" action={<Link to="/logs" className="text-xs font-medium text-accent-strong hover:underline">All logs</Link>}>
              Recent decisions
            </CardTitle>
            {events.length === 0 ? (
              <p className="py-6 text-center text-sm text-ink-faint">Waiting for traffic…</p>
            ) : (
              <ul className="space-y-1.5">
                {events.slice(0, 9).map((event) => (
                    <li key={event.id} className="flex items-center gap-2.5 text-xs">
                      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', event.allowed ? 'bg-good' : 'bg-bad')} />
                      <span className="w-14 shrink-0 tabular-nums text-ink-faint">{clock(Number(event.ts))}</span>
                      <span className="truncate text-ink">{event.subject}</span>
                      <span className="ml-auto shrink-0 text-ink-faint">{event.policy}</span>
                      <span className={cn('w-24 shrink-0 text-right', event.allowed ? 'text-ink-faint' : 'text-bad')}>{REASON_LABEL[event.reason] ?? event.reason}</span>
                    </li>
                  ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  )
}

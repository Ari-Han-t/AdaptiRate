import { motion } from 'framer-motion'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { HeatCell } from '../lib/api'
import { TONE_COLOR, clock, healthTone, num } from '../lib/format'

const AXIS = { stroke: '#98a2b3', fontSize: 11, tickLine: false, axisLine: false } as const
const GRID = '#eaecf0'
const CURSOR = '#cfd4dc'
/** Flat series colours. Areas use the same colour at low opacity; no gradients. */
export const COLORS = { allowed: '#2563eb', blocked: '#e5484d', limit: '#6d28d9', latency: '#d97706', good: '#16a34a' }
const FILL_OPACITY = 0.12

interface TipRow {
  name?: string | number
  value?: number | string | null
  color?: string
}

function Tip({ active, payload, label, format }: { active?: boolean; payload?: TipRow[]; label?: number | string; format: (value: number) => string }) {
  if (!active || !payload?.length) return null
  return (
    <div className="card px-3 py-2 text-xs shadow-lg">
      <p className="mb-1 font-medium text-ink-soft">{typeof label === 'number' ? format(label) : label}</p>
      {payload
        .filter((row) => row.value !== null && row.value !== undefined)
        .map((row) => (
          <p key={String(row.name)} className="flex items-center gap-2 text-ink">
            <span className="h-2 w-2 rounded-full" style={{ background: row.color }} />
            <span className="text-ink-soft">{row.name}</span>
            <span className="ml-auto pl-4 font-medium tabular-nums">{num(Number(row.value), Number(row.value) < 100 ? 1 : 0)}</span>
          </p>
        ))}
    </div>
  )
}

export interface TrafficDatum {
  ts: number
  allowed: number
  blocked: number
  limit?: number | null
}

/** Stacked allowed/blocked area with the adaptive limit drawn over it. */
export function TrafficChart({ data, height = 300, formatTime = clock, showLimit = false, unit = 'req/s' }: { data: TrafficDatum[]; height?: number; formatTime?: (ts: number) => string; showLimit?: boolean; unit?: string }) {
  return (
    <div style={{ height }} role="img" aria-label={`Allowed and blocked traffic in ${unit}`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 6, right: 6, left: -14, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="ts" tickFormatter={formatTime} minTickGap={48} {...AXIS} />
          <YAxis tickFormatter={(value: number) => num(value)} width={52} {...AXIS} />
          <Tooltip content={<Tip format={formatTime} />} cursor={{ stroke: CURSOR }} />
          <Area name="Allowed" type="monotone" dataKey="allowed" stackId="traffic" stroke={COLORS.allowed} strokeWidth={1.75} fill={COLORS.allowed} fillOpacity={FILL_OPACITY} isAnimationActive={false} />
          <Area name="Blocked" type="monotone" dataKey="blocked" stackId="traffic" stroke={COLORS.blocked} strokeWidth={1.75} fill={COLORS.blocked} fillOpacity={FILL_OPACITY} isAnimationActive={false} />
          {showLimit && <Line name="Limit" type="stepAfter" dataKey="limit" stroke={COLORS.limit} strokeWidth={1.75} strokeDasharray="5 4" dot={false} connectNulls isAnimationActive={false} />}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

export function LineSeries({ data, dataKey, name, color, height = 220, formatTime = clock, domain }: { data: object[]; dataKey: string; name: string; color: string; height?: number; formatTime?: (ts: number) => string; domain?: [number, number] }) {
  return (
    <div style={{ height }} role="img" aria-label={name}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 6, left: -14, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="ts" tickFormatter={formatTime} minTickGap={48} {...AXIS} />
          <YAxis tickFormatter={(value: number) => num(value)} width={52} domain={domain} {...AXIS} />
          <Tooltip content={<Tip format={formatTime} />} cursor={{ stroke: CURSOR }} />
          <Area name={name} type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.75} fill={color} fillOpacity={FILL_OPACITY} connectNulls isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export function Bars({ data, dataKey, labelKey, name, color = COLORS.allowed, height = 160 }: { data: object[]; dataKey: string; labelKey: string; name: string; color?: string; height?: number }) {
  return (
    <div style={{ height }} role="img" aria-label={name}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 0, left: -14, bottom: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey={labelKey} minTickGap={24} {...AXIS} />
          <YAxis tickFormatter={(value: number) => num(value)} width={52} allowDecimals={false} {...AXIS} />
          <Tooltip content={<Tip format={String} />} cursor={{ fill: 'rgb(16 24 40 / 0.04)' }} />
          <Bar name={name} dataKey={dataKey} fill={color} radius={[3, 3, 0, 0]} maxBarSize={22} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function Sparkline({ values, color = COLORS.allowed, height = 36 }: { values: number[]; color?: string; height?: number }) {
  const data = values.map((value, index) => ({ index, value }))
  return (
    <div style={{ height }} aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
          <Area type="monotone" dataKey="value" stroke={color} strokeWidth={1.5} fill={color} fillOpacity={0.1} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export function HealthRing({ health, size = 168 }: { health: number | null; size?: number }) {
  const color = TONE_COLOR[healthTone(health)]
  const stroke = 10
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const share = health === null ? 0 : Math.max(0, Math.min(1, health / 100))
  return (
    <div className="relative" style={{ width: size, height: size }} role="img" aria-label={health === null ? 'Health score unavailable' : `Health score ${Math.round(health)} of 100`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={radius} stroke={GRID} strokeWidth={stroke} fill="none" />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={circumference}
          animate={{ strokeDashoffset: circumference * (1 - share) }}
          transition={{ duration: 0.8, ease: 'easeOut' }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-4xl font-semibold tabular-nums tracking-tight">{health === null ? '–' : Math.round(health)}</span>
        <span className="mt-0.5 text-[11px] uppercase tracking-wider text-ink-faint">health</span>
      </div>
    </div>
  )
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** Requests by weekday and hour. Cell opacity scales with volume. */
export function Heatmap({ cells }: { cells: HeatCell[] }) {
  const lookup = new Map(cells.map((cell) => [`${cell.weekday}-${cell.hour}`, cell]))
  const max = Math.max(1, ...cells.map((cell) => cell.requests))
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[640px]">
        <div className="ml-10 grid grid-cols-[repeat(24,minmax(0,1fr))] gap-1 pb-1 text-center text-[10px] text-ink-faint">
          {Array.from({ length: 24 }, (_, hour) => (
            <span key={hour}>{hour % 3 === 0 ? String(hour).padStart(2, '0') : ''}</span>
          ))}
        </div>
        {DAYS.map((day, weekday) => (
          <div key={day} className="mb-1 flex items-center gap-2">
            <span className="w-8 text-right text-[11px] text-ink-faint">{day}</span>
            <div className="grid flex-1 grid-cols-[repeat(24,minmax(0,1fr))] gap-1">
              {Array.from({ length: 24 }, (_, hour) => {
                const cell = lookup.get(`${weekday}-${hour}`)
                const share = cell ? cell.requests / max : 0
                return (
                  <div
                    key={hour}
                    title={`${day} ${String(hour).padStart(2, '0')}:00 · ${num(cell?.requests ?? 0)} requests, ${num(cell?.blocked ?? 0)} blocked`}
                    className="h-6 rounded"
                    style={{ background: share > 0 ? `rgb(37 99 235 / ${0.14 + share * 0.86})` : '#f2f4f7' }}
                  />
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

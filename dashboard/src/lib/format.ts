const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
const plain = new Intl.NumberFormat('en', { maximumFractionDigits: 0 })

export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  if (Math.abs(value) >= 10_000) return compact.format(value)
  if (digits > 0) return value.toFixed(digits)
  return plain.format(value)
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '–'
  return `${(value * 100).toFixed(digits)}%`
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} s`
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount.toFixed(amount >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–'
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`
}

export function windowLabel(seconds: number): string {
  if (seconds === 1) return 'second'
  if (seconds === 60) return 'minute'
  if (seconds === 3600) return 'hour'
  if (seconds === 86400) return 'day'
  return duration(seconds)
}

export function clock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false })
}

export function dateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

export function ago(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return 'never'
  const then = typeof value === 'number' ? value * 1000 : new Date(value).getTime()
  const seconds = Math.max(0, (Date.now() - then) / 1000)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export const ALGORITHM_LABEL: Record<string, string> = {
  token_bucket: 'Token Bucket',
  sliding_window: 'Sliding Window',
  fixed_window: 'Fixed Window',
  leaky_bucket: 'Leaky Bucket',
  adaptive_hybrid: 'Adaptive Hybrid',
}

export const REASON_LABEL: Record<string, string> = {
  ok: 'Allowed',
  limit: 'Over limit',
  paused: 'Provider backoff',
  too_large: 'Cost exceeds capacity',
  rule_allow: 'Allow rule',
  rule_deny: 'Deny rule',
  disabled: 'Policy disabled',
}

export function healthTone(health: number | null | undefined): 'good' | 'warn' | 'bad' | 'idle' {
  if (health === null || health === undefined) return 'idle'
  if (health >= 80) return 'good'
  if (health >= 60) return 'warn'
  return 'bad'
}

export const TONE_COLOR = { good: '#16a34a', warn: '#d97706', bad: '#dc2626', idle: '#98a2b3' } as const

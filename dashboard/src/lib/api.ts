// Typed client for the AdaptiRate REST API.
//
// The access token lives in memory only. The refresh token is an HttpOnly cookie the
// browser sends to /auth/refresh, so nothing readable by script survives a reload.

const BASE = '/api/v1'

export type Role = 'admin' | 'developer' | 'viewer'
export type Algorithm = 'token_bucket' | 'sliding_window' | 'fixed_window' | 'leaky_bucket' | 'adaptive_hybrid'
export type Scope = 'global' | 'user' | 'ip' | 'api_key' | 'role' | 'custom'

export interface User {
  id: string
  email: string
  role: Role
  is_active: boolean
  created_at: string
  last_login_at: string | null
}

export interface Rule {
  attribute: 'user' | 'ip' | 'api_key' | 'role' | 'key'
  values: string[]
  action: 'limit' | 'allow' | 'deny'
  priority: number
  limit: number | null
  window_seconds: number | null
  burst: number | null
}

export interface PolicyInput {
  name: string
  description: string
  enabled: boolean
  algorithm: Algorithm
  limit: number
  window_seconds: number
  burst: number
  priority: number
  scope: Scope
  rules: Rule[]
  adaptive: boolean
  min_limit: number | null
  max_limit: number | null
  target_latency_ms: number | null
  provider: string | null
  provider_dimension: 'requests' | 'tokens'
}

export interface Policy extends PolicyInput {
  id: string
  created_at: string
  updated_at: string
  effective_limit: number | null
  multiplier: number | null
  health: number | null
  paused_ms: number
}

export interface AlgorithmInfo {
  id: Algorithm
  label: string
  summary: string
  best_for: string
  burst: string
  policies: number
  policy_names: string[]
}

export interface ApiKey {
  id: string
  name: string
  prefix: string
  scopes: string[]
  policy_ids: string[]
  created_at: string
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  requests_today: number
  requests_30d: number
}

export interface PolicyState {
  id: string
  name: string
  enabled: boolean
  algorithm: Algorithm
  adaptive: boolean
  base_limit: number
  window_seconds: number
  effective_limit: number
  multiplier: number
  health: number | null
  action: string | null
  penalties: Record<string, number>
  signals: Record<string, number>
  provider: string | null
  provider_cap: number | null
  paused_ms: number
}

export interface ProviderState {
  name: string
  builtin: boolean
  remaining_requests: number | null
  limit_requests: number | null
  reset_requests_in: number | null
  remaining_tokens: number | null
  limit_tokens: number | null
  reset_tokens_in: number | null
  paused_ms: number
  consecutive_throttles: number
  responses: number
  throttled: number
  updated_at: number | null
  custom_headers?: Record<string, string> | null
  policies?: string[]
}

export interface LiveSnapshot {
  ts: number
  requests_per_second: number
  allowed_per_second: number
  blocked_per_second: number
  allowed_last_minute: number
  blocked_last_minute: number
  success_rate: number | null
  health: number | null
  cpu_percent: number | null
  memory_percent: number | null
  latency_p95_ms: number | null
  queue_depth: number | null
  policies: PolicyState[]
  providers: ProviderState[]
}

export interface SeriesPoint {
  ts: number
  allowed: number
  blocked: number
  limit: number | null
  health: number | null
}

export interface DecisionEvent {
  id: string | number
  ts: number | string
  policy: string
  subject: string
  allowed: boolean
  reason: string
  limit: number
  remaining: number
  cost: number
}

export interface Overview {
  hours: number
  allowed: number
  blocked: number
  total: number
  success_rate: number | null
  avg_health: number | null
  peak_minute: string | null
  peak_requests_per_minute: number
  upstream_requests: number
  upstream_error_rate: number | null
  avg_latency_ms: number | null
  worst_p95_latency_ms: number | null
}

export interface AnalyticsPoint {
  ts: number
  allowed: number
  blocked: number
  limit: number | null
  health: number | null
  latency_p95_ms: number | null
  latency_avg_ms: number | null
}

export interface HeatCell {
  weekday: number
  hour: number
  requests: number
  blocked: number
}

export interface TopClient {
  policy: string
  subject: string
  requests: number
}

export interface Check {
  name: string
  ok: boolean
  detail: string
}

export interface Monitoring {
  ts: number
  status: 'ok' | 'degraded'
  checks: Check[]
  host: { cpu_percent: number | null; memory_percent: number | null; load_1m: number | null; cpu_count: number }
  redis: {
    ok: boolean
    latency_ms?: number
    version?: string
    uptime_seconds?: number
    connected_clients?: number
    used_memory_bytes?: number
    ops_per_second?: number
    keys?: number
    cache_hit_rate?: number | null
  }
  postgres: {
    ok: boolean
    latency_ms?: number
    version?: string
    size_bytes?: number
    connections?: number
    max_connections?: number
    rows?: Record<string, number>
  }
  worker: {
    ok: boolean
    last_seen_seconds: number | null
    instance: string | null
    tick_ms: number | null
    logs_written: number | null
  }
  api_processes: {
    instance: string
    last_seen_seconds: number
    started: number
    events_dropped: number
    policies: number
    api_keys: number
  }[]
}

export interface AuditEntry {
  id: number
  ts: string
  actor_type: string
  actor: string
  action: string
  resource_type: string
  resource_id: string | null
  detail: Record<string, unknown>
  ip: string | null
}

export interface RuntimeConfig {
  adaptive: Record<string, number | boolean>
  logging: { log_blocked: boolean; sample_allowed: number; retention_days: number; max_rows: number }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let accessToken: string | null = null
let refreshing: Promise<User | null> | null = null
let onSignedOut: (() => void) | null = null

export function setSignedOutHandler(handler: () => void): void {
  onSignedOut = handler
}

export function getAccessToken(): string | null {
  return accessToken
}

function messageFrom(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc: [...], msg: "..."}]
      return detail
        .map((item: { loc?: unknown[]; msg?: string }) => {
          const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== 'body').join('.') : ''
          return field ? `${field}: ${item.msg}` : (item.msg ?? '')
        })
        .join('; ')
    }
  }
  return fallback
}

/** Exchange the refresh cookie for a new access token. Concurrent callers share one request. */
export function restoreSession(): Promise<User | null> {
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, { method: 'POST', credentials: 'same-origin' })
      if (!response.ok) {
        accessToken = null
        return null
      }
      const body = (await response.json()) as { access_token: string; user: User }
      accessToken = body.access_token
      return body.user
    } catch {
      return null
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

export async function request<T>(path: string, init: RequestInit & { json?: unknown } = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers)
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  let body = init.body
  if (init.json !== undefined) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(init.json)
  }
  const response = await fetch(BASE + path, { ...init, headers, body, credentials: 'same-origin' })

  if (response.status === 401 && retry && !path.startsWith('/auth/')) {
    // The access token expired: refresh once and replay the request.
    if (await restoreSession()) return request<T>(path, init, false)
    onSignedOut?.()
    throw new ApiError(401, 'Your session has expired. Sign in again.')
  }
  if (response.status === 204) return undefined as T
  const parsed: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, messageFrom(parsed, `Request failed (${response.status})`))
  return parsed as T
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export interface LogFilters {
  q?: string
  policy?: string
  allowed?: boolean | null
  reason?: string
  since?: string
  until?: string
}

export const api = {
  async login(email: string, password: string): Promise<User> {
    const body = await request<{ access_token: string; user: User }>('/auth/login', {
      method: 'POST',
      json: { email, password },
    })
    accessToken = body.access_token
    return body.user
  },
  async logout(): Promise<void> {
    await request<void>('/auth/logout', { method: 'POST' }).catch(() => undefined)
    accessToken = null
  },

  policies: () => request<Policy[]>('/policies'),
  createPolicy: (body: PolicyInput) => request<Policy>('/policies', { method: 'POST', json: body }),
  updatePolicy: (id: string, body: Partial<PolicyInput>) =>
    request<Policy>(`/policies/${id}`, { method: 'PATCH', json: body }),
  deletePolicy: (id: string) => request<void>(`/policies/${id}`, { method: 'DELETE' }),
  resetPolicy: (id: string) => request<{ keys_removed: number }>(`/policies/${id}/reset`, { method: 'POST' }),
  algorithms: () => request<AlgorithmInfo[]>('/algorithms'),

  keys: () => request<ApiKey[]>('/keys'),
  createKey: (body: { name: string; scopes: string[]; policy_ids: string[]; expires_at: string | null }) =>
    request<ApiKey & { secret: string }>('/keys', { method: 'POST', json: body }),
  revokeKey: (id: string) => request<ApiKey>(`/keys/${id}/revoke`, { method: 'POST' }),
  keyUsage: (id: string) =>
    request<{ total: number; daily: { date: string; requests: number }[] }>(`/keys/${id}/usage`),

  live: () => request<LiveSnapshot>('/metrics/live'),
  series: (seconds: number, step: number, policy?: string) =>
    request<{ points: SeriesPoint[] }>(`/metrics/series${query({ seconds, step, policy })}`),

  overview: (hours: number, policy?: string) => request<Overview>(`/analytics/overview${query({ hours, policy })}`),
  timeseries: (hours: number, policy?: string) =>
    request<{ bucket_seconds: number; points: AnalyticsPoint[] }>(`/analytics/timeseries${query({ hours, policy })}`),
  heatmap: (days: number, policy?: string) =>
    request<{ cells: HeatCell[]; peak_hours: number[] }>(
      `/analytics/heatmap${query({ days, policy, tz_offset_minutes: -new Date().getTimezoneOffset() })}`,
    ),
  top: (hours: number) => request<{ allowed: TopClient[]; blocked: TopClient[] }>(`/analytics/top${query({ hours })}`),

  monitoring: () => request<Monitoring>('/monitoring'),

  logs: (filters: LogFilters, cursor?: number | null) =>
    request<{ items: DecisionEvent[]; next_cursor: number | null }>(
      `/logs${query({ ...filters, cursor, limit: 100 })}`,
    ),
  async exportLogs(filters: LogFilters, format: 'csv' | 'ndjson'): Promise<Blob> {
    const response = await fetch(`${BASE}/logs/export${query({ ...filters, format })}`, {
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      credentials: 'same-origin',
    })
    if (!response.ok) throw new ApiError(response.status, 'Export failed')
    return response.blob()
  },
  audit: (cursor?: number | null) =>
    request<{ items: AuditEntry[]; next_cursor: number | null }>(`/audit${query({ cursor, limit: 50 })}`),

  providers: () => request<ProviderState[]>('/providers'),
  resetProvider: (name: string) => request<void>(`/providers/${name}`, { method: 'DELETE' }),

  config: () => request<RuntimeConfig>('/config'),
  saveConfig: (body: RuntimeConfig) => request<RuntimeConfig>('/config', { method: 'PUT', json: body }),

  users: () => request<User[]>('/users'),
  createUser: (body: { email: string; password: string; role: Role }) =>
    request<User>('/users', { method: 'POST', json: body }),
  updateUser: (id: string, body: { role?: Role; is_active?: boolean; password?: string }) =>
    request<User>(`/users/${id}`, { method: 'PATCH', json: body }),
  deleteUser: (id: string) => request<void>(`/users/${id}`, { method: 'DELETE' }),
}

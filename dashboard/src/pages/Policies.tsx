import { Pencil, Plus, RotateCcw, ShieldCheck, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Badge, Button, Card, Confirm, Empty, ErrorNote, Field, Input, Meter, Modal, PageHeader, Select, Spinner, Toggle, useToast } from '../components/ui'
import { api } from '../lib/api'
import type { Algorithm, Policy, PolicyInput, Rule, Scope } from '../lib/api'
import { useAuth } from '../lib/auth'
import { ALGORITHM_LABEL, duration, healthTone, num, windowLabel } from '../lib/format'
import { useLoad } from '../lib/hooks'

const SCOPE_LABEL: Record<Scope, string> = {
  custom: 'Custom key',
  user: 'Per user',
  ip: 'Per IP address',
  api_key: 'Per API key',
  role: 'Per role',
  global: 'Global (one shared bucket)',
}

const BLANK: PolicyInput = {
  name: '',
  description: '',
  enabled: true,
  algorithm: 'token_bucket',
  limit: 100,
  window_seconds: 60,
  burst: 0,
  priority: 50,
  scope: 'user',
  rules: [],
  adaptive: true,
  min_limit: null,
  max_limit: null,
  target_latency_ms: null,
  provider: null,
  provider_dimension: 'requests',
}

const BLANK_RULE: Rule = { attribute: 'role', values: [], action: 'limit', priority: 0, limit: 100, window_seconds: null, burst: null }

function optionalNumber(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isFinite(value) ? value : null
}

function RuleEditor({ rule, onChange, onRemove }: { rule: Rule; onChange: (next: Rule) => void; onRemove: () => void }) {
  return (
    <div className="rounded-lg border border-line bg-canvas p-3">
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_2fr_auto]">
        <Select aria-label="Attribute" value={rule.attribute} onChange={(event) => onChange({ ...rule, attribute: event.target.value as Rule['attribute'] })}>
          <option value="role">Role</option>
          <option value="user">User</option>
          <option value="ip">IP / CIDR</option>
          <option value="api_key">API key</option>
          <option value="key">Custom key</option>
        </Select>
        <Select aria-label="Action" value={rule.action} onChange={(event) => onChange({ ...rule, action: event.target.value as Rule['action'] })}>
          <option value="limit">Set limit</option>
          <option value="allow">Always allow</option>
          <option value="deny">Always deny</option>
        </Select>
        <Input
          aria-label="Values"
          placeholder={rule.attribute === 'ip' ? '10.0.0.0/8, 203.0.113.7' : 'premium, enterprise'}
          value={rule.values.join(', ')}
          onChange={(event) => onChange({ ...rule, values: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })}
        />
        <Button variant="ghost" size="sm" aria-label="Remove rule" onClick={onRemove} className="h-9.5">
          <Trash2 size={15} />
        </Button>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {rule.action === 'limit' && (
          <>
            <Input aria-label="Rule limit" type="number" min={1} placeholder="Limit" value={rule.limit ?? ''} onChange={(event) => onChange({ ...rule, limit: optionalNumber(event.target.value) })} />
            <Input aria-label="Rule window seconds" type="number" min={0.001} step="any" placeholder="Window (s), optional" value={rule.window_seconds ?? ''} onChange={(event) => onChange({ ...rule, window_seconds: optionalNumber(event.target.value) })} />
            <Input aria-label="Rule burst" type="number" min={0} placeholder="Burst, optional" value={rule.burst ?? ''} onChange={(event) => onChange({ ...rule, burst: optionalNumber(event.target.value) })} />
          </>
        )}
        <Input aria-label="Rule priority" type="number" placeholder="Priority" title="Higher priority rules are checked first" value={rule.priority} onChange={(event) => onChange({ ...rule, priority: Number(event.target.value) || 0 })} />
      </div>
    </div>
  )
}

function PolicyForm({ initial, onSaved, onClose }: { initial: Policy | null; onSaved: () => void; onClose: () => void }) {
  const toast = useToast()
  const [form, setForm] = useState<PolicyInput>(BLANK)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (initial) {
      const { id: _id, created_at: _c, updated_at: _u, effective_limit: _e, multiplier: _m, health: _h, paused_ms: _p, ...rest } = initial
      setForm(rest)
    } else {
      setForm(BLANK)
    }
    setError(null)
  }, [initial])

  const set = <K extends keyof PolicyInput>(key: K, value: PolicyInput[K]) => setForm((current) => ({ ...current, [key]: value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (form.rules.some((rule) => rule.values.length === 0)) {
      setError('Every rule needs at least one value.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (initial) {
        const { name: _name, ...changes } = form
        await api.updatePolicy(initial.id, changes)
        toast.success(`Updated ${initial.name}`)
      } else {
        await api.createPolicy(form)
        toast.success(`Created ${form.name}`)
      }
      onSaved()
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not save the policy')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      {error && <ErrorNote message={error} />}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" hint={initial ? 'SDKs refer to the policy by this name; it cannot be changed.' : 'Letters, digits, dot, dash, underscore, colon.'}>
          {(id) => <Input id={id} required disabled={!!initial} pattern="[\w.:\-]+" maxLength={100} value={form.name} onChange={(event) => set('name', event.target.value)} />}
        </Field>
        <Field label="Description">{(id) => <Input id={id} maxLength={2000} value={form.description} onChange={(event) => set('description', event.target.value)} />}</Field>
      </div>

      <div className="grid gap-4 sm:grid-cols-4">
        <Field label="Limit">{(id) => <Input id={id} type="number" required min={1} value={form.limit} onChange={(event) => set('limit', Number(event.target.value))} />}</Field>
        <Field label="Window (seconds)">{(id) => <Input id={id} type="number" required min={0.001} step="any" value={form.window_seconds} onChange={(event) => set('window_seconds', Number(event.target.value))} />}</Field>
        <Field label="Burst">{(id) => <Input id={id} type="number" min={0} value={form.burst} onChange={(event) => set('burst', Number(event.target.value))} />}</Field>
        <Field label="Priority" hint="0–100. Higher sheds less.">
          {(id) => <Input id={id} type="number" min={0} max={100} value={form.priority} onChange={(event) => set('priority', Number(event.target.value))} />}
        </Field>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Algorithm">
          {(id) => (
            <Select id={id} value={form.algorithm} onChange={(event) => set('algorithm', event.target.value as Algorithm)}>
              {Object.entries(ALGORITHM_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Limit applies" hint="Which request attribute identifies a bucket.">
          {(id) => (
            <Select id={id} value={form.scope} onChange={(event) => set('scope', event.target.value as Scope)}>
              {Object.entries(SCOPE_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </div>

      <fieldset className="rounded-xl border border-line p-4">
        <legend className="flex items-center gap-3 px-2 text-sm font-medium">
          Adaptive limiting
          <Toggle label="Adaptive limiting" checked={form.adaptive} onChange={(next) => set('adaptive', next)} />
        </legend>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Minimum limit" hint="Default: 10% of the limit.">
            {(id) => <Input id={id} type="number" min={1} disabled={!form.adaptive} value={form.min_limit ?? ''} onChange={(event) => set('min_limit', optionalNumber(event.target.value))} />}
          </Field>
          <Field label="Maximum limit" hint="Default: the limit itself.">
            {(id) => <Input id={id} type="number" min={1} disabled={!form.adaptive} value={form.max_limit ?? ''} onChange={(event) => set('max_limit', optionalNumber(event.target.value))} />}
          </Field>
          <Field label="Target p95 latency (ms)" hint="Default: the global setting.">
            {(id) => <Input id={id} type="number" min={1} disabled={!form.adaptive} value={form.target_latency_ms ?? ''} onChange={(event) => set('target_latency_ms', optionalNumber(event.target.value))} />}
          </Field>
        </div>
      </fieldset>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Upstream provider" hint="Pace against this provider's quota and pause on its 429s.">
          {(id) => <Input id={id} placeholder="openai, anthropic, groq, github, gemini…" pattern="[a-z0-9_\-]*" value={form.provider ?? ''} onChange={(event) => set('provider', event.target.value.trim() || null)} />}
        </Field>
        <Field label="Quota counted in">
          {(id) => (
            <Select id={id} value={form.provider_dimension} disabled={!form.provider} onChange={(event) => set('provider_dimension', event.target.value as 'requests' | 'tokens')}>
              <option value="requests">Requests</option>
              <option value="tokens">Tokens</option>
            </Select>
          )}
        </Field>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">Rules</p>
            <p className="text-xs text-ink-faint">Override the limit, or always allow or deny, for matching requests.</p>
          </div>
          <Button size="sm" onClick={() => set('rules', [...form.rules, { ...BLANK_RULE }])}>
            <Plus size={14} /> Add rule
          </Button>
        </div>
        <div className="space-y-2">
          {form.rules.map((rule, index) => (
            <RuleEditor
              key={index}
              rule={rule}
              onChange={(next) => set('rules', form.rules.map((existing, position) => (position === index ? next : existing)))}
              onRemove={() => set('rules', form.rules.filter((_, position) => position !== index))}
            />
          ))}
        </div>
      </div>

      <div className="flex justify-end gap-2 border-t border-line pt-4">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" busy={busy}>
          {initial ? 'Save changes' : 'Create policy'}
        </Button>
      </div>
    </form>
  )
}

function PolicyCard({ policy, canManage, onEdit, onChanged }: { policy: Policy; canManage: boolean; onEdit: () => void; onChanged: () => void }) {
  const toast = useToast()
  const [confirm, setConfirm] = useState<'delete' | 'reset' | null>(null)
  const effective = policy.effective_limit ?? policy.limit
  const share = effective / policy.limit
  const tone = healthTone(policy.health)

  async function toggle(enabled: boolean) {
    try {
      await api.updatePolicy(policy.id, { enabled })
      toast.success(`${policy.name} ${enabled ? 'enabled' : 'disabled'}`)
      onChanged()
    } catch (reason) {
      toast.error(reason)
    }
  }

  return (
    <Card className={policy.enabled ? '' : 'opacity-70'}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-base font-semibold">{policy.name}</h3>
            <Badge tone="accent">{ALGORITHM_LABEL[policy.algorithm]}</Badge>
            <Badge tone="idle">{SCOPE_LABEL[policy.scope]}</Badge>
            {policy.adaptive && <Badge tone="violet">Adaptive</Badge>}
            {policy.provider && <Badge tone="idle">{policy.provider}</Badge>}
            {policy.paused_ms > 0 && <Badge tone="bad">Paused {duration(policy.paused_ms / 1000)}</Badge>}
          </div>
          {policy.description && <p className="mt-1 truncate text-sm text-ink-soft">{policy.description}</p>}
        </div>
        <Toggle label={`Enable ${policy.name}`} checked={policy.enabled} disabled={!canManage} onChange={(next) => void toggle(next)} />
      </div>

      <div className="mt-4 flex items-end justify-between">
        <p className="text-3xl font-semibold tracking-tight tabular-nums">
          {num(effective)}
          <span className="ml-1.5 text-sm font-normal text-ink-faint">
            {share < 0.999 || share > 1.001 ? `of ${num(policy.limit)} ` : ''}per {windowLabel(policy.window_seconds)}
          </span>
        </p>
        {policy.health !== null && <Badge tone={tone === 'idle' ? 'idle' : tone}>Health {Math.round(policy.health)}</Badge>}
      </div>
      <div className="mt-2">
        <Meter value={effective} max={Math.max(policy.limit, effective)} tone={share < 0.6 ? 'bad' : share < 0.999 ? 'warn' : 'accent'} />
      </div>

      <dl className="mt-4 grid grid-cols-4 gap-2 text-xs">
        {[
          ['Burst', num(policy.burst)],
          ['Priority', String(policy.priority)],
          ['Rules', String(policy.rules.length)],
          ['Floor', policy.adaptive ? num(policy.min_limit ?? Math.max(1, Math.floor(policy.limit / 10))) : '–'],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-line bg-canvas px-2.5 py-2">
            <dt className="text-ink-faint">{label}</dt>
            <dd className="mt-0.5 font-medium tabular-nums text-ink">{value}</dd>
          </div>
        ))}
      </dl>

      {canManage && (
        <div className="mt-4 flex gap-2 border-t border-line pt-3">
          <Button size="sm" onClick={onEdit}>
            <Pencil size={13} /> Edit
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirm('reset')}>
            <RotateCcw size={13} /> Reset counters
          </Button>
          <Button size="sm" variant="ghost" className="ml-auto text-bad hover:text-bad" onClick={() => setConfirm('delete')}>
            <Trash2 size={13} /> Delete
          </Button>
        </div>
      )}

      <Confirm
        open={confirm === 'delete'}
        title={`Delete ${policy.name}?`}
        body="SDKs that reference this policy will start receiving errors. This cannot be undone."
        confirmLabel="Delete policy"
        onClose={() => setConfirm(null)}
        onConfirm={async () => {
          try {
            await api.deletePolicy(policy.id)
            toast.success(`Deleted ${policy.name}`)
            onChanged()
          } catch (reason) {
            toast.error(reason)
          }
        }}
      />
      <Confirm
        open={confirm === 'reset'}
        title={`Reset ${policy.name}?`}
        body="Every client's usage for this policy is cleared and any provider pause is lifted. Clients that were blocked can send again immediately."
        confirmLabel="Reset counters"
        onClose={() => setConfirm(null)}
        onConfirm={async () => {
          try {
            const result = await api.resetPolicy(policy.id)
            toast.success(`Cleared ${result.keys_removed} buckets`)
            onChanged()
          } catch (reason) {
            toast.error(reason)
          }
        }}
      />
    </Card>
  )
}

export function PoliciesPage() {
  const { canManage } = useAuth()
  const { data, error, loading, reload } = useLoad(api.policies, [], 3000)
  const [editing, setEditing] = useState<Policy | 'new' | null>(null)

  return (
    <>
      <PageHeader
        title="Policies"
        subtitle="Limits, who they apply to, and how they adapt"
        action={
          canManage && (
            <Button variant="primary" onClick={() => setEditing('new')}>
              <Plus size={16} /> New policy
            </Button>
          )
        }
      />
      {error && <ErrorNote message={error} onRetry={reload} />}
      {loading && !data ? (
        <Spinner />
      ) : data && data.length === 0 ? (
        <Card>
          <Empty
            icon={<ShieldCheck size={22} />}
            title="No policies yet"
            action={
              canManage && (
                <Button variant="primary" onClick={() => setEditing('new')}>
                  <Plus size={16} /> New policy
                </Button>
              )
            }
          >
            A policy names a limit. Your services ask AdaptiRate about it on each request.
          </Empty>
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {data?.map((policy) => (
            <PolicyCard key={policy.id} policy={policy} canManage={canManage} onEdit={() => setEditing(policy)} onChanged={reload} />
          ))}
        </div>
      )}

      <Modal open={editing !== null} onClose={() => setEditing(null)} title={editing === 'new' ? 'New policy' : `Edit ${editing?.name ?? ''}`} wide>
        <PolicyForm initial={editing === 'new' ? null : editing} onSaved={reload} onClose={() => setEditing(null)} />
      </Modal>
    </>
  )
}

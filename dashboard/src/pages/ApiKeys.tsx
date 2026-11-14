import { Check, Copy, KeyRound, Plus, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Bars } from '../components/charts'
import { Badge, Button, Card, Confirm, Empty, ErrorNote, Field, Input, Modal, PageHeader, Spinner, useToast } from '../components/ui'
import { api } from '../lib/api'
import type { ApiKey } from '../lib/api'
import { useAuth } from '../lib/auth'
import { ago, dateTime, num } from '../lib/format'
import { useLoad } from '../lib/hooks'

const SCOPES = [
  { id: 'evaluate', label: 'Evaluate', hint: 'Ask for rate-limit decisions and leases' },
  { id: 'report', label: 'Report', hint: 'Send latency, error and provider signals' },
  { id: 'read', label: 'Read', hint: 'View policies, metrics and logs' },
  { id: 'manage', label: 'Manage', hint: 'Create and change policies and keys' },
]

function status(key: ApiKey): { label: string; tone: 'good' | 'bad' | 'warn' } {
  if (key.revoked_at) return { label: 'Revoked', tone: 'bad' }
  if (key.expires_at && new Date(key.expires_at).getTime() <= Date.now()) return { label: 'Expired', tone: 'warn' }
  return { label: 'Active', tone: 'good' }
}

function CreateKey({ onCreated, onClose }: { onCreated: (secret: string) => void; onClose: () => void }) {
  const { user } = useAuth()
  const policies = useLoad(api.policies, [])
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>(['evaluate', 'report'])
  const [policyIds, setPolicyIds] = useState<string[]>([])
  const [expires, setExpires] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const toggle = (list: string[], value: string) => (list.includes(value) ? list.filter((item) => item !== value) : [...list, value])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (scopes.length === 0) {
      setError('Choose at least one permission.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await api.createKey({ name: name.trim(), scopes, policy_ids: policyIds, expires_at: expires ? new Date(expires).toISOString() : null })
      onCreated(created.secret)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not create the key')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      {error && <ErrorNote message={error} />}
      <Field label="Name" hint="Where this key will be used, for example 'checkout-service production'.">
        {(id) => <Input id={id} required maxLength={100} autoFocus value={name} onChange={(event) => setName(event.target.value)} />}
      </Field>

      <fieldset>
        <legend className="mb-2 text-xs font-medium text-ink-soft">Permissions</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {SCOPES.filter((scope) => scope.id !== 'manage' || user?.role !== 'viewer').map((scope) => (
            <label key={scope.id} className="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-canvas p-3 hover:border-line-strong">
              <input type="checkbox" className="mt-0.5 accent-accent" checked={scopes.includes(scope.id)} onChange={() => setScopes((current) => toggle(current, scope.id))} />
              <span>
                <span className="block text-sm font-medium">{scope.label}</span>
                <span className="block text-xs text-ink-faint">{scope.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {policies.data && policies.data.length > 0 && (
        <fieldset>
          <legend className="mb-2 text-xs font-medium text-ink-soft">Restrict to policies (none selected means all)</legend>
          <div className="flex flex-wrap gap-2">
            {policies.data.map((policy) => {
              const selected = policyIds.includes(policy.id)
              return (
                <button
                  key={policy.id}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => setPolicyIds((current) => toggle(current, policy.id))}
                  className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${selected ? 'border-accent bg-accent/8 text-accent-strong' : 'border-line text-ink-soft hover:border-line-strong'}`}
                >
                  {policy.name}
                </button>
              )
            })}
          </div>
        </fieldset>
      )}

      <Field label="Expires" hint="Leave empty for a key that does not expire.">
        {(id) => <Input id={id} type="datetime-local" value={expires} onChange={(event) => setExpires(event.target.value)} />}
      </Field>

      <div className="flex justify-end gap-2 border-t border-line pt-4">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" busy={busy}>
          Generate key
        </Button>
      </div>
    </form>
  )
}

function Secret({ secret, onClose }: { secret: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="space-y-4">
      <div className="flex gap-3 rounded-lg border border-warn/25 bg-warn/5 p-3 text-sm text-warn">
        <ShieldAlert size={18} className="mt-0.5 shrink-0" />
        <p>Copy this key now. Only a hash is stored, so it cannot be shown again.</p>
      </div>
      <div className="flex items-center gap-2 rounded-lg border border-line bg-canvas p-3">
        <code className="min-w-0 flex-1 break-all font-mono text-sm text-ink">{secret}</code>
        <Button
          size="sm"
          onClick={async () => {
            await navigator.clipboard.writeText(secret)
            setCopied(true)
          }}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <pre className="overflow-x-auto rounded-lg border border-line bg-canvas p-3 font-mono text-xs text-ink-soft">{`export ADAPTIRATE_API_KEY=${secret}\nexport ADAPTIRATE_URL=${window.location.origin}`}</pre>
      <div className="flex justify-end">
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      </div>
    </div>
  )
}

function Usage({ apiKey }: { apiKey: ApiKey }) {
  const { data, error } = useLoad(() => api.keyUsage(apiKey.id), [apiKey.id])
  if (error) return <ErrorNote message={error} />
  if (!data) return <Spinner />
  return (
    <div>
      <p className="mb-3 text-sm text-ink-soft">
        <span className="text-2xl font-semibold tabular-nums text-ink">{num(data.total)}</span> requests in the last 30 days
      </p>
      <Bars data={data.daily.map((day) => ({ ...day, label: day.date.slice(5) }))} dataKey="requests" labelKey="label" name="Requests" height={200} />
    </div>
  )
}

export function ApiKeysPage() {
  const { canManage } = useAuth()
  const toast = useToast()
  const { data, error, loading, reload } = useLoad(api.keys, [], 10000)
  const [creating, setCreating] = useState(false)
  const [secret, setSecret] = useState<string | null>(null)
  const [revoking, setRevoking] = useState<ApiKey | null>(null)
  const [inspecting, setInspecting] = useState<ApiKey | null>(null)

  return (
    <>
      <PageHeader
        title="API Keys"
        subtitle="Credentials your services use to talk to AdaptiRate"
        action={
          canManage && (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus size={16} /> Generate key
            </Button>
          )
        }
      />
      {error && <ErrorNote message={error} onRetry={reload} />}
      {loading && !data ? (
        <Spinner />
      ) : data && data.length === 0 ? (
        <Card>
          <Empty icon={<KeyRound size={22} />} title="No API keys yet">
            Generate a key and give it to the SDK in your service.
          </Empty>
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {data?.map((key) => {
            const state = status(key)
            return (
              <Card key={key.id} className={state.tone === 'good' ? '' : 'opacity-65'}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="truncate font-semibold">{key.name}</h3>
                    <code className="font-mono text-xs text-ink-faint">{key.prefix}…</code>
                  </div>
                  <Badge tone={state.tone}>{state.label}</Badge>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {key.scopes.map((scope) => (
                    <Badge key={scope} tone={scope === 'manage' ? 'warn' : 'accent'}>
                      {scope}
                    </Badge>
                  ))}
                  {key.policy_ids.length > 0 && <Badge tone="idle">{key.policy_ids.length} policies only</Badge>}
                </div>
                <dl className="mt-4 grid grid-cols-2 gap-2 text-xs">
                  {[
                    ['Today', num(key.requests_today)],
                    ['30 days', num(key.requests_30d)],
                    ['Last used', ago(key.last_used_at)],
                    ['Expires', key.expires_at ? dateTime(key.expires_at) : 'Never'],
                  ].map(([label, value]) => (
                    <div key={label} className="rounded-lg border border-line bg-canvas px-2.5 py-2">
                      <dt className="text-ink-faint">{label}</dt>
                      <dd className="mt-0.5 font-medium tabular-nums text-ink">{value}</dd>
                    </div>
                  ))}
                </dl>
                <div className="mt-4 flex gap-2 border-t border-line pt-3">
                  <Button size="sm" onClick={() => setInspecting(key)}>
                    Usage
                  </Button>
                  {canManage && !key.revoked_at && (
                    <Button size="sm" variant="ghost" className="ml-auto text-bad hover:text-bad" onClick={() => setRevoking(key)}>
                      Revoke
                    </Button>
                  )}
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <Modal open={creating} onClose={() => setCreating(false)} title="Generate API key">
        <CreateKey
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false)
            setSecret(created)
            reload()
          }}
        />
      </Modal>
      <Modal open={secret !== null} onClose={() => setSecret(null)} title="Your new API key">
        {secret && <Secret secret={secret} onClose={() => setSecret(null)} />}
      </Modal>
      <Modal open={inspecting !== null} onClose={() => setInspecting(null)} title={`Usage · ${inspecting?.name ?? ''}`} wide>
        {inspecting && <Usage apiKey={inspecting} />}
      </Modal>
      <Confirm
        open={revoking !== null}
        title={`Revoke ${revoking?.name ?? ''}?`}
        body="Services using this key will be rejected within seconds. This cannot be undone."
        confirmLabel="Revoke key"
        onClose={() => setRevoking(null)}
        onConfirm={async () => {
          if (!revoking) return
          try {
            await api.revokeKey(revoking.id)
            toast.success(`Revoked ${revoking.name}`)
            reload()
          } catch (reason) {
            toast.error(reason)
          }
        }}
      />
    </>
  )
}

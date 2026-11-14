import { Lock, Plus, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Badge, Button, Card, CardTitle, Confirm, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner, Toggle, useToast } from '../components/ui'
import { api } from '../lib/api'
import type { Role, RuntimeConfig, User } from '../lib/api'
import { useAuth } from '../lib/auth'
import { ago, dateTime } from '../lib/format'
import { useLoad } from '../lib/hooks'

interface Knob {
  key: string
  label: string
  hint: string
  step?: number
}

const ADAPTIVE_KNOBS: { title: string; knobs: Knob[] }[] = [
  {
    title: 'Control loop',
    knobs: [
      { key: 'signal_window_seconds', label: 'Signal window (s)', hint: 'How much recent history each decision looks at.' },
      { key: 'decrease_threshold', label: 'Decrease below health', hint: 'Limits are cut when health falls under this.' },
      { key: 'increase_threshold', label: 'Increase above health', hint: 'Limits grow when health is at least this.' },
      { key: 'max_decrease', label: 'Largest cut', hint: 'Share of the limit removed in one step at health 0.', step: 0.05 },
      { key: 'increase_step', label: 'Recovery step', hint: 'Share of the base limit added per second when healthy.', step: 0.01 },
      { key: 'decrease_cooldown_seconds', label: 'Cooldown between cuts (s)', hint: 'Time to let a cut take effect before cutting again.', step: 0.5 },
    ],
  },
  {
    title: 'Latency and errors',
    knobs: [
      { key: 'default_target_latency_ms', label: 'Default target p95 (ms)', hint: 'Used by policies without their own target.' },
      { key: 'latency_critical_factor', label: 'Critical latency factor', hint: 'Latency this many times the target scores zero.', step: 0.5 },
      { key: 'error_rate_ok', label: 'Acceptable error rate', hint: 'Error share that costs nothing.', step: 0.01 },
      { key: 'error_rate_critical', label: 'Critical error rate', hint: 'Error share that scores zero.', step: 0.01 },
      { key: 'throttle_rate_critical', label: 'Critical upstream 429 rate', hint: 'Share of upstream 429s that scores zero.', step: 0.01 },
      { key: 'provider_safety_margin', label: 'Provider quota held back', hint: 'Share of remaining provider quota left unspent.', step: 0.05 },
    ],
  },
  {
    title: 'Resources',
    knobs: [
      { key: 'cpu_ok', label: 'CPU acceptable (%)', hint: 'Below this, CPU costs nothing.' },
      { key: 'cpu_critical', label: 'CPU critical (%)', hint: 'At or above this, CPU scores zero.' },
      { key: 'memory_ok', label: 'Memory acceptable (%)', hint: 'Below this, memory costs nothing.' },
      { key: 'memory_critical', label: 'Memory critical (%)', hint: 'At or above this, memory scores zero.' },
      { key: 'queue_ok', label: 'Queue acceptable', hint: 'Queued requests that cost nothing.' },
      { key: 'queue_critical', label: 'Queue critical', hint: 'Queued requests that score zero.' },
    ],
  },
]

function ConfigEditor({ editable }: { editable: boolean }) {
  const toast = useToast()
  const { data, error, reload } = useLoad(api.config, [])
  const [draft, setDraft] = useState<RuntimeConfig | null>(null)
  const [busy, setBusy] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    if (data) setDraft(structuredClone(data))
  }, [data])

  if (error) return <ErrorNote message={error} onRetry={reload} />
  if (!draft || !data) return <Spinner />
  const dirty = JSON.stringify(draft) !== JSON.stringify(data)

  const setAdaptive = (key: string, value: number | boolean) => setDraft({ ...draft, adaptive: { ...draft.adaptive, [key]: value } })

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    setBusy(true)
    setSaveError(null)
    try {
      await api.saveConfig(draft)
      toast.success('Settings saved. They apply within a second.')
      reload()
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : 'Could not save settings')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={save} className="space-y-4">
      {saveError && <ErrorNote message={saveError} />}
      <Card>
        <CardTitle hint="Applies to every policy with adaptive limiting on" action={<Toggle label="Adaptive engine" checked={draft.adaptive.enabled === true} disabled={!editable} onChange={(next) => setAdaptive('enabled', next)} />}>
          Adaptive engine
        </CardTitle>
        <div className="space-y-6">
          {ADAPTIVE_KNOBS.map((group) => (
            <div key={group.title}>
              <p className="mb-3 text-xs font-medium uppercase tracking-wider text-ink-faint">{group.title}</p>
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {group.knobs.map((knob) => (
                  <Field key={knob.key} label={knob.label} hint={knob.hint}>
                    {(id) => <Input id={id} type="number" step={knob.step ?? 1} min={0} disabled={!editable} value={Number(draft.adaptive[knob.key] ?? 0)} onChange={(event) => setAdaptive(knob.key, Number(event.target.value))} />}
                  </Field>
                ))}
              </div>
            </div>
          ))}
          <label className="flex items-center gap-3 text-sm">
            <Toggle label="Use host signals" checked={draft.adaptive.use_host_signals === true} disabled={!editable} onChange={(next) => setAdaptive('use_host_signals', next)} />
            <span>
              Use this host's CPU and memory when SDKs do not report their own
              <span className="block text-xs text-ink-faint">Turn off if AdaptiRate runs on a different machine from the services it protects.</span>
            </span>
          </label>
        </div>
      </Card>

      <Card>
        <CardTitle hint="What is written to the searchable decision log. Counters and analytics stay exact either way.">Decision log</CardTitle>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <label className="flex items-center gap-3 text-sm">
            <Toggle label="Log blocked requests" checked={draft.logging.log_blocked} disabled={!editable} onChange={(next) => setDraft({ ...draft, logging: { ...draft.logging, log_blocked: next } })} />
            Log blocked requests
          </label>
          <Field label="Share of allowed requests logged" hint="1 logs all, 0.01 logs one in a hundred.">
            {(id) => <Input id={id} type="number" min={0} max={1} step={0.01} disabled={!editable} value={draft.logging.sample_allowed} onChange={(event) => setDraft({ ...draft, logging: { ...draft.logging, sample_allowed: Number(event.target.value) } })} />}
          </Field>
          <Field label="Most rows kept" hint="Oldest rows are removed first.">
            {(id) => <Input id={id} type="number" min={1000} step={1000} disabled={!editable} value={draft.logging.max_rows} onChange={(event) => setDraft({ ...draft, logging: { ...draft.logging, max_rows: Number(event.target.value) } })} />}
          </Field>
          <Field label="Keep logs for (days)">
            {(id) => <Input id={id} type="number" min={1} max={365} disabled={!editable} value={draft.logging.retention_days} onChange={(event) => setDraft({ ...draft, logging: { ...draft.logging, retention_days: Number(event.target.value) } })} />}
          </Field>
        </div>
      </Card>

      {editable ? (
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={!dirty} onClick={() => setDraft(structuredClone(data))}>
            Discard changes
          </Button>
          <Button type="submit" variant="primary" busy={busy} disabled={!dirty}>
            Save settings
          </Button>
        </div>
      ) : (
        <p className="flex items-center gap-2 text-xs text-ink-faint">
          <Lock size={13} /> Only admins can change these settings.
        </p>
      )}
    </form>
  )
}

function Users() {
  const toast = useToast()
  const { user: me } = useAuth()
  const { data, error, reload } = useLoad(api.users, [])
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<User | null>(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function change(user: User, body: { role?: Role; is_active?: boolean }) {
    try {
      await api.updateUser(user.id, body)
      toast.success(`Updated ${user.email}`)
      reload()
    } catch (reason) {
      toast.error(reason)
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setFormError(null)
    try {
      await api.createUser({ email: email.trim(), password, role })
      toast.success(`Added ${email.trim()}`)
      setAdding(false)
      setEmail('')
      setPassword('')
      setRole('viewer')
      reload()
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : 'Could not add the user')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardTitle
        hint="Admins manage everything. Developers manage policies and keys. Viewers can only look."
        action={
          <Button size="sm" onClick={() => setAdding(true)}>
            <Plus size={14} /> Add user
          </Button>
        }
      >
        Users
      </CardTitle>
      {error && <ErrorNote message={error} onRetry={reload} />}
      {!data ? (
        <Spinner />
      ) : (
        <ul className="space-y-2">
          {data.map((user) => (
            <li key={user.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-canvas px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">
                  {user.email} {user.id === me?.id && <Badge tone="accent">You</Badge>}
                </p>
                <p className="text-xs text-ink-faint">Last sign-in {ago(user.last_login_at)}</p>
              </div>
              <Select aria-label={`Role for ${user.email}`} className="w-36" value={user.role} onChange={(event) => void change(user, { role: event.target.value as Role })}>
                <option value="admin">Admin</option>
                <option value="developer">Developer</option>
                <option value="viewer">Viewer</option>
              </Select>
              <Toggle label={`${user.email} active`} checked={user.is_active} onChange={(next) => void change(user, { is_active: next })} />
              <Button size="sm" variant="ghost" aria-label={`Delete ${user.email}`} disabled={user.id === me?.id} className="text-bad hover:text-bad" onClick={() => setRemoving(user)}>
                <Trash2 size={14} />
              </Button>
            </li>
          ))}
        </ul>
      )}

      <Modal open={adding} onClose={() => setAdding(false)} title="Add user">
        <form onSubmit={create} className="space-y-4">
          {formError && <ErrorNote message={formError} />}
          <Field label="Email">{(id) => <Input id={id} type="email" required autoFocus autoComplete="off" value={email} onChange={(event) => setEmail(event.target.value)} />}</Field>
          <Field label="Password" hint="At least 12 characters. Share it with the user through a secure channel.">
            {(id) => <Input id={id} type="password" required minLength={12} autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} />}
          </Field>
          <Field label="Role">
            {(id) => (
              <Select id={id} value={role} onChange={(event) => setRole(event.target.value as Role)}>
                <option value="viewer">Viewer</option>
                <option value="developer">Developer</option>
                <option value="admin">Admin</option>
              </Select>
            )}
          </Field>
          <div className="flex justify-end gap-2 border-t border-line pt-4">
            <Button variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" busy={busy}>
              Add user
            </Button>
          </div>
        </form>
      </Modal>
      <Confirm
        open={removing !== null}
        title={`Delete ${removing?.email ?? ''}?`}
        body="They are signed out everywhere and can no longer sign in."
        confirmLabel="Delete user"
        onClose={() => setRemoving(null)}
        onConfirm={async () => {
          if (!removing) return
          try {
            await api.deleteUser(removing.id)
            toast.success(`Deleted ${removing.email}`)
            reload()
          } catch (reason) {
            toast.error(reason)
          }
        }}
      />
    </Card>
  )
}

function Audit() {
  const { data, error, reload } = useLoad(() => api.audit(), [], 15000)
  return (
    <Card>
      <CardTitle hint="Who changed what, newest first">Audit log</CardTitle>
      {error && <ErrorNote message={error} onRetry={reload} />}
      {!data ? (
        <Spinner />
      ) : (
        <ul className="divide-y divide-line/60">
          {data.items.map((entry) => (
            <li key={entry.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5 text-sm">
              <time className="w-28 shrink-0 font-mono text-xs text-ink-faint">{dateTime(entry.ts)}</time>
              <Badge tone={entry.action.includes('fail') || entry.action.includes('reuse') ? 'bad' : entry.action.includes('delete') || entry.action.includes('revoke') ? 'warn' : 'idle'}>{entry.action}</Badge>
              <span className="truncate text-ink">{entry.actor || entry.actor_type}</span>
              {typeof entry.detail.name === 'string' && <span className="truncate text-ink-soft">{entry.detail.name}</span>}
              <span className="ml-auto font-mono text-xs text-ink-faint">{entry.ip ?? ''}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

export function SettingsPage() {
  const { isAdmin } = useAuth()
  return (
    <>
      <PageHeader title="Settings" subtitle="Tune the adaptive engine, manage access, review changes" />
      <div className="space-y-4">
        <ConfigEditor editable={isAdmin} />
        {isAdmin && <Users />}
        {isAdmin && <Audit />}
      </div>
    </>
  )
}

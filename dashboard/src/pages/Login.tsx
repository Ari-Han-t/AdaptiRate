import { Activity } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Button, ErrorNote, Field, Input } from '../components/ui'
import { useAuth } from '../lib/auth'

export function LoginPage() {
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(email.trim(), password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-accent">
            <Activity size={24} className="text-white" strokeWidth={2.6} />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">
            AdaptiRate
          </h1>
          <p className="mt-1 text-sm text-ink-soft">Sign in to manage rate limits</p>
        </div>

        <form onSubmit={submit} className="card space-y-4 p-6">
          {error && <ErrorNote message={error} />}
          <Field label="Email">
            {(id) => <Input id={id} type="email" autoComplete="username" required autoFocus value={email} onChange={(event) => setEmail(event.target.value)} />}
          </Field>
          <Field label="Password">
            {(id) => <Input id={id} type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />}
          </Field>
          <Button type="submit" variant="primary" busy={busy} className="w-full">
            Sign in
          </Button>
        </form>
        <p className="mt-4 text-center text-xs text-ink-faint">The first admin account is created from AR_BOOTSTRAP_ADMIN_EMAIL and AR_BOOTSTRAP_ADMIN_PASSWORD.</p>
      </div>
    </div>
  )
}

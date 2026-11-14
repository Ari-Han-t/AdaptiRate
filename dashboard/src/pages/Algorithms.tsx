import { motion } from 'framer-motion'
import { Droplets, Layers, Shuffle, SlidersHorizontal, Sparkles, Timer } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useState } from 'react'
import { Badge, Card, CardTitle, Empty, ErrorNote, PageHeader, Select, Spinner, useToast } from '../components/ui'
import { api } from '../lib/api'
import type { Algorithm, Policy } from '../lib/api'
import { useAuth } from '../lib/auth'
import { ALGORITHM_LABEL, num, windowLabel } from '../lib/format'
import { useLoad } from '../lib/hooks'

const ICON: Record<Algorithm, LucideIcon> = {
  token_bucket: Layers,
  sliding_window: SlidersHorizontal,
  fixed_window: Timer,
  leaky_bucket: Droplets,
  adaptive_hybrid: Sparkles,
}

function SwitchRow({ policy, canManage, onChanged }: { policy: Policy; canManage: boolean; onChanged: () => void }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  async function change(algorithm: Algorithm) {
    setBusy(true)
    try {
      await api.updatePolicy(policy.id, { algorithm })
      toast.success(`${policy.name} now uses ${ALGORITHM_LABEL[algorithm]}`)
      onChanged()
    } catch (reason) {
      toast.error(reason)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-canvas px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{policy.name}</p>
        <p className="text-xs text-ink-faint">
          {num(policy.limit)} per {windowLabel(policy.window_seconds)}
          {policy.burst > 0 && ` · burst ${num(policy.burst)}`}
        </p>
      </div>
      {!policy.enabled && <Badge tone="warn">Disabled</Badge>}
      <Select aria-label={`Algorithm for ${policy.name}`} className="w-52" value={policy.algorithm} disabled={!canManage || busy} onChange={(event) => void change(event.target.value as Algorithm)}>
        {Object.entries(ALGORITHM_LABEL).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </Select>
    </div>
  )
}

export function AlgorithmsPage() {
  const { canManage } = useAuth()
  const algorithms = useLoad(api.algorithms, [])
  const policies = useLoad(api.policies, [])

  const reload = () => {
    algorithms.reload()
    policies.reload()
  }

  return (
    <>
      <PageHeader title="Algorithms" subtitle="How each limiter counts, and which policy uses which" />
      {(algorithms.error || policies.error) && <ErrorNote message={algorithms.error ?? policies.error ?? ''} onRetry={reload} />}
      {!algorithms.data ? (
        <Spinner />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {algorithms.data.map((algorithm, index) => {
            const Icon = ICON[algorithm.id]
            return (
              <motion.div key={algorithm.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.04 }}>
                <Card className="h-full">
                  <div className="flex items-start justify-between">
                    <div className="rounded-lg bg-accent/8 p-2.5 text-accent-strong">
                      <Icon size={20} />
                    </div>
                    <Badge tone={algorithm.policies > 0 ? 'good' : 'idle'}>
                      {algorithm.policies} {algorithm.policies === 1 ? 'policy' : 'policies'}
                    </Badge>
                  </div>
                  <h3 className="mt-4 text-base font-semibold">{algorithm.label}</h3>
                  <p className="mt-1 text-sm text-ink-soft">{algorithm.summary}</p>
                  <dl className="mt-4 space-y-2 text-xs">
                    <div>
                      <dt className="text-ink-faint">Best for</dt>
                      <dd className="text-ink-soft">{algorithm.best_for}</dd>
                    </div>
                    <div>
                      <dt className="text-ink-faint">Burst setting</dt>
                      <dd className="text-ink-soft">{algorithm.burst}</dd>
                    </div>
                  </dl>
                </Card>
              </motion.div>
            )
          })}
        </div>
      )}

      <Card className="mt-4">
        <CardTitle hint="Takes effect on the next request. Each algorithm keeps its own counters, so a switch starts from a clean state.">Switch live</CardTitle>
        {!policies.data ? (
          <Spinner />
        ) : policies.data.length === 0 ? (
          <Empty icon={<Shuffle size={20} />} title="No policies to switch">
            Create a policy first.
          </Empty>
        ) : (
          <div className="space-y-2">
            {policies.data.map((policy) => (
              <SwitchRow key={policy.id} policy={policy} canManage={canManage} onChanged={reload} />
            ))}
          </div>
        )}
      </Card>
    </>
  )
}

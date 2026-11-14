import { AnimatePresence, motion } from 'framer-motion'
import {
  Activity,
  BarChart3,
  Cpu,
  Gauge,
  KeyRound,
  LogOut,
  Menu,
  ScrollText,
  Settings,
  ShieldCheck,
  Shuffle,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useAuth } from '../lib/auth'
import { healthTone } from '../lib/format'
import { useLive } from '../lib/live'
import { Badge, Dot, cn } from './ui'

const NAV = [
  { to: '/', label: 'Dashboard', icon: Gauge },
  { to: '/policies', label: 'Policies', icon: ShieldCheck },
  { to: '/algorithms', label: 'Algorithms', icon: Shuffle },
  { to: '/analytics', label: 'Analytics', icon: BarChart3 },
  { to: '/monitoring', label: 'Monitoring', icon: Cpu },
  { to: '/logs', label: 'Logs', icon: ScrollText },
  { to: '/keys', label: 'API Keys', icon: KeyRound },
  { to: '/settings', label: 'Settings', icon: Settings },
]

function Brand() {
  return (
    <div className="flex h-16 items-center gap-2.5 px-5">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent">
        <Activity size={17} className="text-white" strokeWidth={2.6} />
      </div>
      <span className="text-[17px] font-semibold tracking-tight">
        AdaptiRate
      </span>
    </div>
  )
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex-1 space-y-0.5 px-3 py-2" aria-label="Main">
      {NAV.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === '/'}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
              isActive ? 'text-accent-strong' : 'text-ink-soft hover:bg-raised hover:text-ink',
            )
          }
        >
          {({ isActive }) => (
            <>
              {isActive && (
                <span className="absolute inset-0 rounded-lg bg-accent/8" />
              )}
              <Icon size={17} className={cn('relative', isActive ? 'text-accent-strong' : 'text-ink-faint group-hover:text-ink-soft')} />
              <span className="relative">{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}

function Account() {
  const { user, logout } = useAuth()
  if (!user) return null
  return (
    <div className="border-t border-line p-3">
      <div className="flex items-center gap-3 rounded-lg px-2 py-2">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-raised text-xs font-semibold uppercase text-ink-soft ring-1 ring-line">
          {user.email.slice(0, 2)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-ink" title={user.email}>
            {user.email}
          </p>
          <p className="text-xs capitalize text-ink-faint">{user.role}</p>
        </div>
        <button type="button" onClick={() => void logout()} aria-label="Sign out" title="Sign out" className="rounded-md p-1.5 text-ink-faint hover:bg-raised hover:text-ink">
          <LogOut size={16} />
        </button>
      </div>
    </div>
  )
}

function StatusBar() {
  const { snapshot, connected } = useLive()
  const tone = healthTone(snapshot?.health)
  const label = { good: 'Healthy', warn: 'Under pressure', bad: 'Degraded', idle: 'No signals yet' }[tone]
  return (
    <div className="flex items-center gap-2">
      <Badge tone={tone === 'idle' ? 'idle' : tone}>
        <Dot tone={tone === 'idle' ? 'idle' : tone} pulse={tone !== 'idle'} />
        {label}
        {snapshot?.health != null && <span className="opacity-70">· {Math.round(snapshot.health)}</span>}
      </Badge>
      <Badge tone={connected ? 'accent' : 'warn'}>
        <Dot tone={connected ? 'accent' : 'warn'} pulse={connected} />
        {connected ? 'Live' : 'Reconnecting'}
      </Badge>
    </div>
  )
}

export function Layout({ children }: { children: ReactNode }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const location = useLocation()

  useEffect(() => setMenuOpen(false), [location.pathname])

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-panel lg:flex">
        <Brand />
        <Navigation />
        <Account />
      </aside>

      <AnimatePresence>
        {menuOpen && (
          <motion.div className="fixed inset-0 z-40 lg:hidden" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="absolute inset-0 bg-ink/30" onClick={() => setMenuOpen(false)} />
            <motion.aside
              className="absolute inset-y-0 left-0 flex w-64 flex-col border-r border-line bg-panel"
              initial={{ x: -260 }}
              animate={{ x: 0 }}
              exit={{ x: -260 }}
              transition={{ type: 'tween', duration: 0.2 }}
            >
              <div className="flex items-center justify-between pr-3">
                <Brand />
                <button type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)} className="rounded-md p-1.5 text-ink-faint hover:bg-raised">
                  <X size={18} />
                </button>
              </div>
              <Navigation onNavigate={() => setMenuOpen(false)} />
              <Account />
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-3 border-b border-line bg-panel px-4 sm:px-8">
          <button type="button" aria-label="Open menu" onClick={() => setMenuOpen(true)} className="rounded-md p-1.5 text-ink-soft hover:bg-raised lg:hidden">
            <Menu size={20} />
          </button>
          <div className="hidden lg:block" />
          <StatusBar />
        </header>
        <main className="mx-auto w-full max-w-[1500px] flex-1 px-4 py-6 sm:px-8 sm:py-8">
          {children}
        </main>
      </div>
    </div>
  )
}

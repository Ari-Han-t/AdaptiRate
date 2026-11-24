import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, restoreSession, setSignedOutHandler } from './api'
import type { User } from './api'

interface AuthState {
  user: User | null
  loading: boolean
  canManage: boolean
  isAdmin: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

// Public demo deployments set these at build time to sign visitors in automatically.
// Point them at a viewer account: the dashboard then works read-only with no login screen.
const DEMO_EMAIL: string | undefined = import.meta.env.VITE_DEMO_EMAIL
const DEMO_PASSWORD: string | undefined = import.meta.env.VITE_DEMO_PASSWORD

async function demoLogin(): Promise<User | null> {
  if (!DEMO_EMAIL || !DEMO_PASSWORD) return null
  try {
    return await api.login(DEMO_EMAIL, DEMO_PASSWORD)
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setSignedOutHandler(() => setUser(null))
    void restoreSession()
      .then((restored) => restored ?? demoLogin())
      .then((restored) => {
        setUser(restored)
        setLoading(false)
      })
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    setUser(await api.login(email, password))
  }, [])

  const logout = useCallback(async () => {
    await api.logout()
    setUser(null)
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      canManage: user?.role === 'admin' || user?.role === 'developer',
      isAdmin: user?.role === 'admin',
      login,
      logout,
    }),
    [user, loading, login, logout],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}

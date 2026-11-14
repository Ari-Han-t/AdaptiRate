import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Spinner } from './components/ui'
import { useAuth } from './lib/auth'
import { LiveProvider } from './lib/live'
import { AlgorithmsPage } from './pages/Algorithms'
import { AnalyticsPage } from './pages/Analytics'
import { ApiKeysPage } from './pages/ApiKeys'
import { DashboardPage } from './pages/Dashboard'
import { LoginPage } from './pages/Login'
import { LogsPage } from './pages/Logs'
import { MonitoringPage } from './pages/Monitoring'
import { PoliciesPage } from './pages/Policies'
import { SettingsPage } from './pages/Settings'

export function App() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring session" />
      </div>
    )
  }
  if (!user) return <LoginPage />

  return (
    <LiveProvider>
      <Layout>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/policies" element={<PoliciesPage />} />
          <Route path="/algorithms" element={<AlgorithmsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/monitoring" element={<MonitoringPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/keys" element={<ApiKeysPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </LiveProvider>
  )
}

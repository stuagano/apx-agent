import { useState, useEffect, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '@/hooks/use-api'
import { useToast } from '@/hooks/use-toast'
import { useBreadcrumbStore } from '@/stores/breadcrumb-store'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { DataTable } from '@/components/ui/data-table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { AlertTriangle, ShieldAlert, ShieldCheck, RefreshCw } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ViolationSummary {
  total: number
  active: number
  critical: number
  high: number
  medium: number
  low: number
}

interface Violation {
  violation_id: string
  resource_id: string
  resource_name: string
  resource_type: string
  policy_id: string
  policy_name: string
  severity: 'critical' | 'high' | 'medium' | 'low'
  domain: string
  first_seen: string
  last_seen: string
  active: boolean
  scan_id: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  high:     'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  medium:   'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  low:      'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function GovernanceDashboard() {
  const { t } = useTranslation(['governance', 'common'])
  const { get, loading } = useApi()
  const { toast } = useToast()
  const navigate = useNavigate()
  const breadcrumbs = useBreadcrumbStore()

  const [summary, setSummary]         = useState<ViolationSummary | null>(null)
  const [violations, setViolations]   = useState<Violation[]>([])
  const [error, setError]             = useState<string | null>(null)
  const [severityFilter, setSeverityFilter] = useState<string>('all')
  const [showActive, setShowActive]   = useState(true)

  useEffect(() => {
    breadcrumbs.setStaticSegments([
      { label: t('common:governance'), path: '/governance' },
      { label: t('governance:title') },
    ])
    fetchAll()
  }, [])

  useEffect(() => {
    fetchViolations()
  }, [severityFilter, showActive])

  const fetchAll = useCallback(async () => {
    const [sumRes, violRes] = await Promise.all([
      get<ViolationSummary>('/api/governance/violations/summary'),
      get<Violation[]>('/api/governance/violations'),
    ])
    if (sumRes.error || violRes.error) {
      const msg = sumRes.error || violRes.error!
      setError(msg)
      toast({ title: t('common:error'), description: msg, variant: 'destructive' })
      return
    }
    if (sumRes.data)  setSummary(sumRes.data)
    if (violRes.data) setViolations(violRes.data)
  }, [get, toast, t])

  const fetchViolations = useCallback(async () => {
    const params = new URLSearchParams({ active: String(showActive) })
    if (severityFilter !== 'all') params.set('severity', severityFilter)
    const { data, error } = await get<Violation[]>(
      `/api/governance/violations?${params}`
    )
    if (error) return
    if (data) setViolations(data)
  }, [get, severityFilter, showActive])

  const columns = useMemo(() => [
    {
      accessorKey: 'severity',
      header: t('governance:severity'),
      cell: (info: any) => (
        <Badge className={SEVERITY_BADGE[info.getValue()] ?? ''}>
          {info.getValue()}
        </Badge>
      ),
    },
    {
      accessorKey: 'resource_name',
      header: t('governance:resource'),
      cell: (info: any) => (
        <button
          className="text-primary hover:underline font-medium text-left"
          onClick={() => navigate(`/governance/resources/${info.row.original.resource_id}`)}
        >
          {info.getValue()}
        </button>
      ),
    },
    { accessorKey: 'resource_type', header: t('governance:type') },
    { accessorKey: 'policy_name',   header: t('governance:policy') },
    { accessorKey: 'domain',        header: t('governance:domain') },
    {
      accessorKey: 'first_seen',
      header: t('governance:first_seen'),
      cell: (info: any) => new Date(info.getValue()).toLocaleDateString(),
    },
    {
      accessorKey: 'last_seen',
      header: t('governance:last_seen'),
      cell: (info: any) => new Date(info.getValue()).toLocaleDateString(),
    },
  ], [t, navigate])

  return (
    <>
      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>{t('common:error')}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Summary cards */}
      {summary && (
        <div className="grid gap-4 md:grid-cols-5 mb-6">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{t('governance:active')}</CardTitle>
              <ShieldAlert className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{summary.active}</div>
              <p className="text-xs text-muted-foreground">{t('governance:of_total', { total: summary.total })}</p>
            </CardContent>
          </Card>

          {(['critical', 'high', 'medium', 'low'] as const).map((sev) => (
            <Card
              key={sev}
              className="cursor-pointer hover:border-primary transition-colors"
              onClick={() => setSeverityFilter(sev === severityFilter as any ? 'all' : sev)}
            >
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium capitalize">{sev}</CardTitle>
                <ShieldCheck className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className={`text-2xl font-bold ${SEVERITY_BADGE[sev].split(' ')[1]}`}>
                  {summary[sev]}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Controls */}
      <div className="flex gap-3 mb-4 items-center">
        <Select value={severityFilter} onValueChange={setSeverityFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder={t('governance:filter_severity')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('common:all')}</SelectItem>
            <SelectItem value="critical">Critical</SelectItem>
            <SelectItem value="high">High</SelectItem>
            <SelectItem value="medium">Medium</SelectItem>
            <SelectItem value="low">Low</SelectItem>
          </SelectContent>
        </Select>

        <Button
          variant={showActive ? 'default' : 'outline'}
          size="sm"
          onClick={() => setShowActive(!showActive)}
        >
          {showActive ? t('governance:showing_active') : t('governance:showing_all')}
        </Button>

        <Button variant="ghost" size="icon" onClick={fetchAll} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>

        <div className="ml-auto">
          <Button variant="outline" size="sm" onClick={() => navigate('/governance/exceptions')}>
            {t('governance:manage_exceptions')}
          </Button>
        </div>
      </div>

      <DataTable
        columns={columns}
        data={violations}
        loading={loading}
        searchColumn="resource_name"
        onRowClick={(row) => navigate(`/governance/resources/${row.resource_id}`)}
      />
    </>
  )
}

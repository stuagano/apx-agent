import { useState, useEffect, useCallback, useMemo } from 'react'
import { useApi } from '@/hooks/use-api'
import { useToast } from '@/hooks/use-toast'
import { useBreadcrumbStore } from '@/stores/breadcrumb-store'
import { usePermissions } from '@/stores/permissions-store'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { DataTable } from '@/components/ui/data-table'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { AlertTriangle, Clock, MoreHorizontal, Trash2 } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ExceptionSummary {
  total: number
  active: number
  permanent: number
  expired: number
  expiring_soon: number
}

interface Exception {
  exception_id: string
  resource_id: string
  policy_id: string
  approved_by: string
  justification: string
  approved_at: string
  expires_at: string | null
  active: boolean
  expiry_status: 'permanent' | 'active' | 'expiring_soon' | 'expired'
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const EXPIRY_BADGE: Record<string, string> = {
  permanent:    'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  active:       'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  expiring_soon:'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  expired:      'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function GovernanceExceptions() {
  const { t } = useTranslation(['governance', 'common'])
  const { get, post, loading } = useApi()
  const { toast } = useToast()
  const { getPermissionLevel } = usePermissions()
  const breadcrumbs = useBreadcrumbStore()

  const [summary, setSummary]               = useState<ExceptionSummary | null>(null)
  const [exceptions, setExceptions]         = useState<Exception[]>([])
  const [error, setError]                   = useState<string | null>(null)
  const [revokeTarget, setRevokeTarget]     = useState<Exception | null>(null)
  const [bulkRevokeOpen, setBulkRevokeOpen] = useState(false)
  const [showExpired, setShowExpired]       = useState(false)

  useEffect(() => {
    breadcrumbs.setStaticSegments([
      { label: t('common:governance'), path: '/governance' },
      { label: t('governance:exceptions') },
    ])
    fetchAll()
  }, [])

  useEffect(() => {
    fetchExceptions()
  }, [showExpired])

  const fetchAll = useCallback(async () => {
    const [sumRes, excRes] = await Promise.all([
      get<ExceptionSummary>('/api/governance/exceptions/summary'),
      get<Exception[]>('/api/governance/exceptions'),
    ])
    if (sumRes.error || excRes.error) {
      setError(sumRes.error || excRes.error!)
      return
    }
    if (sumRes.data) setSummary(sumRes.data)
    if (excRes.data) setExceptions(excRes.data)
  }, [get])

  const fetchExceptions = useCallback(async () => {
    const params = new URLSearchParams({ active: String(!showExpired) })
    const { data, error } = await get<Exception[]>(`/api/governance/exceptions?${params}`)
    if (error) return
    if (data) setExceptions(data)
  }, [get, showExpired])

  const handleRevoke = useCallback(async (exceptionId: string) => {
    if (getPermissionLevel() === 'read_only') {
      toast({ title: t('common:permission_denied'), variant: 'destructive' })
      return
    }
    // useApi delete_
    const res = await fetch(`/api/governance/exceptions/${exceptionId}`, { method: 'DELETE' })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      toast({ title: t('common:error'), description: body?.detail ?? t('common:unknown_error'), variant: 'destructive' })
      return
    }
    toast({ title: t('common:success'), description: t('governance:exception_revoked') })
    setRevokeTarget(null)
    fetchAll()
  }, [getPermissionLevel, toast, t, fetchAll])

  const handleBulkRevokeExpired = useCallback(async () => {
    if (getPermissionLevel() === 'read_only') {
      toast({ title: t('common:permission_denied'), variant: 'destructive' })
      return
    }
    const { data, error } = await post<{ revoked: number }>(
      '/api/governance/exceptions/bulk-revoke-expired', {}
    )
    if (error) {
      toast({ title: t('common:error'), description: error, variant: 'destructive' })
      return
    }
    toast({
      title: t('common:success'),
      description: t('governance:bulk_revoked', { count: data?.revoked ?? 0 }),
    })
    setBulkRevokeOpen(false)
    fetchAll()
  }, [getPermissionLevel, post, toast, t, fetchAll])

  const columns = useMemo(() => [
    {
      accessorKey: 'expiry_status',
      header: t('governance:status'),
      cell: (info: any) => (
        <Badge className={EXPIRY_BADGE[info.getValue()] ?? ''}>{info.getValue()}</Badge>
      ),
    },
    { accessorKey: 'resource_id', header: t('governance:resource') },
    { accessorKey: 'policy_id',   header: t('governance:policy') },
    { accessorKey: 'approved_by', header: t('governance:approved_by') },
    {
      accessorKey: 'justification',
      header: t('governance:justification'),
      cell: (info: any) => (
        <span className="text-sm truncate max-w-xs block" title={info.getValue()}>
          {info.getValue()}
        </span>
      ),
    },
    {
      accessorKey: 'expires_at',
      header: t('governance:expires'),
      cell: (info: any) =>
        info.getValue() ? new Date(info.getValue()).toLocaleDateString() : '—',
    },
    {
      id: 'actions',
      header: '',
      cell: (info: any) => {
        const exc: Exception = info.row.original
        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon">
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                className="text-destructive"
                onClick={() => setRevokeTarget(exc)}
                disabled={!exc.active}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {t('governance:revoke')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )
      },
    },
  ], [t])

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
        <div className="grid gap-4 md:grid-cols-4 mb-6">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">{t('governance:active')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{summary.active}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">{t('governance:permanent')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-green-600">{summary.permanent}</div>
            </CardContent>
          </Card>
          <Card
            className={`cursor-pointer transition-colors ${summary.expiring_soon > 0 ? 'border-yellow-400' : ''}`}
            onClick={() => setShowExpired(false)}
          >
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {t('governance:expiring_soon')}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className={`text-2xl font-bold ${summary.expiring_soon > 0 ? 'text-yellow-600' : ''}`}>
                {summary.expiring_soon}
              </div>
            </CardContent>
          </Card>
          <Card className={`cursor-pointer transition-colors ${summary.expired > 0 ? 'border-red-400' : ''}`}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">{t('governance:expired')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className={`text-2xl font-bold ${summary.expired > 0 ? 'text-red-600' : ''}`}>
                {summary.expired}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Controls */}
      <div className="flex gap-3 mb-4 items-center">
        <Button
          variant={showExpired ? 'outline' : 'default'}
          size="sm"
          onClick={() => setShowExpired(false)}
        >
          {t('governance:active')}
        </Button>
        <Button
          variant={showExpired ? 'default' : 'outline'}
          size="sm"
          onClick={() => setShowExpired(true)}
        >
          {t('governance:expired')}
        </Button>

        {(summary?.expired ?? 0) > 0 && (
          <div className="ml-auto">
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setBulkRevokeOpen(true)}
              disabled={loading}
            >
              {t('governance:bulk_revoke_expired', { count: summary?.expired })}
            </Button>
          </div>
        )}
      </div>

      <DataTable
        columns={columns}
        data={exceptions}
        loading={loading}
        searchColumn="resource_id"
      />

      {/* Revoke confirmation */}
      <AlertDialog open={!!revokeTarget} onOpenChange={(o) => !o && setRevokeTarget(null)}>
        <AlertDialogContent>
          <AlertDialogTitle>{t('governance:confirm_revoke')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('governance:confirm_revoke_description', {
              resource: revokeTarget?.resource_id,
              policy: revokeTarget?.policy_id,
            })}
          </AlertDialogDescription>
          <div className="flex gap-2 justify-end mt-2">
            <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => revokeTarget && handleRevoke(revokeTarget.exception_id)}
            >
              {t('governance:revoke')}
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>

      {/* Bulk revoke confirmation */}
      <AlertDialog open={bulkRevokeOpen} onOpenChange={setBulkRevokeOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>{t('governance:bulk_revoke_title')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('governance:bulk_revoke_description', { count: summary?.expired })}
          </AlertDialogDescription>
          <div className="flex gap-2 justify-end mt-2">
            <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={handleBulkRevokeExpired}
            >
              {t('governance:revoke_all')}
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

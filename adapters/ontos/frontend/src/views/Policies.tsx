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
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { AlertTriangle, Plus, MoreHorizontal, Lock } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Policy {
  policy_id: string
  policy_name: string
  applies_to: string
  domain: string
  severity: 'critical' | 'high' | 'medium' | 'low'
  description: string
  remediation: string
  active: boolean
  rule_json: string
  origin: 'yaml' | 'user'
  updated_at: string | null
}

type PolicyFormData = Omit<Policy, 'policy_id' | 'origin' | 'updated_at'>

const DEFAULT_FORM: PolicyFormData = {
  policy_name: '',
  applies_to: '*',
  domain: 'User',
  severity: 'medium',
  description: '',
  remediation: '',
  active: true,
  rule_json: '{}',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  high:     'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  medium:   'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  low:      'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function GovernancePolicies() {
  const { t } = useTranslation(['governance', 'common'])
  const { get, post, loading } = useApi()
  // get is used only for fetching policies/ontology-classes
  const { toast } = useToast()
  const { getPermissionLevel } = usePermissions()
  const breadcrumbs = useBreadcrumbStore()

  const [policies, setPolicies]             = useState<Policy[]>([])
  const [ontologyClasses, setOntologyClasses] = useState<string[]>([])
  const [error, setError]                   = useState<string | null>(null)
  const [dialogOpen, setDialogOpen]         = useState(false)
  const [selected, setSelected]             = useState<Policy | null>(null)
  const [formData, setFormData]             = useState<PolicyFormData>(DEFAULT_FORM)
  const [jsonError, setJsonError]           = useState<string | null>(null)

  useEffect(() => {
    breadcrumbs.setStaticSegments([
      { label: t('common:governance'), path: '/governance' },
      { label: t('governance:policies') },
    ])
    fetchAll()
  }, [])

  const fetchAll = useCallback(async () => {
    const [polRes, classRes] = await Promise.all([
      get<Policy[]>('/api/governance/policies'),
      get<string[]>('/api/governance/policies/ontology-classes'),
    ])
    if (polRes.error) {
      setError(polRes.error)
      return
    }
    if (polRes.data)   setPolicies(polRes.data)
    if (classRes.data) setOntologyClasses(classRes.data)
  }, [get])

  const openCreate = useCallback(() => {
    setSelected(null)
    setFormData(DEFAULT_FORM)
    setJsonError(null)
    setDialogOpen(true)
  }, [])

  const openEdit = useCallback((policy: Policy) => {
    setSelected(policy)
    setFormData({
      policy_name:  policy.policy_name,
      applies_to:   policy.applies_to,
      domain:       policy.domain,
      severity:     policy.severity,
      description:  policy.description,
      remediation:  policy.remediation,
      active:       policy.active,
      rule_json:    policy.rule_json,
    })
    setJsonError(null)
    setDialogOpen(true)
  }, [])

  const handleSave = useCallback(async () => {
    if (getPermissionLevel() === 'read_only') {
      toast({ title: t('common:permission_denied'), variant: 'destructive' })
      return
    }
    try {
      JSON.parse(formData.rule_json)
    } catch {
      setJsonError(t('governance:invalid_json'))
      return
    }
    setJsonError(null)

    let errorMsg: string | null = null

    if (selected) {
      // PATCH — useApi doesn't expose patch, use fetch directly
      const res = await fetch(`/api/governance/policies/${selected.policy_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        errorMsg = body?.detail ?? t('common:unknown_error')
      }
    } else {
      const { error } = await post<Policy>('/api/governance/policies', formData)
      errorMsg = error ?? null
    }

    if (errorMsg) {
      toast({ title: t('common:error'), description: errorMsg, variant: 'destructive' })
      return
    }
    toast({ title: t('common:success') })
    setDialogOpen(false)
    fetchAll()
  }, [formData, selected, getPermissionLevel, post, toast, t, fetchAll])

  const field = useCallback(
    (key: keyof PolicyFormData) =>
      (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
        setFormData((prev) => ({ ...prev, [key]: e.target.value })),
    []
  )

  const columns = useMemo(() => [
    {
      accessorKey: 'severity',
      header: t('governance:severity'),
      cell: (info: any) => (
        <Badge className={SEVERITY_BADGE[info.getValue()] ?? ''}>{info.getValue()}</Badge>
      ),
    },
    { accessorKey: 'policy_name', header: t('governance:policy_name') },
    { accessorKey: 'applies_to',  header: t('governance:applies_to') },
    { accessorKey: 'domain',      header: t('governance:domain') },
    {
      accessorKey: 'origin',
      header: t('governance:origin'),
      cell: (info: any) => (
        <span className="flex items-center gap-1 text-sm">
          {info.getValue() === 'yaml' && <Lock className="h-3 w-3 text-muted-foreground" />}
          {info.getValue()}
        </span>
      ),
    },
    {
      accessorKey: 'active',
      header: t('common:status'),
      cell: (info: any) => (
        <Badge variant={info.getValue() ? 'default' : 'secondary'}>
          {info.getValue() ? t('common:active') : t('common:inactive')}
        </Badge>
      ),
    },
    {
      id: 'actions',
      header: '',
      cell: (info: any) => {
        const policy: Policy = info.row.original
        const isYaml = policy.origin === 'yaml'
        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon">
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => openEdit(policy)}
                disabled={isYaml}
              >
                {isYaml ? t('governance:yaml_locked') : t('common:edit')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )
      },
    },
  ], [t, openEdit])

  return (
    <>
      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>{t('common:error')}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-3 mb-6">
        {(['critical', 'high', 'medium'] as const).map((sev) => {
          const count = policies.filter((p) => p.active && p.severity === sev).length
          return (
            <Card key={sev}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium capitalize">{sev}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className={`text-2xl font-bold ${SEVERITY_BADGE[sev].split(' ')[1]}`}>
                  {count}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      <div className="flex justify-end mb-4">
        <Button onClick={openCreate} disabled={loading}>
          <Plus className="h-4 w-4 mr-2" />
          {t('governance:new_policy')}
        </Button>
      </div>

      <DataTable
        columns={columns}
        data={policies}
        loading={loading}
        searchColumn="policy_name"
      />

      {/* Create / Edit dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {selected ? t('governance:edit_policy') : t('governance:new_policy')}
            </DialogTitle>
            {selected?.origin === 'yaml' && (
              <p className="text-sm text-destructive flex items-center gap-1 mt-1">
                <Lock className="h-3 w-3" />{t('governance:yaml_locked_description')}
              </p>
            )}
          </DialogHeader>

          <div className="grid grid-cols-2 gap-4 mt-2">
            <div className="col-span-2">
              <label className="text-sm font-medium">{t('governance:policy_name')}</label>
              <Input value={formData.policy_name} onChange={field('policy_name')} className="mt-1" />
            </div>

            <div>
              <label className="text-sm font-medium">{t('governance:applies_to')}</label>
              <Select value={formData.applies_to} onValueChange={(v) => setFormData((p) => ({ ...p, applies_to: v }))}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (all)</SelectItem>
                  {ontologyClasses.map((c) => (
                    <SelectItem key={c} value={c}>{c}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div>
              <label className="text-sm font-medium">{t('governance:severity')}</label>
              <Select value={formData.severity} onValueChange={(v: any) => setFormData((p) => ({ ...p, severity: v }))}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['critical', 'high', 'medium', 'low'].map((s) => (
                    <SelectItem key={s} value={s}>{s}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="col-span-2">
              <label className="text-sm font-medium">{t('governance:description')}</label>
              <Textarea value={formData.description} onChange={field('description')} className="mt-1" rows={2} />
            </div>

            <div className="col-span-2">
              <label className="text-sm font-medium">{t('governance:remediation')}</label>
              <Textarea value={formData.remediation} onChange={field('remediation')} className="mt-1" rows={2} />
            </div>

            <div className="col-span-2">
              <label className="text-sm font-medium">{t('governance:rule_json')}</label>
              <Textarea
                value={formData.rule_json}
                onChange={field('rule_json')}
                className={`mt-1 font-mono text-xs ${jsonError ? 'border-destructive' : ''}`}
                rows={5}
              />
              {jsonError && <p className="text-xs text-destructive mt-1">{jsonError}</p>}
            </div>
          </div>

          <div className="flex gap-2 justify-end mt-4">
            <Button variant="outline" onClick={() => setDialogOpen(false)}>{t('common:cancel')}</Button>
            <Button onClick={handleSave} disabled={selected?.origin === 'yaml'}>
              {t('common:save')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

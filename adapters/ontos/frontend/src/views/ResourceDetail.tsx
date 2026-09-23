import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useApi } from '@/hooks/use-api'
import { useToast } from '@/hooks/use-toast'
import { useBreadcrumbStore } from '@/stores/breadcrumb-store'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { ArrowLeft, AlertTriangle, Tag, ShieldOff } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ResourceDetail {
  resource_id: string
  resource_name: string
  resource_type: string
  first_seen: string
  last_seen: string
  scan_id: string
  metadata: Record<string, string>
  classifications: { class_name: string; confidence: number; classified_at: string }[]
  violations: {
    violation_id: string
    policy_id: string
    policy_name: string
    severity: string
    domain: string
    first_seen: string
    last_seen: string
    active: boolean
  }[]
  exceptions: {
    exception_id: string
    policy_id: string
    approved_by: string
    justification: string
    approved_at: string
    expires_at: string | null
    active: boolean
    expiry_status: string
  }[]
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  high:     'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  medium:   'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  low:      'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
}

const EXPIRY_BADGE: Record<string, string> = {
  permanent:    'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  active:       'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  expiring_soon:'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  expired:      'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function GovernanceResourceDetail() {
  const { resourceId } = useParams<{ resourceId: string }>()
  const { t } = useTranslation(['governance', 'common'])
  const { get, post, loading } = useApi()
  const { toast } = useToast()
  const navigate = useNavigate()
  const breadcrumbs = useBreadcrumbStore()

  const [resource, setResource]           = useState<ResourceDetail | null>(null)
  const [error, setError]                 = useState<string | null>(null)
  const [approveOpen, setApproveOpen]     = useState(false)
  const [approvePolicyId, setApprovePolicyId] = useState('')
  const [justification, setJustification] = useState('')
  const [expiresDays, setExpiresDays]     = useState('90')
  const [submitting, setSubmitting]       = useState(false)

  useEffect(() => {
    if (!resourceId) return
    fetchResource()
  }, [resourceId])

  const fetchResource = useCallback(async () => {
    const { data, error } = await get<ResourceDetail>(
      `/api/governance/resources/${resourceId}`
    )
    if (error) {
      setError(error)
      return
    }
    if (data) {
      setResource(data)
      breadcrumbs.setStaticSegments([
        { label: t('common:governance'), path: '/governance' },
        { label: t('governance:title'), path: '/governance' },
        { label: data.resource_name },
      ])
    }
  }, [resourceId, get, breadcrumbs, t])

  const handleApproveException = useCallback(async () => {
    if (!resource || !approvePolicyId || !justification) return
    setSubmitting(true)
    const { error } = await post('/api/governance/exceptions', {
      resource_id: resource.resource_id,
      policy_ids: approvePolicyId.split(',').map((s) => s.trim()).filter(Boolean),
      justification,
      expires_days: expiresDays ? parseInt(expiresDays, 10) : null,
    })
    setSubmitting(false)
    if (error) {
      toast({ title: t('common:error'), description: error, variant: 'destructive' })
      return
    }
    toast({ title: t('common:success'), description: t('governance:exception_approved') })
    setApproveOpen(false)
    setJustification('')
    setApprovePolicyId('')
    setExpiresDays('90')
    fetchResource()
  }, [resource, approvePolicyId, justification, expiresDays, post, toast, t, fetchResource])

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>{t('common:error')}</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    )
  }

  if (!resource) return null

  const activeViolations  = resource.violations.filter((v) => v.active)
  const activeExceptions  = resource.exceptions.filter((e) => e.active)

  return (
    <>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Button variant="ghost" size="icon" onClick={() => navigate(-1)}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-xl font-semibold">{resource.resource_name}</h1>
          <p className="text-sm text-muted-foreground capitalize">{resource.resource_type}</p>
        </div>
        {activeViolations.length > 0 && (
          <div className="ml-auto">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setApprovePolicyId(activeViolations.map((v) => v.policy_id).join(', '))
                setApproveOpen(true)
              }}
            >
              <ShieldOff className="h-4 w-4 mr-2" />
              {t('governance:approve_exception')}
            </Button>
          </div>
        )}
      </div>

      {/* Metadata card */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-sm font-medium">{t('governance:resource_metadata')}</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground">{t('governance:type')}</dt>
              <dd className="font-medium capitalize">{resource.resource_type}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t('governance:first_seen')}</dt>
              <dd className="font-medium">{new Date(resource.first_seen).toLocaleDateString()}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t('governance:last_seen')}</dt>
              <dd className="font-medium">{new Date(resource.last_seen).toLocaleDateString()}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t('governance:scan_id')}</dt>
              <dd className="font-mono text-xs truncate">{resource.scan_id}</dd>
            </div>
            {Object.entries(resource.metadata || {}).map(([k, v]) => (
              <div key={k}>
                <dt className="text-muted-foreground">{k}</dt>
                <dd className="font-medium truncate">{v}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>

      {/* Tabs */}
      <Tabs defaultValue="violations">
        <TabsList>
          <TabsTrigger value="violations">
            {t('governance:violations')}
            {activeViolations.length > 0 && (
              <Badge className="ml-2 bg-red-100 text-red-800">{activeViolations.length}</Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="classifications">
            {t('governance:classifications')}
            <Badge className="ml-2" variant="secondary">{resource.classifications.length}</Badge>
          </TabsTrigger>
          <TabsTrigger value="exceptions">
            {t('governance:exceptions')}
            {activeExceptions.length > 0 && (
              <Badge className="ml-2" variant="secondary">{activeExceptions.length}</Badge>
            )}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="violations" className="mt-4 space-y-3">
          {resource.violations.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4">{t('governance:no_violations')}</p>
          ) : resource.violations.map((v) => (
            <Card key={v.violation_id} className={v.active ? '' : 'opacity-50'}>
              <CardContent className="pt-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium text-sm">{v.policy_name}</p>
                    <p className="text-xs text-muted-foreground">{v.domain}</p>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <Badge className={SEVERITY_BADGE[v.severity] ?? ''}>{v.severity}</Badge>
                    {!v.active && <Badge variant="secondary">{t('governance:resolved')}</Badge>}
                  </div>
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  {t('governance:first_seen')} {new Date(v.first_seen).toLocaleDateString()}
                  {' · '}
                  {t('governance:last_seen')} {new Date(v.last_seen).toLocaleDateString()}
                </p>
              </CardContent>
            </Card>
          ))}
        </TabsContent>

        <TabsContent value="classifications" className="mt-4">
          {resource.classifications.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4">{t('governance:no_classifications')}</p>
          ) : (
            <div className="flex flex-wrap gap-2 pt-2">
              {resource.classifications.map((c) => (
                <Badge key={c.class_name} variant="outline" className="gap-1">
                  <Tag className="h-3 w-3" />
                  {c.class_name}
                </Badge>
              ))}
            </div>
          )}
        </TabsContent>

        <TabsContent value="exceptions" className="mt-4 space-y-3">
          {resource.exceptions.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4">{t('governance:no_exceptions')}</p>
          ) : resource.exceptions.map((e) => (
            <Card key={e.exception_id} className={e.active ? '' : 'opacity-50'}>
              <CardContent className="pt-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium text-sm">{e.policy_id}</p>
                    <p className="text-xs text-muted-foreground mt-1">{e.justification}</p>
                  </div>
                  <Badge className={EXPIRY_BADGE[e.expiry_status] ?? ''}>{e.expiry_status}</Badge>
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  {t('governance:approved_by')} {e.approved_by}
                  {e.expires_at && ` · ${t('governance:expires')} ${new Date(e.expires_at).toLocaleDateString()}`}
                </p>
              </CardContent>
            </Card>
          ))}
        </TabsContent>
      </Tabs>

      {/* Approve exception dialog */}
      <Dialog open={approveOpen} onOpenChange={setApproveOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('governance:approve_exception')}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div>
              <label className="text-sm font-medium">{t('governance:policy_ids')}</label>
              <Input
                value={approvePolicyId}
                onChange={(e) => setApprovePolicyId(e.target.value)}
                placeholder="POL-001, POL-002"
                className="mt-1"
              />
              <p className="text-xs text-muted-foreground mt-1">{t('governance:policy_ids_hint')}</p>
            </div>
            <div>
              <label className="text-sm font-medium">{t('governance:justification')}</label>
              <Textarea
                value={justification}
                onChange={(e) => setJustification(e.target.value)}
                placeholder={t('governance:justification_placeholder')}
                className="mt-1"
                rows={3}
              />
            </div>
            <div>
              <label className="text-sm font-medium">{t('governance:expires_days')}</label>
              <Input
                type="number"
                value={expiresDays}
                onChange={(e) => setExpiresDays(e.target.value)}
                placeholder="90"
                className="mt-1 w-32"
                min={1}
                max={730}
              />
              <p className="text-xs text-muted-foreground mt-1">{t('governance:expires_days_hint')}</p>
            </div>
          </div>
          <div className="flex gap-2 justify-end mt-4">
            <Button variant="outline" onClick={() => setApproveOpen(false)}>
              {t('common:cancel')}
            </Button>
            <Button
              onClick={handleApproveException}
              disabled={submitting || !justification || !approvePolicyId}
            >
              {submitting ? t('common:saving') : t('governance:approve')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

import { useState, useEffect, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '@/hooks/use-api'
import { useToast } from '@/hooks/use-toast'
import { useBreadcrumbStore } from '@/stores/breadcrumb-store'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { DataTable } from '@/components/ui/data-table'
import { AlertTriangle, RefreshCw, Wrench, Users, Bot } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Funnel {
  total_violations: number
  with_remediation: number
  pending_review: number
  approved: number
  applied: number
  verified: number
  verification_failed: number
  rejected: number
}

interface AgentStats {
  agent_id: string
  agent_version: string
  total_proposals: number
  verified: number
  failed: number
  rejected: number
  precision_pct: number | null
  avg_confidence: number | null
}

interface ReviewerStats {
  reviewer: string
  pending_reviews: number
  total_approved: number
  total_rejected: number
  total_reassigned: number
  total_reviews: number
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const FUNNEL_STAGES: { key: keyof Funnel; color: string }[] = [
  { key: 'total_violations',    color: 'bg-red-500' },
  { key: 'with_remediation',    color: 'bg-purple-500' },
  { key: 'pending_review',      color: 'bg-yellow-500' },
  { key: 'applied',             color: 'bg-blue-500' },
  { key: 'verified',            color: 'bg-green-500' },
]

// ── Component ─────────────────────────────────────────────────────────────────

export default function RemediationDashboard() {
  const { t } = useTranslation(['governance', 'common'])
  const { get, loading } = useApi()
  const { toast } = useToast()
  const navigate = useNavigate()
  const breadcrumbs = useBreadcrumbStore()

  const [funnel, setFunnel]     = useState<Funnel | null>(null)
  const [agents, setAgents]     = useState<AgentStats[]>([])
  const [reviewers, setReviewers] = useState<ReviewerStats[]>([])
  const [error, setError]       = useState<string | null>(null)

  useEffect(() => {
    breadcrumbs.setStaticSegments([
      { label: t('common:governance'), path: '/governance' },
      { label: t('governance:remediation_dashboard') },
    ])
    fetchAll()
  }, [])

  const fetchAll = useCallback(async () => {
    const [funnelRes, agentRes, reviewerRes] = await Promise.all([
      get<Funnel>('/api/governance/remediation/funnel'),
      get<AgentStats[]>('/api/governance/remediation/agents'),
      get<ReviewerStats[]>('/api/governance/remediation/reviewer-load'),
    ])
    if (funnelRes.error || agentRes.error || reviewerRes.error) {
      const msg = funnelRes.error || agentRes.error || reviewerRes.error!
      setError(msg)
      toast({ title: t('common:error'), description: msg, variant: 'destructive' })
      return
    }
    if (funnelRes.data)   setFunnel(funnelRes.data)
    if (agentRes.data)    setAgents(agentRes.data)
    if (reviewerRes.data) setReviewers(reviewerRes.data)
  }, [get, toast, t])

  const reviewerColumns = useMemo(() => [
    { accessorKey: 'reviewer',        header: 'Reviewer' },
    { accessorKey: 'pending_reviews',  header: t('governance:pending_reviews') },
    { accessorKey: 'total_approved',   header: t('governance:total_approved') },
    { accessorKey: 'total_rejected',   header: t('governance:total_rejected') },
    { accessorKey: 'total_reviews',    header: 'Total' },
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

      {/* Controls */}
      <div className="flex gap-3 mb-6 items-center">
        <Button variant="ghost" size="icon" onClick={fetchAll} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
        <div className="ml-auto">
          <Button onClick={() => navigate('/governance/remediation/inbox')}>
            <Wrench className="h-4 w-4 mr-2" />
            {t('governance:review_now')}
          </Button>
        </div>
      </div>

      {/* Funnel */}
      {funnel && (
        <div className="mb-8">
          <h3 className="text-sm font-medium text-muted-foreground mb-4 uppercase tracking-wide">
            {t('governance:remediation_review')}
          </h3>
          <div className="space-y-3">
            {FUNNEL_STAGES.map(({ key, color }) => {
              const count = funnel[key]
              const maxCount = funnel.total_violations || 1
              const widthPct = Math.max((count / maxCount) * 100, 8)
              const i18nKey = `governance:funnel_${key === 'total_violations' ? 'violations' : key === 'with_remediation' ? 'proposed' : key}`
              return (
                <button
                  key={key}
                  className="w-full text-left group"
                  onClick={() => {
                    if (key === 'pending_review') navigate('/governance/remediation/inbox')
                  }}
                >
                  <div className="flex items-center gap-3">
                    <div className="w-32 text-sm text-muted-foreground truncate">
                      {t(i18nKey)}
                    </div>
                    <div className="flex-1 h-8 bg-muted rounded-md overflow-hidden">
                      <div
                        className={`h-full ${color} rounded-md flex items-center px-3 transition-all group-hover:opacity-90`}
                        style={{ width: `${widthPct}%` }}
                      >
                        <span className="text-sm font-bold text-white">{count}</span>
                      </div>
                    </div>
                  </div>
                </button>
              )
            })}
          </div>

          {/* Secondary stats row */}
          <div className="grid grid-cols-3 gap-4 mt-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-yellow-600">
                  {t('governance:funnel_failed')}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{funnel.verification_failed}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-red-600">
                  {t('governance:funnel_rejected')}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{funnel.rejected}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-blue-600">
                  {t('governance:funnel_approved')}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{funnel.approved}</div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Agent Effectiveness */}
      {agents.length > 0 && (
        <div className="mb-8">
          <h3 className="text-sm font-medium text-muted-foreground mb-4 uppercase tracking-wide flex items-center gap-2">
            <Bot className="h-4 w-4" />
            {t('governance:agent_effectiveness')}
          </h3>
          <div className="grid gap-4 md:grid-cols-3">
            {agents.map((a) => (
              <Card key={`${a.agent_id}-${a.agent_version}`}>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium">{a.agent_id}</CardTitle>
                  <p className="text-xs text-muted-foreground">v{a.agent_version}</p>
                </CardHeader>
                <CardContent>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div>
                      <div className="text-lg font-bold">{a.total_proposals}</div>
                      <div className="text-xs text-muted-foreground">{t('governance:total_proposals')}</div>
                    </div>
                    <div>
                      <div className="text-lg font-bold text-green-600">
                        {a.precision_pct != null ? `${a.precision_pct}%` : '—'}
                      </div>
                      <div className="text-xs text-muted-foreground">{t('governance:precision')}</div>
                    </div>
                    <div>
                      <div className="text-lg font-bold">
                        {a.avg_confidence != null ? a.avg_confidence.toFixed(2) : '—'}
                      </div>
                      <div className="text-xs text-muted-foreground">{t('governance:avg_confidence')}</div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* Reviewer Load */}
      {reviewers.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-muted-foreground mb-4 uppercase tracking-wide flex items-center gap-2">
            <Users className="h-4 w-4" />
            {t('governance:reviewer_load')}
          </h3>
          <DataTable
            columns={reviewerColumns}
            data={reviewers}
            loading={loading}
            searchColumn="reviewer"
          />
        </div>
      )}
    </>
  )
}

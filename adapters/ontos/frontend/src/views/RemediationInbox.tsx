import { useState, useEffect, useCallback } from 'react'
import { useApi } from '@/hooks/use-api'
import { useToast } from '@/hooks/use-toast'
import { useBreadcrumbStore } from '@/stores/breadcrumb-store'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { AlertTriangle, Check, X, ArrowRightLeft, ChevronRight } from 'lucide-react'
import ProposalDiff from '@/components/ProposalDiff'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ProposalSummary {
  proposal_id: string
  violation_id: string
  resource_id: string
  resource_name: string
  resource_type: string
  policy_id: string
  policy_name: string
  severity: 'critical' | 'high' | 'medium' | 'low'
  domain: string
  agent_id: string
  agent_version: string
  status: string
  confidence: number
  proposed_sql: string
  created_at: string
}

interface ReviewRecord {
  review_id: string
  proposal_id: string
  reviewer: string
  decision: string
  reasoning: string
  reassigned_to: string | null
  reviewed_at: string
}

interface ProposalDetail extends ProposalSummary {
  context_json: string
  citations: string
  pre_state: string
  proposed_state: string
  review_history: ReviewRecord[]
}

type ReviewDecision = 'approved' | 'rejected' | 'reassigned'

// ── Helpers ───────────────────────────────────────────────────────────────────

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  high:     'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  medium:   'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  low:      'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
}

const CONFIDENCE_COLOR = (c: number) =>
  c >= 0.8 ? 'text-green-600' : c >= 0.5 ? 'text-yellow-600' : 'text-red-600'

function parseState(json: string): Record<string, string | null> {
  try { return JSON.parse(json) } catch { return {} }
}

function parseContext(json: string): Record<string, unknown> {
  try { return JSON.parse(json) } catch { return {} }
}

const TABS = ['pending_review', 'approved', 'applied', ''] as const

// ── Component ─────────────────────────────────────────────────────────────────

export default function RemediationInbox() {
  const { t } = useTranslation(['governance', 'common'])
  const { get, post, loading } = useApi()
  const { toast } = useToast()
  const breadcrumbs = useBreadcrumbStore()

  const [proposals, setProposals]       = useState<ProposalSummary[]>([])
  const [selected, setSelected]         = useState<ProposalDetail | null>(null)
  const [activeTab, setActiveTab]       = useState<string>('pending_review')
  const [error, setError]               = useState<string | null>(null)

  // Review dialog state
  const [reviewDialog, setReviewDialog] = useState<ReviewDecision | null>(null)
  const [reasoning, setReasoning]       = useState('')
  const [reassignTo, setReassignTo]     = useState('')

  useEffect(() => {
    breadcrumbs.setStaticSegments([
      { label: t('common:governance'), path: '/governance' },
      { label: t('governance:remediation_dashboard'), path: '/governance/remediation' },
      { label: t('governance:remediation_inbox') },
    ])
  }, [])

  useEffect(() => {
    fetchProposals()
  }, [activeTab])

  const fetchProposals = useCallback(async () => {
    const params = new URLSearchParams()
    if (activeTab) params.set('status', activeTab)
    const { data, error: err } = await get<ProposalSummary[]>(
      `/api/governance/remediation/proposals?${params}`
    )
    if (err) { setError(err); return }
    if (data) {
      setProposals(data)
      if (data.length > 0 && (!selected || !data.find(p => p.proposal_id === selected.proposal_id))) {
        fetchDetail(data[0].proposal_id)
      } else if (data.length === 0) {
        setSelected(null)
      }
    }
  }, [get, activeTab, selected])

  const fetchDetail = useCallback(async (proposalId: string) => {
    const { data, error: err } = await get<ProposalDetail>(
      `/api/governance/remediation/proposals/${proposalId}`
    )
    if (err) {
      toast({ title: t('common:error'), description: err, variant: 'destructive' })
      return
    }
    if (data) setSelected(data)
  }, [get, toast, t])

  const handleReview = useCallback(async () => {
    if (!selected || !reviewDialog) return

    const body: Record<string, string> = {
      decision: reviewDialog,
      reasoning,
    }
    if (reviewDialog === 'reassigned') body.reassigned_to = reassignTo

    // Optimistic: remove from list if in pending tab
    const prevProposals = proposals
    const prevSelected = selected
    if (activeTab === 'pending_review') {
      setProposals((prev) => prev.filter((p) => p.proposal_id !== selected.proposal_id))
    }

    const { error: err } = await post(
      `/api/governance/remediation/proposals/${selected.proposal_id}/review`,
      body,
    )

    if (err) {
      // Rollback
      setProposals(prevProposals)
      setSelected(prevSelected)
      toast({ title: t('governance:review_failed'), description: err, variant: 'destructive' })
    } else {
      toast({ title: t('common:success'), description: t('governance:review_submitted') })
      // Select next proposal
      const remaining = prevProposals.filter((p) => p.proposal_id !== selected.proposal_id)
      if (remaining.length > 0) {
        fetchDetail(remaining[0].proposal_id)
      } else {
        setSelected(null)
      }
    }

    setReviewDialog(null)
    setReasoning('')
    setReassignTo('')
  }, [selected, reviewDialog, reasoning, reassignTo, proposals, activeTab, post, toast, t, fetchDetail])

  const tabLabel = (tab: string) => {
    if (tab === 'pending_review') return t('governance:filter_pending')
    if (tab === 'approved') return t('governance:filter_approved')
    if (tab === 'applied') return t('governance:filter_applied')
    return t('governance:filter_all')
  }

  // Parse pre_state and proposed_state from backend-provided JSON
  const preState = selected ? parseState(selected.pre_state) : {}
  const context = selected ? parseContext(selected.context_json) : {}
  const proposedState = selected ? parseState(selected.proposed_state) : {}

  return (
    <div className="flex gap-0 h-[calc(100vh-12rem)]">
      {/* Left panel — queue list */}
      <div className="w-[30%] border-r flex flex-col">
        {/* Tabs */}
        <div className="flex border-b p-1 gap-1">
          {TABS.map((tab) => (
            <button
              key={tab}
              className={`flex-1 text-xs px-2 py-1.5 rounded transition-colors ${
                activeTab === tab
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-muted'
              }`}
              onClick={() => setActiveTab(tab)}
            >
              {tabLabel(tab)}
            </button>
          ))}
        </div>

        {/* Proposal list */}
        <div className="flex-1 overflow-y-auto">
          {proposals.length === 0 && (
            <div className="p-4 text-sm text-muted-foreground text-center">
              {t('governance:no_proposals')}
            </div>
          )}
          {proposals.map((p) => (
            <button
              key={p.proposal_id}
              className={`w-full text-left p-3 border-b transition-colors hover:bg-muted/50 ${
                selected?.proposal_id === p.proposal_id
                  ? 'bg-muted border-l-2 border-l-primary'
                  : ''
              }`}
              onClick={() => fetchDetail(p.proposal_id)}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium truncate max-w-[70%]">
                  {p.resource_name}
                </span>
                <Badge className={`text-[10px] ${SEVERITY_BADGE[p.severity] ?? ''}`}>
                  {p.severity}
                </Badge>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span className={CONFIDENCE_COLOR(p.confidence)}>
                  {(p.confidence * 100).toFixed(0)}%
                </span>
                <span>·</span>
                <span className="truncate">{p.agent_id}</span>
                <ChevronRight className="h-3 w-3 ml-auto opacity-50" />
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Right panel — detail */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            {t('governance:select_proposal')}
          </div>
        ) : (
          <>
            {/* Header */}
            <div className="p-4 border-b">
              <div className="flex items-center gap-2 mb-1">
                <h2 className="text-lg font-semibold">{selected.resource_name}</h2>
                <Badge className={SEVERITY_BADGE[selected.severity] ?? ''}>
                  {selected.severity}
                </Badge>
              </div>
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <span>{selected.policy_name}</span>
                <span>·</span>
                <span>{selected.agent_id} v{selected.agent_version}</span>
                <span>·</span>
                <span className={CONFIDENCE_COLOR(selected.confidence)}>
                  {t('governance:confidence')}: {(selected.confidence * 100).toFixed(0)}%
                </span>
              </div>
            </div>

            {/* Scrollable content */}
            <div className="flex-1 overflow-y-auto p-4 space-y-6">
              {error && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>{t('common:error')}</AlertTitle>
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}

              {/* Diff */}
              <div>
                <ProposalDiff preState={preState} proposedState={proposedState} />
              </div>

              {/* SQL */}
              <div>
                <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wide mb-2">
                  {t('governance:proposed_sql')}
                </h3>
                <pre className="bg-muted rounded-lg p-4 text-sm font-mono overflow-x-auto">
                  {selected.proposed_sql}
                </pre>
              </div>

              {/* Context (collapsible) */}
              {selected.context_json && (
                <details>
                  <summary className="text-sm font-medium text-muted-foreground uppercase tracking-wide cursor-pointer">
                    {t('governance:agent_context')}
                  </summary>
                  <pre className="bg-muted rounded-lg p-4 text-sm font-mono mt-2 overflow-x-auto">
                    {JSON.stringify(context, null, 2)}
                  </pre>
                </details>
              )}

              {/* Review history */}
              {selected.review_history.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wide mb-2">
                    {t('governance:review_history')}
                  </h3>
                  <div className="space-y-2">
                    {selected.review_history.map((r) => (
                      <div key={r.review_id} className="flex items-start gap-2 text-sm border-l-2 border-muted pl-3">
                        <div>
                          <span className="font-medium">{r.reviewer}</span>
                          <span className="text-muted-foreground"> — {r.decision}</span>
                          {r.reassigned_to && (
                            <span className="text-muted-foreground"> → {r.reassigned_to}</span>
                          )}
                          {r.reasoning && (
                            <p className="text-muted-foreground mt-0.5">{r.reasoning}</p>
                          )}
                          <p className="text-xs text-muted-foreground">
                            {new Date(r.reviewed_at).toLocaleString()}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Action bar — sticky bottom */}
            {selected.status === 'pending_review' && (
              <div className="border-t p-4 flex gap-3 justify-end">
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setReviewDialog('rejected')}
                >
                  <X className="h-4 w-4 mr-1" />
                  {t('governance:action_reject')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setReviewDialog('reassigned')}
                >
                  <ArrowRightLeft className="h-4 w-4 mr-1" />
                  {t('governance:action_reassign')}
                </Button>
                <Button
                  size="sm"
                  onClick={() => setReviewDialog('approved')}
                >
                  <Check className="h-4 w-4 mr-1" />
                  {t('governance:action_approve')}
                </Button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Review confirmation dialog */}
      <AlertDialog open={!!reviewDialog} onOpenChange={(o) => !o && setReviewDialog(null)}>
        <AlertDialogContent>
          <AlertDialogTitle>
            {reviewDialog === 'approved' && t('governance:confirm_approve')}
            {reviewDialog === 'rejected' && t('governance:confirm_reject')}
            {reviewDialog === 'reassigned' && t('governance:confirm_reassign')}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {reviewDialog === 'approved' && t('governance:confirm_approve_desc')}
            {reviewDialog === 'rejected' && t('governance:confirm_reject_desc')}
            {reviewDialog === 'reassigned' && t('governance:confirm_reassign_desc')}
          </AlertDialogDescription>

          <div className="space-y-3 mt-2">
            {reviewDialog === 'reassigned' && (
              <Input
                placeholder={t('governance:reassign_to_placeholder')}
                value={reassignTo}
                onChange={(e) => setReassignTo(e.target.value)}
              />
            )}
            <Textarea
              placeholder={t('governance:reasoning_placeholder')}
              value={reasoning}
              onChange={(e) => setReasoning(e.target.value)}
              rows={3}
            />
          </div>

          <div className="flex gap-2 justify-end mt-2">
            <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleReview}
              disabled={reviewDialog === 'rejected' && !reasoning.trim()}
              className={
                reviewDialog === 'rejected'
                  ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90'
                  : ''
              }
            >
              {reviewDialog === 'approved' && t('governance:action_approve')}
              {reviewDialog === 'rejected' && t('governance:action_reject')}
              {reviewDialog === 'reassigned' && t('governance:action_reassign')}
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

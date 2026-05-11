import type { Decision, DecisionCategory } from './types'

interface Props {
  decision: Decision | null
  auditLogged: boolean
  searchPath: 'vector' | 'sql' | null
  candidateCount: number
  isRunning: boolean
}

const CATEGORY_STYLES: Record<DecisionCategory, { badge: string; label: string; border: string; bg: string }> = {
  EXACT:           { badge: 'text-green-400',  label: 'EXACT MATCH',        border: 'border-green-800',  bg: 'bg-green-950/20' },
  HIGH_CONFIDENCE: { badge: 'text-green-300',  label: 'HIGH CONFIDENCE',    border: 'border-green-900',  bg: 'bg-green-950/10' },
  LOW_CONFIDENCE:  { badge: 'text-amber-400',  label: 'LOW CONFIDENCE',     border: 'border-amber-900',  bg: 'bg-amber-950/20' },
  NO_MATCH:        { badge: 'text-red-400',    label: 'NO MATCH',           border: 'border-red-900',    bg: 'bg-red-950/20'   },
}

const RATIONALE_BORDER: Record<DecisionCategory, string> = {
  EXACT:           'border-green-500',
  HIGH_CONFIDENCE: 'border-green-700',
  LOW_CONFIDENCE:  'border-amber-500',
  NO_MATCH:        'border-red-500',
}

function DetailRow({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex justify-between items-baseline py-1.5 border-b border-row/50 text-[11px]">
      <span className="text-gray-500">{label}</span>
      <span className={`font-medium text-right max-w-[55%] ${valueClass ?? 'text-gray-300'}`}>{value}</span>
    </div>
  )
}

export function DecisionPanel({ decision, auditLogged, searchPath, candidateCount, isRunning }: Props) {
  if (isRunning && !decision) {
    return (
      <div className="flex items-center justify-center flex-1 text-gray-600 text-sm h-full">
        Evaluating…
      </div>
    )
  }
  if (!decision) {
    return (
      <div className="flex items-center justify-center flex-1 text-gray-700 text-sm h-full">
        Decision will appear here
      </div>
    )
  }

  const style = CATEGORY_STYLES[decision.category]

  return (
    <div className="flex flex-col gap-3 p-3.5 overflow-y-auto">
      <div className={`rounded-lg border px-4 py-3 text-center ${style.border} ${style.bg}`}>
        <div className={`text-lg font-bold tracking-wide ${style.badge}`}>{style.label}</div>
        <div className="text-xs text-gray-400 mt-1">{decision.confidence.toFixed(2)} confidence</div>
      </div>

      <div className="flex flex-col">
        <DetailRow
          label="Matched Account"
          value={decision.account_id ?? '—'}
          valueClass={decision.matched ? 'text-green-400' : 'text-gray-400'}
        />
        <DetailRow label="Candidates reviewed" value={String(decision.candidates_reviewed)} />
        <DetailRow label="Search path" value={searchPath ?? '—'} />
        <div className="flex justify-between items-baseline py-1.5 text-[11px]">
          <span className="text-gray-500">Audit logged</span>
          <span className={auditLogged ? 'text-green-400 font-medium' : 'text-gray-600'}>
            {auditLogged ? '✓' : '…'}
          </span>
        </div>
      </div>

      {decision.rationale && (
        <div className={`bg-panel rounded-md p-3 text-[11px] text-gray-400 leading-relaxed border-l-2 ${RATIONALE_BORDER[decision.category]}`}>
          {decision.rationale}
        </div>
      )}
    </div>
  )
}

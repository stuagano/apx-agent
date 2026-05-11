import type { TraceStep } from './types'

interface Props {
  steps: TraceStep[]
  isRunning: boolean
}

const TOOL_COLORS: Record<string, { dot: string; text: string }> = {
  normalize_record:    { dot: 'bg-blue-400',   text: 'text-blue-400' },
  vector_search:       { dot: 'bg-green-400',  text: 'text-green-400' },
  sql_search:          { dot: 'bg-purple-400', text: 'text-purple-400' },
  evaluate_candidates: { dot: 'bg-amber-400',  text: 'text-amber-400' },
  log_decision:        { dot: 'bg-gray-400',   text: 'text-gray-400' },
}

function msLabel(ms: number | null): string {
  if (ms === null) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

export function TracePanel({ steps, isRunning }: Props) {
  if (steps.length === 0 && !isRunning) return null

  return (
    <div className="border-t border-row px-4 py-2.5 flex-shrink-0">
      <div className="flex items-start gap-0">
        {steps.map((step, i) => {
          const colors = TOOL_COLORS[step.tool] ?? { dot: 'bg-gray-500', text: 'text-gray-400' }
          const isLast = i === steps.length - 1
          return (
            <div key={i} className="flex items-start gap-0 min-w-0">
              {/* Step */}
              <div className="flex flex-col items-start min-w-0 max-w-[200px]">
                <div className="flex items-center gap-1.5">
                  {step.status === 'running' ? (
                    <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${colors.dot} animate-pulse`} />
                  ) : (
                    <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${colors.dot}`} />
                  )}
                  <span className={`text-[10px] font-semibold whitespace-nowrap ${colors.text}`}>
                    {step.label}
                  </span>
                  {step.ms !== null && (
                    <span className="text-[9px] text-gray-700 whitespace-nowrap">{msLabel(step.ms)}</span>
                  )}
                </div>
                {step.detail && (
                  <p className="text-[9px] text-gray-600 mt-0.5 ml-3 leading-tight line-clamp-2">
                    {step.detail}
                  </p>
                )}
              </div>
              {/* Connector */}
              {!isLast && (
                <div className="flex items-center mx-2 mt-[5px] flex-shrink-0">
                  <div className="w-4 h-px bg-row" />
                  <span className="text-gray-700 text-[10px]">›</span>
                </div>
              )}
            </div>
          )
        })}
        {isRunning && steps.length === 0 && (
          <span className="text-[10px] text-gray-700 animate-pulse">Starting…</span>
        )}
      </div>
    </div>
  )
}

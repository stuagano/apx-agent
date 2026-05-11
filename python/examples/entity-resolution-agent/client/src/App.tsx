import { useEntityResolution } from './useEntityResolution'
import { StepProgress } from './StepProgress'
import { InputPanel } from './InputPanel'
import { CandidatesPanel } from './CandidatesPanel'
import { DecisionPanel } from './DecisionPanel'
import { TracePanel } from './TracePanel'

export default function App() {
  const { matchState, isRunning, run } = useEntityResolution()

  return (
    <div className="flex flex-col h-screen bg-surface text-gray-300 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-row flex-shrink-0">
        <span className="text-xs font-semibold uppercase tracking-widest text-gray-400">
          Entity Resolution
        </span>
        <StepProgress step={matchState.step} />
        <span className="text-[10px] text-gray-700 uppercase tracking-widest">Demo Mode</span>
      </div>

      {/* Three panels */}
      <div className="flex flex-1 overflow-hidden min-h-0">
        {/* Left — Input */}
        <div className="w-[27%] flex flex-col border-r border-row overflow-hidden">
          <div className="px-3.5 py-2.5 border-b border-row flex-shrink-0">
            <span className="text-[10px] font-semibold uppercase tracking-widest text-gray-500">
              Applicant Input
            </span>
          </div>
          <div className="flex-1 overflow-y-auto">
            <InputPanel onRun={run} isRunning={isRunning} />
          </div>
        </div>

        {/* Middle — Candidates */}
        <div className="w-[44%] flex flex-col border-r border-row overflow-hidden">
          <CandidatesPanel
            candidates={matchState.candidates}
            searchPath={matchState.searchPath}
            normalizeResult={matchState.normalizeResult}
            isRunning={isRunning}
          />
        </div>

        {/* Right — Decision */}
        <div className="w-[29%] flex flex-col overflow-hidden">
          <div className="px-3.5 py-2.5 border-b border-row flex-shrink-0">
            <span className="text-[10px] font-semibold uppercase tracking-widest text-gray-500">
              Decision
            </span>
          </div>
          <div className="flex-1 overflow-y-auto">
            <DecisionPanel
              decision={matchState.decision}
              auditLogged={matchState.auditLogged}
              searchPath={matchState.searchPath}
              candidateCount={matchState.candidates.length}
              isRunning={isRunning}
            />
          </div>
        </div>
      </div>

      {/* Agent trace — shows tool call chain as it happens */}
      <TracePanel steps={matchState.traceSteps} isRunning={isRunning} />
    </div>
  )
}

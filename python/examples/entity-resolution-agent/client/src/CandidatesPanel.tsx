import type { Candidate, MatchState } from './types'

interface Props {
  candidates: Candidate[]
  searchPath: 'vector' | 'sql' | null
  normalizeResult: MatchState['normalizeResult']
  isRunning: boolean
}

function ScoreBar({ score }: { score: number | null }) {
  if (score === null) return <span className="text-xs text-gray-600">—</span>
  const pct = Math.round(score * 100)
  const colorClass = score >= 0.75 ? 'bg-green-400' : score >= 0.70 ? 'bg-amber-400' : 'bg-red-400'
  const textClass = score >= 0.75 ? 'text-green-400' : score >= 0.70 ? 'text-amber-400' : 'text-red-400'
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-14 h-1 rounded bg-row overflow-hidden">
        <div className={`h-full rounded ${colorClass}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[11px] font-semibold min-w-[28px] ${textClass}`}>{score.toFixed(2)}</span>
    </div>
  )
}

export function CandidatesPanel({ candidates, searchPath, normalizeResult, isRunning }: Props) {
  const isEmpty = !isRunning && candidates.length === 0

  return (
    <div className="flex flex-col overflow-hidden h-full">
      <div className="flex flex-col border-b border-row flex-shrink-0">
        <div className="flex items-center justify-between px-3.5 py-2.5">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-gray-500">Candidates</span>
          {searchPath && (
            <div className="flex items-center gap-1.5 text-[10px] text-gray-400 border border-row rounded px-2 py-0.5">
              <div className={`w-1.5 h-1.5 rounded-full ${searchPath === 'vector' ? 'bg-green-400' : 'bg-purple-400'}`} />
              {searchPath === 'vector' ? 'Vector Search' : 'SQL Search'} · {candidates.length} results
            </div>
          )}
        </div>
        {normalizeResult && (
          <div className="px-3.5 pb-2 flex items-center gap-2 flex-wrap">
            <span className="text-[9px] uppercase tracking-widest text-gray-700">Normalized</span>
            <span className="text-[10px] text-gray-400 font-medium">{normalizeResult.name}</span>
            <span className="text-gray-700 text-[10px]">·</span>
            <span className="text-[10px] text-gray-500">{normalizeResult.address}</span>
            <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded ${normalizeResult.strategy === 'vector' ? 'bg-green-900/50 text-green-400' : 'bg-purple-900/50 text-purple-400'}`}>
              {normalizeResult.strategy === 'vector' ? 'Vector path' : 'SQL path'}
            </span>
          </div>
        )}
      </div>

      {isRunning && candidates.length === 0 ? (
        <div className="flex items-center justify-center flex-1 text-gray-600 text-sm">
          Searching…
        </div>
      ) : isEmpty ? (
        <div className="flex items-center justify-center flex-1 text-gray-700 text-sm">
          Run a match to see candidates
        </div>
      ) : (
        <div className="overflow-y-auto flex-1">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr>
                <th className="px-3 py-2 text-[9px] uppercase tracking-wider text-gray-600 border-b border-row w-6">#</th>
                <th className="px-2 py-2 text-[9px] uppercase tracking-wider text-gray-600 border-b border-row">Account</th>
                <th className="px-2 py-2 text-[9px] uppercase tracking-wider text-gray-600 border-b border-row">Acct #</th>
                <th className="px-2 py-2 text-[9px] uppercase tracking-wider text-gray-600 border-b border-row">Score</th>
                <th className="px-2 py-2 border-b border-row w-16"></th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => {
                const isBest = i === 0 && c.score !== null && c.score >= 0.70
                return (
                  <tr
                    key={c.account_id}
                    className={isBest ? 'bg-green-950/20 border-l-2 border-green-500' : ''}
                  >
                    <td className="px-3 py-2 text-[11px] text-gray-600">{i + 1}</td>
                    <td className="px-2 py-2">
                      <div className="text-[12px] font-medium text-gray-200">{c.name}</div>
                      <div className="text-[10px] text-gray-500 mt-0.5">{c.address}</div>
                    </td>
                    <td className="px-2 py-2 text-[10px] text-gray-500">{c.account_number || '—'}</td>
                    <td className="px-2 py-2"><ScoreBar score={c.score} /></td>
                    <td className="px-2 py-2">
                      {isBest && (
                        <span className="text-[9px] font-semibold bg-green-900/50 text-green-400 px-1.5 py-0.5 rounded">
                          MATCH
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

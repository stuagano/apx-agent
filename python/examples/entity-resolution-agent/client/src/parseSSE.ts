import type { SSEAction, Step, Candidate, Decision } from './types'

const TOOL_STEP_MAP: Record<string, Step> = {
  normalize_record: 'normalize',
  vector_search: 'search',
  sql_search: 'search',
  evaluate_candidates: 'evaluate',
  log_decision: 'decide',
}

type TraceEntry = { name: string; args: Record<string, unknown>; result: string; ms: number }

function summarizeTool(entry: TraceEntry): string {
  try {
    const result = JSON.parse(entry.result)
    if (entry.name === 'normalize_record') {
      return `"${result.name}" · ${result.address} · strategy: ${result.strategy}`
    }
    if (entry.name === 'vector_search' || entry.name === 'sql_search') {
      const count = result.candidates?.length ?? 0
      const top = result.candidates?.[0]
      return top
        ? `${count} candidates · top: "${top.name}" (${top.score != null ? top.score.toFixed(2) : '—'})`
        : `${count} candidates`
    }
    if (entry.name === 'evaluate_candidates') {
      const d = result.decision
      return `${d.category} · confidence ${d.confidence?.toFixed(2)} · ${d.account_id ?? 'no match'}`
    }
    if (entry.name === 'log_decision') {
      return result.status ?? 'logged'
    }
  } catch { /* fall through */ }
  return ''
}

export function parseSSEEvent(eventType: string, rawData: string): SSEAction[] {
  try {
    const data: unknown = JSON.parse(rawData)

    if (eventType === 'span.start') {
      const span = data as { type: string; name: string }
      if (span.type === 'tool' && span.name in TOOL_STEP_MAP) {
        return [{ type: 'ADVANCE_STEP', step: TOOL_STEP_MAP[span.name], tool: span.name }]
      }
      return []
    }

    if (eventType === 'tool.trace') {
      const entries: TraceEntry[] = Array.isArray(data) ? data as TraceEntry[] : [data as TraceEntry]
      const actions: SSEAction[] = []

      for (const entry of entries) {
        const detail = summarizeTool(entry)
        actions.push({ type: 'TOOL_DONE', tool: entry.name, detail, ms: entry.ms })

        if (entry.name === 'normalize_record') {
          try {
            const r = JSON.parse(entry.result)
            actions.push({ type: 'SET_NORMALIZE', name: r.name, address: r.address, strategy: r.strategy })
          } catch { /* skip */ }
        } else if (entry.name === 'vector_search' || entry.name === 'sql_search') {
          try {
            const r = JSON.parse(entry.result) as { candidates?: Candidate[] }
            actions.push({
              type: 'SET_CANDIDATES',
              candidates: r.candidates ?? [],
              searchPath: entry.name === 'vector_search' ? 'vector' : 'sql',
            })
          } catch { /* skip */ }
        } else if (entry.name === 'evaluate_candidates') {
          try {
            const r = JSON.parse(entry.result) as { decision: Decision }
            actions.push({ type: 'SET_DECISION', decision: r.decision })
          } catch { /* skip */ }
        } else if (entry.name === 'log_decision') {
          actions.push({ type: 'SET_AUDIT_LOGGED' })
        }
      }
      return actions
    }

    if (eventType === 'response.output_item.done') return [{ type: 'DONE' }]
    if (eventType === 'error') {
      const err = data as { error?: string }
      return [{ type: 'ERROR', message: err.error ?? 'Unknown error' }]
    }
  } catch { /* ignore */ }
  return []
}

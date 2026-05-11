import { useState, useCallback } from 'react'
import type { MatchState, SSEAction, TraceStep } from './types'
import { parseSSEEvent } from './parseSSE'

const TOOL_LABELS: Record<string, string> = {
  normalize_record: 'Normalize record',
  vector_search: 'Vector Search',
  sql_search: 'SQL Search',
  evaluate_candidates: 'Evaluate candidates',
  log_decision: 'Log decision',
}

const INITIAL_STATE: MatchState = {
  step: 'idle',
  candidates: [],
  searchPath: null,
  decision: null,
  auditLogged: false,
  error: null,
  traceSteps: [],
  normalizeResult: null,
}

function applyAction(state: MatchState, action: SSEAction): MatchState {
  switch (action.type) {
    case 'ADVANCE_STEP': {
      const running: TraceStep = {
        tool: action.tool,
        label: TOOL_LABELS[action.tool] ?? action.tool,
        detail: '',
        ms: null,
        status: 'running',
      }
      return { ...state, step: action.step, traceSteps: [...state.traceSteps, running] }
    }
    case 'TOOL_DONE': {
      const updated = state.traceSteps.map(t =>
        t.tool === action.tool && t.status === 'running'
          ? { ...t, detail: action.detail, ms: action.ms, status: 'done' as const }
          : t
      )
      return { ...state, traceSteps: updated }
    }
    case 'SET_NORMALIZE':
      return { ...state, normalizeResult: { name: action.name, address: action.address, strategy: action.strategy } }
    case 'SET_CANDIDATES':
      return { ...state, candidates: action.candidates, searchPath: action.searchPath }
    case 'SET_DECISION':
      return { ...state, decision: action.decision }
    case 'SET_AUDIT_LOGGED':
      return { ...state, auditLogged: true, step: 'done' }
    case 'DONE':
      return { ...state, step: 'done' }
    case 'ERROR':
      return { ...state, step: 'error', error: action.message }
  }
}

export function useEntityResolution() {
  const [matchState, setMatchState] = useState<MatchState>(INITIAL_STATE)
  const [isRunning, setIsRunning] = useState(false)

  const run = useCallback(async (name: string, address: string, accountNumber: string) => {
    setMatchState(INITIAL_STATE)
    setIsRunning(true)

    const prompt = `Match this applicant: name="${name}", address="${address}", account_number="${accountNumber}"`

    let resp: Response
    try {
      resp = await fetch('/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stream: true, input: [{ role: 'user', content: prompt }] }),
      })
    } catch (e) {
      setMatchState(s => ({ ...s, step: 'error', error: String(e) }))
      setIsRunning(false)
      return
    }

    if (!resp.ok) {
      setMatchState(s => ({ ...s, step: 'error', error: `HTTP ${resp.status}` }))
      setIsRunning(false)
      return
    }

    const reader = resp.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''

        for (const part of parts) {
          let eventType = 'message'
          let dataLine = ''
          for (const line of part.split('\n')) {
            if (line.startsWith('event: ')) eventType = line.slice(7).trim()
            else if (line.startsWith('data: ')) dataLine = line.slice(6)
          }
          if (!dataLine) continue

          const actions = parseSSEEvent(eventType, dataLine)
          if (actions.length > 0) {
            setMatchState(s => actions.reduce(applyAction, s))
          }
        }
      }
    } finally {
      setIsRunning(false)
    }
  }, [])

  const reset = useCallback(() => {
    setMatchState(INITIAL_STATE)
    setIsRunning(false)
  }, [])

  return { matchState, isRunning, run, reset }
}

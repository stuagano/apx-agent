export type Step = 'idle' | 'normalize' | 'search' | 'evaluate' | 'decide' | 'done' | 'error'

export interface Scenario {
  label: string
  subLabel: string
  tag: string
  tagColor: 'green' | 'yellow' | 'blue' | 'purple'
  name: string
  address: string
  accountNumber: string
}

export interface Candidate {
  account_id: string
  name: string
  address: string
  account_number?: string
  score: number | null
}

export type DecisionCategory = 'EXACT' | 'HIGH_CONFIDENCE' | 'LOW_CONFIDENCE' | 'NO_MATCH'

export interface Decision {
  matched: boolean
  account_id: string | null
  category: DecisionCategory
  rationale: string
  confidence: number
  candidates_reviewed: number
}

export interface TraceStep {
  tool: string
  label: string
  detail: string
  ms: number | null
  status: 'running' | 'done'
}

export interface MatchState {
  step: Step
  candidates: Candidate[]
  searchPath: 'vector' | 'sql' | null
  decision: Decision | null
  auditLogged: boolean
  error: string | null
  traceSteps: TraceStep[]
  normalizeResult: { name: string; address: string; strategy: 'vector' | 'sql' } | null
}

export type SSEAction =
  | { type: 'ADVANCE_STEP'; step: Step; tool: string }
  | { type: 'SET_CANDIDATES'; candidates: Candidate[]; searchPath: 'vector' | 'sql' }
  | { type: 'SET_DECISION'; decision: Decision }
  | { type: 'SET_AUDIT_LOGGED' }
  | { type: 'DONE' }
  | { type: 'ERROR'; message: string }
  | { type: 'TOOL_DONE'; tool: string; detail: string; ms: number }
  | { type: 'SET_NORMALIZE'; name: string; address: string; strategy: 'vector' | 'sql' }

/**
 * Lightweight agent trace system for apx-agent.
 *
 * Captures the conversation flow through an agent: incoming requests,
 * LLM calls, tool invocations, sub-agent calls, and responses. Stored
 * in a ring buffer and viewable via /_apx/traces.
 *
 * Traces propagate through AsyncLocalStorage alongside OBO headers,
 * so any code in the request path can add spans without explicit passing.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TraceSpan {
  type: 'request' | 'llm' | 'tool' | 'agent_call' | 'response' | 'error';
  name: string;
  startTime: number;
  duration_ms?: number;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
}

export interface Trace {
  id: string;
  agentName: string;
  startTime: number;
  endTime?: number;
  duration_ms?: number;
  spans: TraceSpan[];
  status?: 'in_progress' | 'completed' | 'error';
}

export interface TraceContext {
  trace: Trace;
}

// ---------------------------------------------------------------------------
// Ring buffer storage
// ---------------------------------------------------------------------------

const MAX_TRACES = 200;
const traceBuffer: Trace[] = [];

export function storeTrace(trace: Trace): void {
  traceBuffer.push(trace);
  if (traceBuffer.length > MAX_TRACES) {
    traceBuffer.shift();
  }
}

export function getTraces(): Trace[] {
  return [...traceBuffer].reverse(); // newest first
}

export function getTrace(id: string): Trace | undefined {
  return traceBuffer.find((t) => t.id === id);
}

// ---------------------------------------------------------------------------
// Trace lifecycle
// ---------------------------------------------------------------------------

let idCounter = 0;

export function createTrace(agentName: string): Trace {
  const trace: Trace = {
    id: `tr-${Date.now()}-${++idCounter}`,
    agentName,
    startTime: Date.now(),
    spans: [],
    status: 'in_progress',
  };
  return trace;
}

export function addSpan(trace: Trace, span: Omit<TraceSpan, 'startTime'>): TraceSpan {
  const full: TraceSpan = { ...span, startTime: Date.now() };
  trace.spans.push(full);
  return full;
}

export function endSpan(span: TraceSpan): void {
  span.duration_ms = Date.now() - span.startTime;
}

export function endTrace(trace: Trace, status: 'completed' | 'error' = 'completed'): void {
  trace.endTime = Date.now();
  trace.duration_ms = trace.endTime - trace.startTime;
  trace.status = status;
  storeTrace(trace);
}

// ---------------------------------------------------------------------------
// Context helpers (used with request-context.ts)
// ---------------------------------------------------------------------------

// Re-exported for convenience — the actual AsyncLocalStorage lives in request-context.ts
// Callers use getRequestContext().trace to access the current trace

export function truncate(value: unknown, maxLen = 200): string {
  const s = typeof value === 'string' ? value : JSON.stringify(value);
  if (!s) return '';
  return s.length > maxLen ? s.slice(0, maxLen) + '...' : s;
}

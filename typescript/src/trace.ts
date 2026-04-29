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
  /**
   * Trace id of the calling agent, when this trace was triggered by a
   * cross-agent HTTP call. Set from inbound trace headers. The chat-style
   * detail view uses this to render a "called by" link.
   *
   * Note: span-level parent IDs would be needed for OTel-grade span linkage;
   * we intentionally only track trace-level parentage here (sufficient for
   * the chat-style view).
   */
  parentTraceId?: string;
  parentAgentName?: string;
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

// ---------------------------------------------------------------------------
// Cross-agent trace propagation
// ---------------------------------------------------------------------------

/**
 * HTTP headers used to propagate trace context across cross-agent calls.
 * Both directions: caller sets on outbound request; callee echoes its own
 * trace id on response so the caller can link parent → child.
 */
export const TRACE_ID_HEADER = 'x-apx-trace-id';
export const PARENT_AGENT_HEADER = 'x-apx-parent-agent';

/** Outbound headers to attach to a cross-agent fetch when a trace is active. */
export function traceHeadersOut(trace: Trace | undefined): Record<string, string> {
  if (!trace) return {};
  return {
    [TRACE_ID_HEADER]: trace.id,
    [PARENT_AGENT_HEADER]: trace.agentName,
  };
}

/**
 * Extract parent-trace context from inbound HTTP headers. Express normalizes
 * header names to lowercase; we accept either case to be defensive.
 */
export function traceHeadersIn(headers: Record<string, string | string[] | undefined>): {
  parentTraceId?: string;
  parentAgentName?: string;
} {
  const traceId = headers[TRACE_ID_HEADER] ?? headers[TRACE_ID_HEADER.toUpperCase()];
  const agent = headers[PARENT_AGENT_HEADER] ?? headers[PARENT_AGENT_HEADER.toUpperCase()];
  return {
    parentTraceId: typeof traceId === 'string' ? traceId : undefined,
    parentAgentName: typeof agent === 'string' ? agent : undefined,
  };
}

/** Read the callee's trace id from a cross-agent response, if present. */
export function traceIdFromResponse(response: { headers: { get(name: string): string | null } }): string | undefined {
  return response.headers.get(TRACE_ID_HEADER) ?? undefined;
}

/**
 * Derive a short, human-readable label from an agent URL — used as the span
 * name when the caller doesn't have a friendlier name handy.
 */
export function agentNameFromUrl(url: string): string {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/\.cloud\.databricks\.com$/, '').replace(/\.databricksapps\.com$/, '');
    return host.split('.')[0] || url;
  } catch {
    return url;
  }
}

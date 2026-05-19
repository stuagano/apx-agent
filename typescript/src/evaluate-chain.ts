/**
 * evaluate-chain — eval a multi-agent chain and correlate sub-agent invocations.
 *
 * `evaluate()` (in `./evaluate.js`) runs an evalset through the supervisor's
 * predict path. That captures the supervisor's final answer, but doesn't tell
 * you *which sub-agents handled which prompt* — and in a multi-agent estate
 * that's exactly what you want to know when an eval regresses.
 *
 * `evaluateChain` runs the eval the same way, then post-processes the MLflow
 * traces emitted during the run to correlate per-prompt outcomes with the
 * sub-agent endpoints that fired. The audit-attribute schema
 * (``apx.subagent.endpoint``, ``apx.subagent.name``, ``apx.tool.name`` when
 * sub-agents are wired as agent-as-tool) provides the linkage.
 *
 * Both `mlflowEvaluate` and `traceSearch` are injectable — the caller wires
 * whatever they have (REST call, mlflow JS shim, fake for tests). The trace
 * shape duck-types both DataFrame-row dicts and Trace-object instances, the
 * same way `_normaliseTrace` in `./trace-export.js` does.
 *
 * Returns a `ChainEvalReport` per prompt: the supervisor's response, the list
 * of sub-agents invoked, the latencies, and the raw `evaluate()` result for
 * downstream consumers.
 */

import { z } from 'zod';
import {
  evaluate,
  extractMessages,
  type MlflowEvaluateFn,
} from './evaluate.js';
import type { RunViaSDKFn } from './run-once.js';
import type { AgentConfig, AgentExports } from './agent/plugin.js';
import type { RawTrace, TraceAttrs, TraceLike } from './trace-export.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Per-prompt chain-evaluation result — frozen. */
export interface ChainCaseResult {
  readonly request: string;
  readonly response: string;
  readonly subAgentsInvoked: readonly string[];
  readonly toolCalls: readonly string[];
  readonly durationMs: number | null;
  readonly traceId: string | null;
}

/** Full chain-evaluation report — frozen. */
export interface ChainEvalReport {
  readonly cases: readonly ChainCaseResult[];
  readonly rawEvalResult?: unknown;
  readonly subAgentCoverage: Record<string, number>;
}

/** Caller-supplied trace search — same shape as `./trace-export.js`. */
export type TraceSearchFn = (opts: {
  experimentNames: string[];
  maxResults: number;
}) => Promise<unknown[]>;

// ---------------------------------------------------------------------------
// EvaluateChainOptions
// ---------------------------------------------------------------------------

const OptionsSchema = z.object({
  model: z.string().min(1, 'model is required and must be non-empty'),
  experiment: z.string().min(1, 'experiment is required for trace correlation'),
  userToken: z.string().min(1).optional(),
  workspaceHost: z.string().min(1).optional(),
  lookbackTraces: z.number().int().positive().optional(),
});

export interface EvaluateChainOptions {
  /**
   * The supervisor agent. Duck-typed: accepts `AgentExports`, a raw
   * `AgentConfig`, or any shape with `.getConfig()`/`.getTools()` accessors
   * — same as `evaluate()` accepts.
   */
  agent: AgentExports | AgentConfig | unknown;
  /** LLM endpoint name for the supervisor (e.g. 'databricks-claude-sonnet-4-6'). */
  model: string;
  /** Eval dataset — passed straight through to `mlflowEvaluate`. */
  evalset: unknown;
  /**
   * MLflow experiment to log into and read traces from. Required so trace
   * correlation works — without it, this helper can't distinguish the
   * supervisor's traces from unrelated ones.
   */
  experiment: string;
  /** Optional scorer list forwarded to `evaluate()`. */
  scorers?: unknown[];
  /** Optional OBO token threaded through to `evaluate()`. */
  userToken?: string;
  /** Optional workspace host paired with `userToken`. */
  workspaceHost?: string;
  /** How many recent traces to fetch when correlating. Default 50. */
  lookbackTraces?: number;
  /** Required mlflow.genai.evaluate injectable — same as `evaluate()`. */
  mlflowEvaluate: MlflowEvaluateFn;
  /** Injectable runner. Defaults to the real `runViaSDK` inside `evaluate()`. */
  runViaSDK?: RunViaSDKFn;
  /** Required trace-search injectable. */
  traceSearch: TraceSearchFn;
}

// ---------------------------------------------------------------------------
// Helpers — exported for direct use + parity with Python's _* helpers
// ---------------------------------------------------------------------------

/**
 * Extract the request text from each evalset row.
 *
 * Tolerates dict / list / DataFrame shapes and the same key fallbacks
 * `evaluate` uses (request / input / prompt / query / question). The Python
 * implementation reuses `extractMessages`'s logic but emits a flat list of
 * request strings instead of message dicts. Bare strings pass through; rows
 * whose extracted message has a non-user role or empty content are skipped.
 */
export function requestTextsFromEvalset(evalset: unknown): string[] {
  let rows: unknown[];

  // DataFrame-like → to_dict(orient='records')
  if (
    evalset !== null &&
    typeof evalset === 'object' &&
    typeof (evalset as { to_dict?: unknown }).to_dict === 'function'
  ) {
    try {
      const fn = (evalset as { to_dict: (orient?: string) => unknown }).to_dict;
      const result = fn.call(evalset, 'records');
      rows = Array.isArray(result) ? result : [];
    } catch {
      rows = [];
    }
  } else if (Array.isArray(evalset)) {
    rows = evalset;
  } else {
    rows = [];
  }

  const out: string[] = [];
  for (const r of rows) {
    if (typeof r === 'string') {
      out.push(r);
      continue;
    }
    if (r === null || typeof r !== 'object') continue;
    const obj = r as Record<string, unknown>;
    for (const key of ['request', 'input', 'prompt', 'query', 'question']) {
      const val = obj[key];
      if (val !== undefined && val !== null && val !== '') {
        out.push(String(val));
        break;
      }
    }
  }
  return out;
}

/** Pull the trace-level attributes off either dict-row or Trace-object shape. */
function traceAttrs(t: unknown): TraceAttrs {
  if (t === null || typeof t !== 'object') return {};
  if (isDictRow(t)) {
    const row = t as Record<string, unknown>;
    const tags = row['tags'];
    const attrs = row['attributes'];
    if (tags && typeof tags === 'object') return tags as TraceAttrs;
    if (attrs && typeof attrs === 'object') return attrs as TraceAttrs;
    return {};
  }
  const info = (t as TraceLike).info;
  if (!info) return {};
  const tags = info.tags;
  return tags && typeof tags === 'object' ? { ...tags } : {};
}

/** Pull child spans off either shape. */
function traceSpans(t: unknown): SpanLike[] {
  if (t === null || typeof t !== 'object') return [];
  if (isDictRow(t)) {
    const row = t as Record<string, unknown>;
    const spans = row['spans'];
    return Array.isArray(spans) ? (spans as SpanLike[]) : [];
  }
  const data = (t as TraceLike).data;
  if (!data) return [];
  const spans = data.spans;
  return spans ? (Array.from(spans) as SpanLike[]) : [];
}

/** Pull a usable trace id from either shape. */
function traceId(t: unknown): string | null {
  if (t === null || typeof t !== 'object') return null;
  if (isDictRow(t)) {
    const row = t as Record<string, unknown>;
    const id = row['trace_id'] ?? row['request_id'];
    return id === null || id === undefined || id === '' ? null : String(id);
  }
  const info = (t as TraceLike).info;
  if (!info) return null;
  const id = info.trace_id ?? info.request_id;
  return id === null || id === undefined || id === '' ? null : String(id);
}

/** Pull execution time (ms) off either shape. */
function traceDurationMs(t: unknown): number | null {
  if (t === null || typeof t !== 'object') return null;
  let raw: unknown;
  if (isDictRow(t)) {
    raw = (t as Record<string, unknown>)['execution_time_ms'];
  } else {
    const info = (t as TraceLike).info;
    raw = info?.execution_time_ms;
  }
  return coerceInt(raw);
}

function coerceInt(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === 'bigint') return Number(value);
  if (typeof value === 'string') {
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : null;
  }
  return null;
}

interface SpanLike {
  attributes?: TraceAttrs | null;
  inputs?: unknown;
  outputs?: unknown;
}

/** Same duck-typing rule as trace-export.ts: anything with `info` is a Trace. */
function isDictRow(t: unknown): boolean {
  if (t === null || typeof t !== 'object') return false;
  if ('info' in (t as object)) return false;
  return true;
}

/**
 * Best-effort match: look for the request text in the root span's inputs.
 *
 * Mirrors Python's `_trace_matches_request` — a fuzzy contains check on the
 * first span's inputs once they've been stringified. The root span carries
 * the predict input, so this catches the messages array verbatim.
 */
function traceMatchesRequest(spans: SpanLike[], requestText: string): boolean {
  if (spans.length === 0) return false;
  const root = spans[0];
  const inputs = root.inputs;
  if (inputs === null || inputs === undefined) return false;
  const haystack =
    typeof inputs === 'string' ? inputs : stringifyForMatch(inputs);
  return haystack.includes(requestText);
}

function stringifyForMatch(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Pull the final assistant text out of the trace's root span outputs. */
function responseTextFromTrace(spans: SpanLike[]): string {
  if (spans.length === 0) return '';
  const root = spans[0];
  const outputs = root.outputs;
  if (outputs === null || outputs === undefined || typeof outputs !== 'object') {
    return '';
  }
  const messages = (outputs as { messages?: unknown }).messages;
  if (!Array.isArray(messages)) return '';
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m === null || typeof m !== 'object') continue;
    const role = (m as { role?: unknown }).role;
    if (role === 'assistant') {
      const content = (m as { content?: unknown }).content;
      if (content !== null && content !== undefined && content !== '') {
        return String(content);
      }
    }
  }
  return '';
}

/**
 * Match a request to a trace and pull the chain-relevant fields off it.
 *
 * Walks `traces` in order, returns the first match (and harvests sub-agent
 * endpoints + tool names off all child spans). Returns `null` when no trace
 * matches.
 */
export function buildCaseFromTrace(
  requestText: string,
  traces: unknown[] | null | undefined,
): ChainCaseResult | null {
  if (!traces) return null;
  for (const t of traces) {
    const spans = traceSpans(t);
    if (!traceMatchesRequest(spans, requestText)) continue;

    const subAgents: string[] = [];
    const toolCalls: string[] = [];
    for (const span of spans) {
      const spanAttrs: TraceAttrs =
        span.attributes && typeof span.attributes === 'object'
          ? span.attributes
          : {};
      if ('apx.subagent.endpoint' in spanAttrs) {
        subAgents.push(String(spanAttrs['apx.subagent.endpoint']));
      } else if ('apx.subagent.name' in spanAttrs) {
        subAgents.push(String(spanAttrs['apx.subagent.name']));
      }
      if ('apx.tool.name' in spanAttrs) {
        toolCalls.push(String(spanAttrs['apx.tool.name']));
      }
    }

    return Object.freeze({
      request: requestText,
      response: responseTextFromTrace(spans),
      subAgentsInvoked: Object.freeze([...subAgents]),
      toolCalls: Object.freeze([...toolCalls]),
      durationMs: traceDurationMs(t),
      traceId: traceId(t),
    });
  }
  return null;
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Run an evalset through the supervisor agent and correlate sub-agent
 * invocations against the MLflow traces emitted during the run.
 *
 * Calls `evaluate()` (which compiles the agent and runs each evalset row
 * through `predict`), then queries the injected `traceSearch` for traces
 * emitted under `experiment` and pulls `apx.subagent.*` / `apx.tool.name`
 * attributes off each trace's spans to build the per-case chain report.
 *
 * `traceSearch` failures yield an empty `cases` array with a `console.warn`
 * — the eval itself succeeded, so the caller still gets the raw result.
 *
 * @throws ZodError when `model` or `experiment` is missing/empty.
 */
export async function evaluateChain(
  opts: EvaluateChainOptions,
): Promise<ChainEvalReport> {
  OptionsSchema.parse({
    model: opts.model,
    experiment: opts.experiment,
    userToken: opts.userToken,
    workspaceHost: opts.workspaceHost,
    lookbackTraces: opts.lookbackTraces,
  });

  if (typeof opts.mlflowEvaluate !== 'function') {
    throw new Error(
      'evaluateChain requires an `mlflowEvaluate` injectable.',
    );
  }
  if (typeof opts.traceSearch !== 'function') {
    throw new Error(
      'evaluateChain requires a `traceSearch` injectable.',
    );
  }

  const rawEvalResult = await evaluate({
    agent: opts.agent,
    model: opts.model,
    evalset: opts.evalset,
    scorers: opts.scorers,
    userToken: opts.userToken,
    workspaceHost: opts.workspaceHost,
    experiment: opts.experiment,
    mlflowEvaluate: opts.mlflowEvaluate,
    runViaSDK: opts.runViaSDK,
  });

  const lookback = opts.lookbackTraces ?? 50;
  let rawTraces: unknown[] = [];
  try {
    const result = await opts.traceSearch({
      experimentNames: [opts.experiment],
      maxResults: lookback,
    });
    rawTraces = Array.isArray(result) ? result : [];
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      `evaluateChain: traceSearch failed: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
    return Object.freeze({
      cases: Object.freeze([] as readonly ChainCaseResult[]),
      rawEvalResult,
      subAgentCoverage: Object.freeze({}) as Record<string, number>,
    });
  }

  const traces = normaliseTracesIter(rawTraces);
  const requests = requestTextsFromEvalset(opts.evalset);

  const cases: ChainCaseResult[] = [];
  for (const req of requests) {
    const c = buildCaseFromTrace(req, traces);
    if (c !== null) cases.push(c);
  }

  const coverage: Record<string, number> = {};
  for (const c of cases) {
    for (const sub of c.subAgentsInvoked) {
      coverage[sub] = (coverage[sub] ?? 0) + 1;
    }
  }

  return Object.freeze({
    cases: Object.freeze([...cases]) as readonly ChainCaseResult[],
    rawEvalResult,
    subAgentCoverage: Object.freeze({ ...coverage }),
  });
}

/**
 * Handle the DataFrame-like return shape that `mlflow.search_traces` produces
 * in Python. If `traceSearch` returns something with `to_dict`, unwrap it
 * to records — otherwise pass through.
 */
function normaliseTracesIter(traces: unknown[]): unknown[] {
  // The injectable in TS already returns an array, but a caller forwarding
  // a Python-shape mock might hand back a single dataframe-like with
  // `to_dict`. Mirror the Python branch for parity.
  if (
    traces.length === 1 &&
    traces[0] !== null &&
    typeof traces[0] === 'object' &&
    typeof (traces[0] as { to_dict?: unknown }).to_dict === 'function'
  ) {
    try {
      const fn = (traces[0] as { to_dict: (orient?: string) => unknown }).to_dict;
      const records = fn.call(traces[0], 'records');
      return Array.isArray(records) ? records : [];
    } catch {
      return [];
    }
  }
  return traces;
}

// Re-export the upstream types the caller may want to assert against.
export type { RawTrace };
// Suppress unused-import warning for `extractMessages` re-used by tests.
export { extractMessages };

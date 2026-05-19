/**
 * Audit log attributes — a stable span-attribute schema for every apx-agent trace.
 *
 * Every framework-emitted span carries a consistent set of ``apx.*``
 * attributes so downstream consumers (watchdog, compliance dashboards,
 * ad-hoc SQL over the traces table) can query without parsing
 * agent-specific schemas.
 *
 * The attribute keys live as constants on ``AuditAttrs`` — code that
 * writes attributes uses these constants, code that reads them queries
 * the same strings. No ad-hoc strings sprinkled through the codebase.
 *
 * The schema is intentionally narrow: audit attributes describe *what
 * happened* (operation, identity, scope, decision) without logging raw
 * inputs or outputs. Use ``hashForAudit`` to record presence /
 * fingerprint without exfiltrating content.
 */

import { createHash } from 'node:crypto';

// ---------------------------------------------------------------------------
// Standard attribute keys
// ---------------------------------------------------------------------------

/**
 * Stable attribute-key constants for apx-agent audit logging.
 *
 * Grouped by domain. All keys live under the ``apx.`` namespace.
 * Add new keys here; never use string literals in the codebase
 * when an audit-relevant attribute is being set.
 */
export const AuditAttrs = {
  // Agent identity & scope
  AGENT_NAME: 'apx.agent.name',
  AGENT_VERSION: 'apx.agent.version',
  SESSION_ID: 'apx.session.id',
  OPERATION: 'apx.operation', // predict | predict_stream | tool_call | model_call | sub_agent_call

  // User / OBO identity (no PII — presence flags + hashes only)
  USER_TOKEN_PROVIDED: 'apx.user.token_provided',
  USER_HASH: 'apx.user.hash', // SHA-256(user_id) when known — for correlation without leaking ids

  // Tool calls
  TOOL_NAME: 'apx.tool.name',
  TOOL_UC_FUNCTION: 'apx.tool.uc_function',
  TOOL_INPUT_KEYS: 'apx.tool.input_keys', // comma-separated argument names
  TOOL_INPUT_HASH: 'apx.tool.input_hash',
  TOOL_OUTPUT_TYPE: 'apx.tool.output_type',
  TOOL_OUTPUT_SIZE: 'apx.tool.output_size',
  TOOL_DURATION_MS: 'apx.tool.duration_ms',

  // Model / LLM calls
  MODEL_ENDPOINT: 'apx.model.endpoint',
  MODEL_INPUT_MESSAGES: 'apx.model.input_messages',
  MODEL_INPUT_TOKENS: 'apx.model.input_tokens',
  MODEL_OUTPUT_TOKENS: 'apx.model.output_tokens',
  MODEL_STREAMING: 'apx.model.streaming',

  // Sub-agent dispatch
  SUBAGENT_ENDPOINT: 'apx.subagent.endpoint',
  SUBAGENT_NAME: 'apx.subagent.name',

  // Resources declared on the agent at call time
  RESOURCE_KINDS: 'apx.resources.kinds',
  RESOURCE_COUNT: 'apx.resources.count',

  // Watchdog runtime decisions
  WATCHDOG_ACTION: 'apx.watchdog.action', // allow | reject | redact
  WATCHDOG_POLICY_ID: 'apx.watchdog.policy_id',
  WATCHDOG_REASON: 'apx.watchdog.reason',
  WATCHDOG_DOMAIN: 'apx.watchdog.domain',
} as const;

// ---------------------------------------------------------------------------
// Short kwarg → standard key mapping
// ---------------------------------------------------------------------------

/**
 * Translates ``setAuditAttrs(span, { toolName: "x" })`` →
 * ``span.setAttribute("apx.tool.name", "x")``.
 *
 * Keeps call sites readable while the on-wire schema is canonical.
 */
const KWARG_TO_KEY: ReadonlyMap<string, string> = Object.freeze(
  new Map<string, string>([
    ['agentName', AuditAttrs.AGENT_NAME],
    ['agentVersion', AuditAttrs.AGENT_VERSION],
    ['sessionId', AuditAttrs.SESSION_ID],
    ['operation', AuditAttrs.OPERATION],
    ['userTokenProvided', AuditAttrs.USER_TOKEN_PROVIDED],
    ['userHash', AuditAttrs.USER_HASH],
    ['toolName', AuditAttrs.TOOL_NAME],
    ['toolUcFunction', AuditAttrs.TOOL_UC_FUNCTION],
    ['toolInputKeys', AuditAttrs.TOOL_INPUT_KEYS],
    ['toolInputHash', AuditAttrs.TOOL_INPUT_HASH],
    ['toolOutputType', AuditAttrs.TOOL_OUTPUT_TYPE],
    ['toolOutputSize', AuditAttrs.TOOL_OUTPUT_SIZE],
    ['toolDurationMs', AuditAttrs.TOOL_DURATION_MS],
    ['modelEndpoint', AuditAttrs.MODEL_ENDPOINT],
    ['modelInputMessages', AuditAttrs.MODEL_INPUT_MESSAGES],
    ['modelInputTokens', AuditAttrs.MODEL_INPUT_TOKENS],
    ['modelOutputTokens', AuditAttrs.MODEL_OUTPUT_TOKENS],
    ['modelStreaming', AuditAttrs.MODEL_STREAMING],
    ['subagentEndpoint', AuditAttrs.SUBAGENT_ENDPOINT],
    ['subagentName', AuditAttrs.SUBAGENT_NAME],
    ['resourceKinds', AuditAttrs.RESOURCE_KINDS],
    ['resourceCount', AuditAttrs.RESOURCE_COUNT],
    ['watchdogAction', AuditAttrs.WATCHDOG_ACTION],
    ['watchdogPolicyId', AuditAttrs.WATCHDOG_POLICY_ID],
    ['watchdogReason', AuditAttrs.WATCHDOG_REASON],
    ['watchdogDomain', AuditAttrs.WATCHDOG_DOMAIN],
  ]),
);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Minimum surface required to annotate a span. */
export interface SpanLike {
  setAttribute(key: string, value: unknown): void;
}

/** Short-keyed audit fields. Unknown keys throw — typos fail loud. */
export type AuditFields = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Set audit attributes on ``span`` using short kwarg names.
 *
 * Maps each kwarg to its canonical ``AuditAttrs`` key. Empty / ``null`` /
 * ``undefined`` values are skipped so callers can pass optional fields
 * uniformly without scattering ``if x: ...`` checks.
 *
 * Unknown kwarg names throw ``Error`` — typos in attribute names are
 * silent killers in real audit pipelines, so this fails loud.
 *
 * @param span Span-like object with ``setAttribute(key, value)``.
 *   ``null`` / ``undefined`` is accepted as a no-op (matches Python's
 *   ``set_audit_attrs`` for the case where no span is active).
 * @param fields Short-key audit fields.
 */
export function setAuditAttrs(span: SpanLike | null | undefined, fields: AuditFields): void {
  if (span === null || span === undefined) {
    return;
  }
  for (const [kwarg, value] of Object.entries(fields)) {
    if (value === null || value === undefined || value === '') {
      continue;
    }
    const canonical = KWARG_TO_KEY.get(kwarg);
    if (canonical === undefined) {
      throw new Error(
        `setAuditAttrs: unknown kwarg ${JSON.stringify(kwarg)}. ` +
          'Add the key to AuditAttrs and KWARG_TO_KEY in audit.ts, ' +
          'or set the span attribute directly for one-off non-audit attributes.',
      );
    }
    try {
      span.setAttribute(canonical, value);
    } catch {
      // Swallow span errors — matches Python's defensive contract.
      // A broken span must not crash agent execution.
    }
  }
}

/**
 * Hash a value for audit attributes — fingerprint without exfiltrating content.
 *
 * SHA-256 of the value's UTF-8 string representation, truncated to
 * ``length`` hex chars (default 16 — 64 bits of collision space, enough
 * for correlation across traces).
 */
export function hashForAudit(value: unknown, length: number = 16): string {
  const text = stringify(value);
  return createHash('sha256').update(text, 'utf8').digest('hex').slice(0, length);
}

/**
 * Return ``{ typeName, sizeEstimate }`` for a tool output.
 *
 * Used to record ``apx.tool.output_type`` and ``apx.tool.output_size``
 * without storing the raw output.
 */
export function outputSummary(value: unknown): { typeName: string; sizeEstimate: number } {
  const typeName = jsTypeName(value);
  let sizeEstimate: number;
  if (typeof value === 'string') {
    sizeEstimate = value.length;
  } else if (Array.isArray(value)) {
    sizeEstimate = value.length;
  } else if (value !== null && typeof value === 'object') {
    sizeEstimate = Object.keys(value as Record<string, unknown>).length;
  } else {
    sizeEstimate = String(value).length;
  }
  return { typeName, sizeEstimate };
}

/**
 * Return a comma-separated list of input argument names.
 *
 * For ``apx.tool.input_keys``. Records the *shape* of the input
 * (which keys were passed) without the values themselves.
 */
export function inputKeysSummary(args: unknown): string {
  if (args !== null && typeof args === 'object' && !Array.isArray(args)) {
    return Object.keys(args as Record<string, unknown>).sort().join(',');
  }
  if (typeof args === 'string') {
    return '<string>';
  }
  return jsTypeName(args);
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** Stable string form for hashing — mirrors Python's ``str(value)`` semantics
 * for the cases that matter (objects → JSON, primitives → String()). */
function stringify(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** JS equivalent of Python's ``type(value).__name__``. */
function jsTypeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'list';
  const t = typeof value;
  if (t === 'string') return 'str';
  if (t === 'number') return Number.isInteger(value) ? 'int' : 'float';
  if (t === 'boolean') return 'bool';
  if (t === 'object') return 'dict';
  return t;
}

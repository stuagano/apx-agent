/**
 * databricks-watchdog integration (TypeScript port of python/src/apx_agent/_watchdog.py).
 *
 * `databricks-watchdog` is the compliance posture layer for Unity Catalog:
 * declarative cross-domain policies, violation lifecycle tracking, owner
 * accountability, and an MCP tool surface for AI assistants to query and act
 * on governance posture.
 *
 * The integration has three wire-protocol contracts:
 *
 *   1. **Metadata shape**: UC tags on the registered model. Watchdog's
 *      crawler reads tags off the model record. {@link setUcTagsForAgent}
 *      writes them.
 *   2. **Runtime policy decisions**: watchdog's MCP tools.
 *      {@link makeMcpTransport} produces a transport that calls a named MCP
 *      tool with the operation context.
 *   3. **Violation reports**: an INSERT into a watchdog-owned UC Delta
 *      table. {@link makeUcViolationWriter} produces a transport that
 *      handles `violation_report` requests.
 *
 * Three public pieces sit on top of those:
 *
 *   - {@link WatchdogClient} — adapter that dispatches evaluate / report
 *     calls through a pluggable transport. Default transport is a no-op stub.
 *   - {@link WatchdogGuard} — produces callables for the existing apx-agent
 *     hooks (input guardrails, output guardrails, before-tool, before-model).
 *     Reject short-circuits; redact rewrites; allow passes through. Violation
 *     reports fire automatically on reject / redact.
 *   - {@link emitAgentMetadata} — produces the crawler-facing metadata dict.
 *     {@link setUcTagsForAgent} writes it into UC as tags on the registered
 *     model.
 *
 * Async note (port semantics): Python's `_watchdog.py` is sync. In TypeScript
 * every transport and every hook callable is async because the wire calls
 * (MCP HTTP, Statements API SQL) are async. The shape and behaviour are
 * otherwise preserved 1:1.
 */

import { z } from 'zod';
import { setAuditAttrs, type SpanLike } from './audit.js';
import {
  collectResourceSpecs,
  getResources,
  type ResourceSpec,
} from './resources.js';

// ---------------------------------------------------------------------------
// Decision type
// ---------------------------------------------------------------------------

/**
 * A single policy decision returned by watchdog.
 *
 * Frozen at construction — mutation throws in strict mode.
 *
 * Fields:
 *   - `action` — `"allow"` | `"reject"` | `"redact"`. Defaults to `"allow"`
 *     so the no-op stub transport produces a pass-through decision.
 *   - `reason` — human-readable explanation surfaced to the user (on
 *     `"reject"`) or attached as a violation reason.
 *   - `policyId` — watchdog's identifier for the policy that produced this
 *     decision. Pass back when reporting violations so watchdog can aggregate
 *     by policy.
 *   - `domain` — governance domain (`"security"`, `"data_quality"`,
 *     `"cost"`, `"agent"`, ...) for trace attribution.
 *   - `redactedContent` — when `action="redact"`, the rewritten content
 *     (e.g. PII stripped). When absent/empty, callers fall back to the
 *     original content unchanged.
 *   - `metadata` — free-form dict for additional context watchdog wants to
 *     surface (owner email, remediation link, ...).
 */
export interface WatchdogDecision {
  readonly action: 'allow' | 'reject' | 'redact';
  readonly reason?: string;
  readonly policyId?: string;
  readonly domain?: string;
  readonly redactedContent?: string;
  readonly metadata: Readonly<Record<string, unknown>>;
}

/** Construct a frozen {@link WatchdogDecision} with sensible defaults. */
export function makeWatchdogDecision(
  fields: Partial<Omit<WatchdogDecision, 'metadata'>> & { metadata?: Record<string, unknown> } = {},
): WatchdogDecision {
  return Object.freeze({
    action: fields.action ?? 'allow',
    reason: fields.reason,
    policyId: fields.policyId,
    domain: fields.domain,
    redactedContent: fields.redactedContent,
    metadata: Object.freeze({ ...(fields.metadata ?? {}) }),
  }) as WatchdogDecision;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/**
 * Transport callable shape: receives a request object, returns an object the
 * {@link WatchdogClient} parses into a {@link WatchdogDecision}. Plugged in
 * by callers once the watchdog-side wire protocol is pinned down.
 */
export type TransportFn = (request: object) => Promise<object>;

/** Default transport — allow everything, report nothing. */
export const noOpTransport: TransportFn = async () => ({ action: 'allow' });

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function recordDecisionOnSpan(
  decision: WatchdogDecision,
  agentName: string | undefined,
  getActiveSpan: () => SpanLike | null,
): void {
  if (decision.action === 'allow') return;
  const span = getActiveSpan();
  setAuditAttrs(span, {
    watchdogAction: decision.action,
    watchdogReason: decision.reason,
    watchdogPolicyId: decision.policyId,
    watchdogDomain: decision.domain,
    agentName,
  });
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export interface WatchdogClientOptions {
  /**
   * Watchdog HTTP / MCP endpoint URL. Stored for caller reference; the default
   * transport doesn't consult it.
   */
  endpoint?: string;
  /**
   * Transport callable. When omitted, a no-op allow-everything transport is
   * used so this module is safe to import and exercise before watchdog's wire
   * API is finalised.
   */
  transport?: TransportFn;
}

const ClientOptionsSchema = z.object({
  endpoint: z.string().optional(),
  transport: z.unknown().optional(),
});

export interface EvaluateOpts {
  operation: string;
  context?: Record<string, unknown>;
}

/**
 * Adapter for talking to a databricks-watchdog deployment.
 *
 * Subclass to replace {@link evaluate} / {@link reportViolation} entirely
 * when more control is needed.
 */
export class WatchdogClient {
  readonly endpoint: string | undefined;
  private readonly transport: TransportFn;

  constructor(options: WatchdogClientOptions = {}) {
    ClientOptionsSchema.parse({ endpoint: options.endpoint });
    this.endpoint = options.endpoint;
    this.transport = options.transport ?? noOpTransport;
  }

  /**
   * Ask watchdog whether `operation` should proceed.
   *
   * Falls back to `allow` (with a `console.warn`) on transport failure so a
   * watchdog outage doesn't black-hole production traffic — the runtime
   * decides its own fail-open vs fail-closed policy on top of this.
   */
  async evaluate(opts: EvaluateOpts): Promise<WatchdogDecision> {
    const request = {
      operation: opts.operation,
      context: opts.context ?? {},
    };

    let response: unknown;
    try {
      response = await this.transport(request);
    } catch (e) {
      console.warn(
        `[watchdog] transport failed for operation ${opts.operation}: ${e instanceof Error ? e.message : String(e)} — falling back to allow.`,
      );
      return makeWatchdogDecision({
        action: 'allow',
        reason: `watchdog transport error: ${e instanceof Error ? e.message : String(e)}`,
      });
    }

    if (!isPlainObject(response)) {
      console.warn(
        `[watchdog] transport returned ${typeof response}, expected object — allowing.`,
      );
      return makeWatchdogDecision({ action: 'allow' });
    }

    const action = typeof response.action === 'string' ? response.action : 'allow';
    if (action !== 'allow' && action !== 'reject' && action !== 'redact') {
      // Unknown action string — treat as allow so an upstream typo doesn't lock the runtime.
      return makeWatchdogDecision({ action: 'allow' });
    }

    return makeWatchdogDecision({
      action,
      reason: typeof response.reason === 'string' ? response.reason : undefined,
      policyId: typeof response.policy_id === 'string' ? response.policy_id : undefined,
      domain: typeof response.domain === 'string' ? response.domain : undefined,
      redactedContent:
        typeof response.redacted_content === 'string' ? response.redacted_content : undefined,
      metadata: isPlainObject(response.metadata) ? response.metadata : {},
    });
  }

  /**
   * Report a runtime block / redaction back to watchdog.
   *
   * Used so the watchdog compliance dashboard reflects what actually happened
   * at runtime, not just what static crawls discovered. Failures are
   * logged-and-continued — the agent doesn't have a useful response to "I
   * couldn't report a violation".
   */
  async reportViolation(
    decision: WatchdogDecision,
    context: Record<string, unknown> = {},
  ): Promise<void> {
    const payload = {
      type: 'violation_report',
      decision: {
        action: decision.action,
        reason: decision.reason ?? null,
        policy_id: decision.policyId ?? null,
        domain: decision.domain ?? null,
        metadata: decision.metadata,
      },
      context,
    };
    try {
      await this.transport(payload);
    } catch (e) {
      console.warn(
        `[watchdog] violation report failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Guard adapters — plug into existing apx-agent hooks
// ---------------------------------------------------------------------------

export interface WatchdogGuardOptions {
  client: WatchdogClient;
  /** Agent name threaded into every context dict watchdog receives. */
  agentName?: string;
  /**
   * Returns the currently active tracing span, or `null` when no span is
   * active. Defaults to `() => null` (no MLflow JS bridge yet). When wired,
   * decisions other than `allow` are annotated with `apx.watchdog.*` audit
   * attributes via {@link setAuditAttrs}.
   */
  getActiveSpan?: () => SpanLike | null;
}

/** Hook callable shapes returned by {@link WatchdogGuard}. */
export type InputGuard = (messages: unknown) => Promise<string | null>;
export type OutputGuard = (text: string) => Promise<string | null>;
export type BeforeTool = (toolName: string, args: Record<string, unknown>) => Promise<void>;
export type BeforeModel = (prompts: unknown) => Promise<void>;

/**
 * Bridge {@link WatchdogClient} decisions into apx-agent's hook callables.
 *
 * Each `forX()` method returns a callable wired through the right hook
 * signature. Reject decisions raise / short-circuit per the hook's contract;
 * redact decisions modify content where the hook supports it; allow
 * decisions are pass-through.
 */
export class WatchdogGuard {
  readonly client: WatchdogClient;
  readonly agentName: string | undefined;
  private readonly getActiveSpan: () => SpanLike | null;

  constructor(options: WatchdogGuardOptions) {
    this.client = options.client;
    this.agentName = options.agentName;
    this.getActiveSpan = options.getActiveSpan ?? ((): SpanLike | null => null);
  }

  private buildContext(extra: Record<string, unknown> = {}): Record<string, unknown> {
    const base: Record<string, unknown> = {};
    if (this.agentName) base.agent_name = this.agentName;
    Object.assign(base, extra);
    return base;
  }

  /** input_guardrails-compatible callable. Returns `null` to allow, string to short-circuit. */
  forInput(): InputGuard {
    return async (messages: unknown): Promise<string | null> => {
      const ctx = this.buildContext({ messages: summariseMessages(messages) });
      const decision = await this.client.evaluate({ operation: 'input_message', context: ctx });
      recordDecisionOnSpan(decision, this.agentName, this.getActiveSpan);
      if (decision.action === 'reject') {
        await this.client.reportViolation(decision, ctx);
        return decision.reason ?? 'Request blocked by Watchdog policy.';
      }
      return null;
    };
  }

  /** output_guardrails-compatible callable. Returns `null` to allow, string to replace. */
  forOutput(): OutputGuard {
    return async (text: string): Promise<string | null> => {
      const ctx = this.buildContext({ text_length: text.length });
      const decision = await this.client.evaluate({ operation: 'output_message', context: ctx });
      recordDecisionOnSpan(decision, this.agentName, this.getActiveSpan);
      if (decision.action === 'reject') {
        await this.client.reportViolation(decision, this.buildContext());
        return decision.reason ?? 'Response blocked by Watchdog policy.';
      }
      if (decision.action === 'redact' && decision.redactedContent) {
        await this.client.reportViolation(decision, this.buildContext());
        return decision.redactedContent;
      }
      return null;
    };
  }

  /** before_tool-compatible callable. Throws to abort. */
  forTool(): BeforeTool {
    return async (toolName: string, args: Record<string, unknown>): Promise<void> => {
      const ctx = this.buildContext({ tool_name: toolName, arguments: args });
      const decision = await this.client.evaluate({ operation: 'tool_call', context: ctx });
      recordDecisionOnSpan(decision, this.agentName, this.getActiveSpan);
      if (decision.action === 'reject') {
        await this.client.reportViolation(decision, ctx);
        throw new Error(decision.reason ?? `Tool ${JSON.stringify(toolName)} blocked by Watchdog policy.`);
      }
    };
  }

  /** before_model-compatible callable. Throws to abort the LLM call. */
  forModel(): BeforeModel {
    return async (prompts: unknown): Promise<void> => {
      const ctx = this.buildContext({ prompt_count: countPrompts(prompts) });
      const decision = await this.client.evaluate({ operation: 'model_call', context: ctx });
      recordDecisionOnSpan(decision, this.agentName, this.getActiveSpan);
      if (decision.action === 'reject') {
        await this.client.reportViolation(decision, this.buildContext());
        throw new Error(decision.reason ?? 'Model call blocked by Watchdog policy.');
      }
    };
  }
}

function summariseMessages(messages: unknown): Record<string, unknown> {
  if (!Array.isArray(messages)) return { count: 0 };
  const last = messages.length > 0 ? messages[messages.length - 1] : undefined;
  let lastRole: unknown = null;
  if (last !== null && typeof last === 'object') {
    lastRole = (last as Record<string, unknown>).role ?? null;
  }
  return { count: messages.length, last_role: lastRole };
}

function countPrompts(prompts: unknown): number {
  if (Array.isArray(prompts)) return prompts.length;
  return 1;
}

// ---------------------------------------------------------------------------
// Metadata emission
// ---------------------------------------------------------------------------

/**
 * Minimum surface emitAgentMetadata reads off each tool callable yielded by
 * `toolIterator`. TS `AgentTool` doesn't carry UC binding metadata directly,
 * so callers can optionally tag each tool with `ucName` / `grants` for
 * watchdog. Attached resource specs (via `attachResources`) are read through
 * {@link getResources}.
 */
export interface ToolDescriptor {
  name: string;
  description?: string;
  ucName?: string;
  grants?: string[];
}

export interface EmitAgentMetadataOptions<Agent> {
  agent: Agent;
  model?: string;
  name?: string;
  /** Yields every tool callable/descriptor for the agent tree. */
  toolIterator: (agent: Agent) => Iterable<ToolDescriptor & object>;
  /** Yields every sub-agent URL/name registered on the agent tree. */
  subAgentIterator: (agent: Agent) => Iterable<string>;
  /** Optional agent instructions string. */
  instructions?: string;
}

/** Crawler-facing metadata blob. JSON-serializable. */
export interface AgentMetadata {
  name: string | null;
  model: string | null;
  instructions: string;
  tools: Array<{
    name: string;
    description: string;
    uc_name: string | null;
    grants: string[];
    resources: Array<{ kind: string; identifier: string }>;
  }>;
  sub_agents: string[];
  resources: Array<{ kind: string; identifier: string }>;
}

/**
 * Produce the agent's crawler-facing metadata for watchdog.
 *
 * Returns a JSON-serializable object suitable for posting to watchdog's
 * crawl endpoint, writing as an MLflow tag, or persisting to a UC manifest
 * table.
 */
export function emitAgentMetadata<Agent>(
  options: EmitAgentMetadataOptions<Agent>,
): AgentMetadata {
  const toolsMeta: AgentMetadata['tools'] = [];
  for (const fn of options.toolIterator(options.agent)) {
    toolsMeta.push({
      name: fn.name,
      description: (fn.description ?? '').trim(),
      uc_name: fn.ucName ?? null,
      grants: fn.grants ? [...fn.grants] : [],
      resources: getResources(fn).map((s: ResourceSpec) => ({
        kind: s.kind,
        identifier: s.identifier,
      })),
    });
  }

  const resources = collectResourceSpecs<Agent>({
    agent: options.agent,
    model: options.model,
    toolIterator: options.toolIterator as (a: Agent) => Iterable<unknown>,
    subAgentIterator: options.subAgentIterator,
  }).map((s) => ({ kind: s.kind, identifier: s.identifier }));

  return {
    name: options.name ?? null,
    model: options.model ?? null,
    instructions: options.instructions ?? '',
    tools: toolsMeta,
    sub_agents: [...options.subAgentIterator(options.agent)],
    resources,
  };
}

// ---------------------------------------------------------------------------
// UC tags writer
// ---------------------------------------------------------------------------

const TAG_VALUE_MAX = 4000;

function truncate(value: string, limit: number = TAG_VALUE_MAX): string {
  if (value.length <= limit) return value;
  return value.slice(0, limit - 14) + '...[truncated]';
}

function buildUcTagPayload(metadata: AgentMetadata): Record<string, string> {
  const toolsShort = metadata.tools.map((t) => t.name);
  const ucFunctions = metadata.tools
    .map((t) => t.uc_name)
    .filter((n): n is string => typeof n === 'string' && n.length > 0);
  const subAgents = metadata.sub_agents;
  const resources = metadata.resources;

  const tags: Record<string, string> = {
    'apx.agent.name': String(metadata.name ?? ''),
    'apx.agent.model': String(metadata.model ?? ''),
    'apx.agent.tool_count': String(toolsShort.length),
    'apx.agent.tools': truncate(JSON.stringify(toolsShort)),
    'apx.agent.uc_functions': truncate(ucFunctions.join(',')),
    'apx.agent.sub_agents': truncate(subAgents.join(',')),
    'apx.agent.resources': truncate(JSON.stringify(resources)),
    'apx.agent.metadata': truncate(JSON.stringify(metadata)),
  };

  // Drop empty values — UC rejects empty tag values, and watchdog gains
  // nothing from `apx.agent.sub_agents = ""`.
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(tags)) {
    if (v !== '') out[k] = v;
  }
  return out;
}

/** Minimum MLflow client surface needed to write registered-model tags. */
export interface MlflowTagClient {
  setRegisteredModelTag(name: string, key: string, value: string): Promise<void>;
}

export interface SetUcTagsOptions<Agent> {
  registeredModelName: string;
  agent: Agent;
  model?: string;
  name?: string;
  mlflowClient: MlflowTagClient;
  toolIterator: (agent: Agent) => Iterable<ToolDescriptor & object>;
  subAgentIterator: (agent: Agent) => Iterable<string>;
  instructions?: string;
}

/**
 * Write the agent's metadata as UC tags on its registered model.
 *
 * Per-tag write failures are logged via `console.warn` and the remaining tags
 * are still attempted. Returns the dict of keys-values that were *attempted*
 * (one entry per tag actually passed to `mlflowClient.setRegisteredModelTag`).
 */
export async function setUcTagsForAgent<Agent>(
  options: SetUcTagsOptions<Agent>,
): Promise<Record<string, string>> {
  const metadata = emitAgentMetadata<Agent>({
    agent: options.agent,
    model: options.model,
    name: options.name,
    toolIterator: options.toolIterator,
    subAgentIterator: options.subAgentIterator,
    instructions: options.instructions,
  });

  const tags = buildUcTagPayload(metadata);

  for (const [key, value] of Object.entries(tags)) {
    try {
      await options.mlflowClient.setRegisteredModelTag(options.registeredModelName, key, value);
    } catch (e) {
      console.warn(
        `[watchdog] failed to write UC tag ${key} on ${options.registeredModelName}: ${e instanceof Error ? e.message : String(e)} — continuing with remaining tags.`,
      );
    }
  }

  return tags;
}

// ---------------------------------------------------------------------------
// UC violations writer — transport that handles violation_report only
// ---------------------------------------------------------------------------

function sqlStrLiteral(value: string): string {
  // Escape backslashes before single quotes (Spark SQL treats backslash as an
  // escape character by default). Mirrors engine-delta.ts / memory-lakebase.ts;
  // a trailing backslash would otherwise escape the closing quote and break out
  // of the literal. Injected fields originate from the MCP transport response.
  return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "''")}'`;
}

/** SQL executor callable — wraps the Statements API or any equivalent. */
export type SqlExecutor = (sql: string) => Promise<unknown>;

export interface MakeUcViolationWriterOptions {
  violationsTable: string;
  sqlExecutor: SqlExecutor;
  /** Create the violations table on first use. Default: true. */
  autoCreate?: boolean;
}

const UcViolationWriterOptionsSchema = z.object({
  violationsTable: z.string(),
  autoCreate: z.boolean().optional(),
});

/**
 * Return a transport that handles only `violation_report` requests.
 *
 * Inserts a row into the watchdog runtime-violations Delta table per
 * report. Non-violation-report requests pass through as `{action: "allow"}`
 * so this transport can be composed with an evaluate transport via
 * {@link makeWatchdogTransport}.
 */
export function makeUcViolationWriter(options: MakeUcViolationWriterOptions): TransportFn {
  UcViolationWriterOptionsSchema.parse({
    violationsTable: options.violationsTable,
    autoCreate: options.autoCreate,
  });

  if (options.violationsTable.split('.').length !== 3) {
    throw new Error(
      `violationsTable must be a three-part UC name (catalog.schema.table); got ${JSON.stringify(options.violationsTable)}`,
    );
  }

  const autoCreate = options.autoCreate ?? true;
  let created = false;

  const ensureTable = async (): Promise<void> => {
    if (created || !autoCreate) return;
    const ddl =
      `CREATE TABLE IF NOT EXISTS ${options.violationsTable} (` +
      `  ts TIMESTAMP NOT NULL,` +
      `  agent_name STRING,` +
      `  operation STRING,` +
      `  action STRING,` +
      `  reason STRING,` +
      `  policy_id STRING,` +
      `  domain STRING,` +
      `  context STRING,` +
      `  metadata STRING` +
      `) USING DELTA`;
    try {
      await options.sqlExecutor(ddl);
    } catch (e) {
      console.warn(
        `[watchdog] CREATE TABLE IF NOT EXISTS ${options.violationsTable} failed: ${e instanceof Error ? e.message : String(e)} — subsequent inserts may fail.`,
      );
    }
    created = true;
  };

  return async (req: object): Promise<object> => {
    const r = req as Record<string, unknown>;
    if (r.type !== 'violation_report') {
      return { action: 'allow' };
    }
    await ensureTable();
    const decision = isPlainObject(r.decision) ? r.decision : {};
    const context = isPlainObject(r.context) ? r.context : {};
    const ts = Date.now() / 1000;

    const sql =
      `INSERT INTO ${options.violationsTable} ` +
      `(ts, agent_name, operation, action, reason, policy_id, domain, context, metadata) ` +
      `VALUES (` +
      `  CAST(${ts} AS TIMESTAMP), ` +
      `  ${sqlStrLiteral(String(context.agent_name ?? ''))}, ` +
      `  ${sqlStrLiteral(String(context.operation ?? r.operation ?? ''))}, ` +
      `  ${sqlStrLiteral(String(decision.action ?? ''))}, ` +
      `  ${sqlStrLiteral(String(decision.reason ?? ''))}, ` +
      `  ${sqlStrLiteral(String(decision.policy_id ?? ''))}, ` +
      `  ${sqlStrLiteral(String(decision.domain ?? ''))}, ` +
      `  ${sqlStrLiteral(JSON.stringify(context))}, ` +
      `  ${sqlStrLiteral(JSON.stringify(decision.metadata ?? {}))}` +
      `)`;

    try {
      await options.sqlExecutor(sql);
    } catch (e) {
      console.warn(
        `[watchdog] failed to insert violation row into ${options.violationsTable}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
    return {};
  };
}

// ---------------------------------------------------------------------------
// MCP evaluate transport
// ---------------------------------------------------------------------------

/**
 * Callable that invokes a remote MCP tool and returns its parsed JSON
 * content block. The TS SDK already has a low-level MCP client
 * ({@link src/agent/mcp-client.ts}); rather than depend on it directly here,
 * we accept a minimal callable so callers can plug their own MCP transport
 * (or a test fake) in.
 *
 * Contract: given `(url, toolName, args)`, return the first parseable JSON
 * payload from the tool's content blocks. Implementations should resolve
 * `{action: "allow"}` (or throw) when the tool returns nothing useful.
 */
export type McpToolCallFn = (
  url: string,
  toolName: string,
  args: Record<string, unknown>,
) => Promise<Record<string, unknown>>;

export interface MakeMcpTransportOptions {
  mcpUrl: string;
  toolName: string;
  timeoutMs?: number;
  authHeaders?: Record<string, string>;
  /**
   * MCP tool call implementation. Required for the transport to actually call
   * an MCP endpoint — when omitted, evaluate requests fall back to allow with
   * a warning so the module is safe to construct without a wired client.
   */
  mcpClient?: McpToolCallFn;
}

/**
 * Return a transport that calls a watchdog MCP tool for evaluate requests.
 *
 * Non-evaluate requests (`type === 'violation_report'`) pass through as
 * `{}` so this transport can be composed with a violation writer via
 * {@link makeWatchdogTransport}.
 *
 * Falls back to `{action: "allow"}` on any error from the MCP client so
 * watchdog outages don't black-hole production traffic.
 */
export function makeMcpTransport(options: MakeMcpTransportOptions): TransportFn {
  const timeoutMs = options.timeoutMs ?? 5_000;

  return async (req: object): Promise<object> => {
    const r = req as Record<string, unknown>;
    if (r.type === 'violation_report') {
      // Violation reports go to the UC table writer, not MCP.
      return {};
    }

    if (!options.mcpClient) {
      console.warn(
        `[watchdog] makeMcpTransport invoked without an mcpClient — allowing.`,
      );
      return { action: 'allow', reason: 'watchdog mcp transport not configured' };
    }

    const args: Record<string, unknown> = {
      operation: r.operation,
      context: isPlainObject(r.context) ? r.context : {},
    };

    try {
      const callPromise = options.mcpClient(options.mcpUrl, options.toolName, args);
      const timeoutPromise = new Promise<never>((_, reject) => {
        setTimeout(() => reject(new Error(`watchdog mcp call timed out after ${timeoutMs}ms`)), timeoutMs).unref?.();
      });
      const result = await Promise.race([callPromise, timeoutPromise]);
      if (!isPlainObject(result)) {
        console.warn(`[watchdog] MCP tool ${options.toolName} returned non-object — allowing.`);
        return { action: 'allow' };
      }
      return result;
    } catch (e) {
      console.warn(
        `[watchdog] MCP call to ${options.toolName} failed: ${e instanceof Error ? e.message : String(e)} — falling back to allow.`,
      );
      return {
        action: 'allow',
        reason: `watchdog mcp transport error: ${e instanceof Error ? e.message : String(e)}`,
      };
    }
  };
}

// ---------------------------------------------------------------------------
// Combined transport
// ---------------------------------------------------------------------------

export interface MakeWatchdogTransportOptions {
  mcpUrl: string;
  mcpToolName: string;
  violationsTable: string;
  sqlExecutor: SqlExecutor;
  mcpClient?: McpToolCallFn;
  mcpTimeoutMs?: number;
  mcpAuthHeaders?: Record<string, string>;
  autoCreateTable?: boolean;
}

/**
 * Combined transport: evaluate via MCP, violation reports into UC Delta.
 *
 * This is the canonical watchdog transport — both wire-protocol contracts
 * answered, composed into one callable that
 * `new WatchdogClient({transport})` consumes.
 */
export function makeWatchdogTransport(options: MakeWatchdogTransportOptions): TransportFn {
  const evalTransport = makeMcpTransport({
    mcpUrl: options.mcpUrl,
    toolName: options.mcpToolName,
    timeoutMs: options.mcpTimeoutMs,
    authHeaders: options.mcpAuthHeaders,
    mcpClient: options.mcpClient,
  });
  const violationTransport = makeUcViolationWriter({
    violationsTable: options.violationsTable,
    sqlExecutor: options.sqlExecutor,
    autoCreate: options.autoCreateTable,
  });

  return async (req: object): Promise<object> => {
    const r = req as Record<string, unknown>;
    if (r.type === 'violation_report') {
      return violationTransport(req);
    }
    return evalTransport(req);
  };
}

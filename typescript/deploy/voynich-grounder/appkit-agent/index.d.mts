import { AsyncLocalStorage } from "node:async_hooks";
import { z } from "zod";
import { Express } from "express";

//#region src/agent/tools.d.ts
/** A tool function with metadata derived from its Zod schema. */
interface AgentTool {
  name: string;
  description: string;
  parameters: z.ZodType;
  handler: (args: unknown) => Promise<unknown>;
}
/** OpenAI function calling format. */
interface FunctionSchema {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
  };
}
/**
 * Define a typed agent tool.
 *
 * @example
 * const getLineage = defineTool({
 *   name: 'get_table_lineage',
 *   description: 'Get upstream sources for a table',
 *   parameters: z.object({ tableName: z.string() }),
 *   handler: async ({ tableName }) => {
 *     // query Unity Catalog lineage
 *   },
 * });
 */
declare function defineTool<T extends z.ZodType>(opts: {
  name: string;
  description: string;
  parameters: T;
  handler: (args: z.infer<T>) => Promise<unknown>;
}): AgentTool;
/** Convert a Zod schema to JSON Schema, suitable for OpenAI function calling. */
declare function zodToJsonSchema(schema: z.ZodType): Record<string, unknown>;
/**
 * Ensure a JSON schema is "strict" for OpenAI — adds `additionalProperties: false`
 * on all object types recursively.
 */
declare function toStrictSchema(schema: Record<string, unknown> | null | undefined): Record<string, unknown>;
/** Convert AgentTools to OpenAI function calling format. */
declare function toolsToFunctionSchemas(tools: AgentTool[]): FunctionSchema[];
//#endregion
//#region src/workflows/state.d.ts
/**
 * AgentState — shared key-value store for workflow agents.
 *
 * Follows the Google ADK pattern:
 * - `output_key`: when an agent has an outputKey, its result is stored under that key
 * - Template interpolation: `{variable_name}` in instruction strings resolves from state
 * - Scoped state: keys prefixed with `temp:` are turn-specific and cleared between steps
 *
 * @example
 * const state = new AgentState({ topic: 'billing' });
 * state.set('analysis', 'The billing data shows...');
 *
 * // Template interpolation
 * const instructions = state.interpolate('Summarize the {topic} analysis: {analysis}');
 * // => 'Summarize the billing analysis: The billing data shows...'
 *
 * // Scoped temp values
 * state.set('temp:scratchpad', 'intermediate work');
 * state.clearTemp();
 * state.has('temp:scratchpad'); // false
 */
declare class AgentState {
  private store;
  constructor(initial?: Record<string, unknown>);
  /** Get a value by key. Returns undefined if not present. */
  get<T = unknown>(key: string): T | undefined;
  /** Set a value. Use `temp:` prefix for turn-scoped data. */
  set(key: string, value: unknown): void;
  /** Check if a key exists. */
  has(key: string): boolean;
  /** Delete a key. */
  delete(key: string): boolean;
  /** Return all keys. */
  keys(): string[];
  /** Return all entries as a plain object. */
  toObject(): Record<string, unknown>;
  /**
   * Clear all keys with the `temp:` prefix.
   * Called between agent steps in a sequential pipeline so
   * temporary scratchpad data doesn't leak across turns.
   */
  clearTemp(): void;
  /**
   * Replace `{variable_name}` placeholders in a string with state values.
   *
   * Only replaces variables that exist in state. Unknown placeholders are
   * left as-is so downstream agents can still reference them (or the caller
   * gets a clear signal that the variable wasn't set).
   *
   * Values are coerced to strings via `String()`.
   */
  interpolate(template: string): string;
  /** Create a shallow copy of this state. */
  clone(): AgentState;
}
//#endregion
//#region src/workflows/types.d.ts
/** A message in the conversation. */
interface Message {
  role: string;
  content: string;
}
/**
 * Base interface for any agent that can be composed in a workflow.
 *
 * This is intentionally minimal — a plain function that takes messages
 * and returns text. Workflow agents (Sequential, Parallel, Loop, Router,
 * Handoff) all implement this interface while adding composition logic.
 */
interface Runnable {
  /**
   * When set, the agent's output is stored in AgentState under this key
   * after execution. Follows the Google ADK output_key pattern.
   */
  outputKey?: string;
  /** Run the agent and return the final text. */
  run(messages: Message[], state?: AgentState): Promise<string>;
  /** Stream text chunks. Default: run to completion and yield once. */
  stream?(messages: Message[], state?: AgentState): AsyncGenerator<string>;
  /** Collect tool descriptors for all agents in the tree. */
  collectTools?(): AgentTool[];
}
//#endregion
//#region src/agent/plugin.d.ts
interface AgentConfig {
  /** Human-readable agent name (used in traces). */
  name?: string;
  /** Model serving endpoint name (e.g. 'databricks-claude-sonnet-4-6'). */
  model: string;
  /** System prompt for the agent. */
  instructions?: string;
  /** Registered tool functions. */
  tools?: AgentTool[];
  /** Max tool-calling loop iterations. */
  maxIterations?: number;
  /** URLs of remote sub-agents (Databricks Apps). */
  subAgents?: string[];
  /**
   * A workflow agent (SequentialAgent, ParallelAgent, etc.) to use as the
   * execution engine for /responses. When set, the plugin delegates to
   * `workflow.run()` instead of the default LLM tool-calling loop.
   */
  workflow?: Runnable;
  /**
   * Remote MCP server URLs to consume as tools.
   *
   * Tools are discovered at startup via tools/list and merged into the
   * agent's tool list alongside any statically registered tools.
   *
   * Supports Databricks managed MCP URLs:
   *   /api/2.0/mcp/genie/{space_id}              — Genie Space
   *   /api/2.0/mcp/functions/{catalog}/{schema}   — UC Functions
   *
   * @example
   * mcpServers: [
   *   'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
   *   'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
   * ]
   */
  mcpServers?: string[];
  /** API route prefix. Defaults to '/api/agent'. */
  apiPrefix?: string;
}
interface AgentExports {
  getTools: () => AgentTool[];
  getConfig: () => AgentConfig;
  getToolSchemas: () => FunctionSchema[];
}
/**
 * Create the agent plugin.
 *
 * This returns a plain plugin object compatible with AppKit's createApp().
 * When AppKit's class-based Plugin API is confirmed and stable, this can
 * be converted to extend Plugin<AgentConfig>.
 */
declare function createAgentPlugin(config: AgentConfig): {
  name: "agent";
  displayName: string;
  description: string;
  setup(expressApp: Express): Promise<void>;
  injectRoutes(router: {
    get: Function;
    post: Function;
    all: Function;
  }): void;
  exports(): AgentExports;
};
//#endregion
//#region src/agent/runner.d.ts
interface RunParams {
  model: string;
  instructions: string;
  messages: Array<{
    role: string;
    content: string;
  }>;
  tools: AgentTool[];
  subAgents?: string[];
  maxTurns?: number;
  /** @deprecated No longer used — tools are called directly. */
  app?: Express;
  oboHeaders: Record<string, string>;
  /** @deprecated No longer used. */
  apiPrefix?: string;
}
/**
 * @deprecated FMAPI runner handles auth internally. This is a no-op kept
 * for backward compatibility with plugin.ts setup().
 */
declare function initDatabricksClient(): void;
/**
 * @deprecated Kept for backward compatibility. Tools are called directly now.
 */
declare function toFunctionTool(agentTool: AgentTool, ..._rest: any[]): any;
/**
 * @deprecated Kept for backward compatibility.
 */
declare function toSubAgentTool(name: string, description: string, url: string, oboHeaders: Record<string, string>): any;
/** Run the agent loop and return the final text. */
declare function runViaSDK(params: RunParams): Promise<string>;
/** Stream the agent loop, yielding text chunks. */
declare function streamViaSDK(params: RunParams): AsyncGenerator<string>;
//#endregion
//#region src/trace.d.ts
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
interface TraceSpan {
  type: 'request' | 'llm' | 'tool' | 'agent_call' | 'response' | 'error';
  name: string;
  startTime: number;
  duration_ms?: number;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
}
interface Trace {
  id: string;
  agentName: string;
  startTime: number;
  endTime?: number;
  duration_ms?: number;
  spans: TraceSpan[];
  status?: 'in_progress' | 'completed' | 'error';
}
interface TraceContext {
  trace: Trace;
}
declare function storeTrace(trace: Trace): void;
declare function getTraces(): Trace[];
declare function getTrace(id: string): Trace | undefined;
declare function createTrace(agentName: string): Trace;
declare function addSpan(trace: Trace, span: Omit<TraceSpan, 'startTime'>): TraceSpan;
declare function endSpan(span: TraceSpan): void;
declare function endTrace(trace: Trace, status?: 'completed' | 'error'): void;
declare function truncate(value: unknown, maxLen?: number): string;
//#endregion
//#region src/agent/request-context.d.ts
interface RequestContext {
  /** OBO and auth headers forwarded from the incoming HTTP request. */
  oboHeaders: Record<string, string>;
  /** Optional distributed-tracing handle for the current request. */
  trace?: Trace;
}
/** Run `fn` with the given context available to all async descendants. */
declare function runWithContext<T>(ctx: RequestContext, fn: () => T): T;
/** Return the current request context, or undefined outside a request. */
declare function getRequestContext(): RequestContext | undefined;
//#endregion
//#region src/discovery/index.d.ts
interface AgentCard {
  schemaVersion: string;
  name: string;
  description: string;
  url: string;
  protocolVersion: string;
  capabilities: {
    streaming: boolean;
    multiTurn: boolean;
  };
  authentication: {
    schemes: string[];
    credentials: string;
  };
  skills: Array<{
    id: string;
    name: string;
    description: string;
  }>;
  mcpEndpoint?: string;
}
interface DiscoveryConfig {
  name?: string;
  description?: string;
  /** Public URL of this agent (supports $ENV_VAR). */
  url?: string;
  /** URL of an agent registry to auto-register with on startup. */
  registry?: string;
}
declare function createDiscoveryPlugin(config: DiscoveryConfig, agentExports: () => AgentExports | null): {
  name: "discovery";
  displayName: string;
  description: string;
  setup(): void;
  injectRoutes(router: {
    get: Function;
  }): void;
};
//#endregion
//#region src/mcp/index.d.ts
interface McpConfig {
  /** Path to mount the MCP endpoint. Defaults to '/mcp'. */
  path?: string;
}
interface McpAuthContext {
  authorization: string;
  oboToken: string;
}
declare const mcpAuthStore: AsyncLocalStorage<McpAuthContext>;
declare function getMcpAuth(): McpAuthContext | undefined;
declare function createMcpPlugin(config: McpConfig, agentExports: () => AgentExports | null): {
  name: "mcp";
  displayName: string;
  description: string;
  setup(): Promise<void>;
  injectRoutes(router: {
    all: Function;
    get: Function;
    post: Function;
  }): void;
};
//#endregion
//#region src/dev/index.d.ts
interface DevUIConfig {
  /** Base path for dev UI routes. Defaults to '/_apx'. */
  basePath?: string;
  /** Disable in production. Defaults to true. */
  productionGuard?: boolean;
}
declare function createDevPlugin(config: DevUIConfig, agentExports: () => AgentExports | null): {
  name: "devUI";
  displayName: string;
  description: string;
  injectRoutes(router: {
    get: Function;
  }): void;
};
//#endregion
//#region src/workflows/engine.d.ts
/**
 * WorkflowEngine — durable execution primitive for workflow agents.
 *
 * Each step of a workflow is wrapped in `engine.step(runId, stepKey, handler)`.
 * The engine persists the step's output (or failure) keyed by `(runId, stepKey)`,
 * so a subsequent call with the same key returns the cached result instead of
 * re-invoking the handler. This is what lets a workflow resume after a crash,
 * redeploy, or pause — the completed steps replay from persistence, and the
 * first uncompleted step runs fresh.
 *
 * The interface is intentionally small. Callers invoke `step()` inline around
 * any expensive or non-deterministic operation; there is no decorator, DSL, or
 * build step. This matches the shape of `step.run()` in Inngest and `@DBOS.step`
 * in DBOS.
 *
 * See `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`.
 */
/** Lifecycle status of a workflow run. */
type RunStatus = 'running' | 'paused' | 'completed' | 'converged' | 'failed' | 'cancelled';
/** Persisted record of a single step invocation. */
interface StepRecord {
  stepKey: string;
  status: 'completed' | 'failed';
  output?: unknown;
  error?: string;
  durationMs: number;
  recordedAt: string;
}
/** Full snapshot of a run, including its step log. */
interface RunSnapshot {
  runId: string;
  workflowName: string;
  status: RunStatus;
  input: unknown;
  output?: unknown;
  startedAt: string;
  updatedAt: string;
  steps: StepRecord[];
}
/** Compact summary returned by listRuns(). */
interface RunSummary {
  runId: string;
  workflowName: string;
  status: RunStatus;
  startedAt: string;
  updatedAt: string;
}
/** Filter options for listRuns(). */
interface RunFilter {
  workflowName?: string;
  status?: RunStatus;
  limit?: number;
}
/**
 * Thrown when a handler raised an error that the engine persisted. Replay of
 * a previously failed step re-throws this so callers see the same failure
 * they would have seen originally.
 */
declare class StepFailedError extends Error {
  readonly stepKey: string;
  constructor(stepKey: string, message: string);
}
/**
 * Pluggable backend for durable workflow execution.
 *
 * Implementations:
 * - `InMemoryEngine` — per-process Map, default, used in tests and dev.
 * - `DeltaEngine` (Phase 4) — SQL Statements API against a Delta table.
 * - `InngestEngine` (Phase 5) — adapter onto Inngest's step runner.
 */
interface WorkflowEngine {
  /**
   * Start a new run, or re-open an existing one.
   *
   * If `opts.runId` is provided and an existing run is found, the run is
   * re-opened: status is set back to `running` and subsequent `step()` calls
   * replay from the persisted log. Otherwise, a new run is created.
   *
   * Returns the run's ID.
   */
  startRun(workflowName: string, input: unknown, opts?: {
    runId?: string;
  }): Promise<string>;
  /**
   * Execute a checkpointed step.
   *
   * - On cache hit with `status = 'completed'`: returns the persisted output
   *   without invoking `handler`.
   * - On cache hit with `status = 'failed'`: re-throws a `StepFailedError`
   *   without invoking `handler`.
   * - On cache miss: invokes `handler`, persists the result (or failure),
   *   then returns or throws.
   *
   * `stepKey` must be stable across replays — e.g. `mutate-${generation}`.
   */
  step<T>(runId: string, stepKey: string, handler: () => Promise<T>): Promise<T>;
  /** Mark a run finished with a terminal or paused status. */
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;
  /** Read the full snapshot of a run. Returns null if not found. */
  getRun(runId: string): Promise<RunSnapshot | null>;
  /** List runs matching the given filter. */
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}
//#endregion
//#region src/workflows/sequential.d.ts
interface SequentialAgentOptions {
  /** Durable execution engine. Default: fresh in-process `InMemoryEngine`. */
  engine?: WorkflowEngine;
  /** If set, resume an existing run with this ID. */
  runId?: string;
  /** Workflow name for engine run records. Default: `sequential`. */
  workflowName?: string;
}
declare class SequentialAgent implements Runnable {
  private agents;
  private instructions;
  private engine;
  private providedRunId;
  private workflowName;
  constructor(agents: Runnable[], instructions?: string, options?: SequentialAgentOptions);
  run(messages: Message[], state?: AgentState): Promise<string>;
  stream(messages: Message[], state?: AgentState): AsyncGenerator<string>;
  collectTools(): AgentTool[];
  /**
   * Prepend system instructions to messages.
   * If state is provided, interpolate {variables} in the instructions.
   */
  private prependInstructions;
}
//#endregion
//#region src/workflows/parallel.d.ts
declare class ParallelAgent implements Runnable {
  private agents;
  private instructions;
  private separator;
  constructor(agents: Runnable[], options?: {
    instructions?: string;
    separator?: string;
  });
  run(messages: Message[]): Promise<string>;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
  private prependInstructions;
}
//#endregion
//#region src/workflows/loop.d.ts
type StopPredicate = (result: string, iteration: number) => boolean;
interface LoopAgentOptions {
  maxIterations?: number;
  stopWhen?: StopPredicate;
  /**
   * Durable execution engine. If omitted, an in-process `InMemoryEngine` is
   * used — preserves the pre-durable behavior (state lost on restart).
   */
  engine?: WorkflowEngine;
  /** If set, resume an existing run with this ID. */
  runId?: string;
  /** Workflow name for engine run records. Default: `loop`. */
  workflowName?: string;
}
declare class LoopAgent implements Runnable {
  private agent;
  private maxIterations;
  private stopWhen;
  private engine;
  private providedRunId;
  private workflowName;
  constructor(agent: Runnable, options?: LoopAgentOptions);
  run(messages: Message[]): Promise<string>;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
}
//#endregion
//#region src/workflows/router.d.ts
interface Route {
  name: string;
  description: string;
  agent: Runnable;
  /** Deterministic condition. If provided and returns true, this route is selected. */
  condition?: (messages: Message[]) => boolean;
}
interface RouterConfig {
  routes: Route[];
  /** Instructions for LLM-based routing (used when no condition matches). */
  instructions?: string;
  /** Fallback agent when no route matches and LLM routing is disabled. */
  fallback?: Runnable;
}
declare class RouterAgent implements Runnable {
  private routes;
  private instructions;
  private fallback;
  constructor(config: RouterConfig);
  run(messages: Message[]): Promise<string>;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
  private selectRoute;
}
//#endregion
//#region src/workflows/handoff.d.ts
interface HandoffConfig {
  /** Named agents that can hand off to each other. */
  agents: Record<string, Runnable>;
  /** Which agent starts the conversation. */
  start: string;
  /** Maximum number of handoffs before forcing a response. */
  maxHandoffs?: number;
  /**
   * Handoff callback — called when a handoff occurs. Use for logging,
   * metrics, or injecting context into the conversation.
   */
  onHandoff?: (from: string, to: string, context: string) => void;
}
/**
 * Wraps a Runnable to detect handoff requests in its output.
 *
 * The wrapped agent's response is checked for `transfer_to_<name>` patterns.
 * This is a simple text-matching approach — for real production use, the
 * underlying agent should use function calling with transfer tools.
 */
declare class HandoffAgent implements Runnable {
  private agents;
  private start;
  private maxHandoffs;
  private onHandoff;
  constructor(config: HandoffConfig);
  run(messages: Message[]): Promise<string>;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
}
//#endregion
//#region src/workflows/remote.d.ts
interface RemoteAgentConfig {
  /** Full URL to the agent card (/.well-known/agent.json). */
  cardUrl: string;
  /** Optional headers to forward on every request (e.g. OBO auth). */
  headers?: Record<string, string>;
  /** Request timeout in ms. Default: 120_000 (2 min). */
  timeoutMs?: number;
}
declare class RemoteAgent implements Runnable {
  /** Agent card metadata — populated after `init()`. */
  card: AgentCard | null;
  private cardUrl;
  private baseUrl;
  private headers;
  private timeoutMs;
  private initPromise;
  constructor(config: RemoteAgentConfig);
  /**
   * Create a RemoteAgent from a full agent card URL.
   * The card is fetched eagerly so metadata is available immediately.
   */
  static fromCardUrl(cardUrl: string, headers?: Record<string, string>): Promise<RemoteAgent>;
  /**
   * Create a RemoteAgent from a Databricks App name.
   *
   * Constructs the agent card URL from `DATABRICKS_HOST`:
   *   `https://<host>/apps/<appName>/.well-known/agent.json`
   *
   * Falls back to the apps subdomain pattern if DATABRICKS_HOST is not set
   * but DATABRICKS_WORKSPACE_ID is available.
   */
  static fromAppName(appName: string, headers?: Record<string, string>): Promise<RemoteAgent>;
  /** Fetch the agent card. Safe to call multiple times (idempotent). */
  init(): Promise<void>;
  private fetchCard;
  run(messages: Message[]): Promise<string>;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
  get name(): string;
  get description(): string;
  get skills(): Array<{
    id: string;
    name: string;
    description: string;
  }>;
  private extractText;
  private parseSSE;
}
//#endregion
//#region src/workflows/session.d.ts
/**
 * Interface for session persistence backends.
 *
 * The default InMemorySessionStore is suitable for development and testing.
 * Implement this interface for durable storage (e.g., Lakebase, Redis, DynamoDB).
 */
interface SessionStore {
  /** Persist a session snapshot. */
  save(id: string, data: SessionSnapshot): Promise<void>;
  /** Load a session snapshot by ID. Returns null if not found. */
  load(id: string): Promise<SessionSnapshot | null>;
  /** Delete a session by ID. */
  delete(id: string): Promise<void>;
  /** List all session IDs. */
  list(): Promise<string[]>;
}
/** Serializable snapshot of a session. */
interface SessionSnapshot {
  id: string;
  messages: Message[];
  state: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}
/** Simple in-memory store for development. Data is lost on process restart. */
declare class InMemorySessionStore implements SessionStore {
  private sessions;
  save(id: string, data: SessionSnapshot): Promise<void>;
  load(id: string): Promise<SessionSnapshot | null>;
  delete(id: string): Promise<void>;
  list(): Promise<string[]>;
}
/** Replace the default session store (e.g., with a Lakebase adapter). */
declare function setDefaultSessionStore(store: SessionStore): void;
/** Get the current default session store. */
declare function getDefaultSessionStore(): SessionStore;
declare class Session {
  readonly id: string;
  readonly state: AgentState;
  private messages;
  private store;
  private createdAt;
  private updatedAt;
  constructor(options?: {
    id?: string;
    state?: AgentState;
    store?: SessionStore;
  });
  /** Append a message to the conversation history. */
  addMessage(role: string, content: string): void;
  /** Return a copy of the conversation history. */
  getHistory(): Message[];
  /** Persist this session to the store. */
  save(): Promise<void>;
  /**
   * Load a session from the store.
   *
   * Returns a fully hydrated Session instance, or null if not found.
   */
  static load(id: string, store?: SessionStore): Promise<Session | null>;
  /** Delete this session from the store. */
  delete(): Promise<void>;
}
//#endregion
//#region src/workflows/hypothesis.d.ts
interface Hypothesis {
  id: string;
  generation: number;
  parent_id: string | null;
  fitness: Record<string, number>;
  metadata: Record<string, unknown>;
  flagged_for_review: boolean;
  created_at: string;
}
declare function createHypothesis(opts: {
  generation: number;
  parent_id?: string;
  fitness?: Record<string, number>;
  metadata?: Record<string, unknown>;
}): Hypothesis;
declare function compositeFitness(h: Hypothesis, weights: Record<string, number>): number;
//#endregion
//#region src/workflows/population.d.ts
interface PopulationStoreConfig {
  host?: string;
  populationTable: string;
  warehouseId?: string;
  chunkSize?: number;
  cacheEnabled?: boolean;
}
declare class PopulationStore {
  private host;
  private populationTable;
  private warehouseId;
  private chunkSize;
  private cacheEnabled;
  private cache;
  constructor(config: PopulationStoreConfig);
  writeHypotheses(hypotheses: Hypothesis[]): Promise<void>;
  updateFitnessScores(updates: Array<{
    id: string;
    fitness: Record<string, number>;
  }>): Promise<void>;
  flagForReview(ids: string[]): Promise<void>;
  loadGeneration(generation: number): Promise<Hypothesis[]>;
  loadTopSurvivors(generation: number, topN: number, weights: Record<string, number>): Promise<Hypothesis[]>;
  getFitnessHistory(nGenerations: number, weights: Record<string, number>): Promise<Array<{
    generation: number;
    best: number;
    avg: number;
  }>>;
  getActiveConstraints(): Promise<Array<Record<string, unknown>>>;
  clearCache(): void;
  private executeSql;
  private parseRows;
}
//#endregion
//#region src/workflows/evolutionary.d.ts
interface EvolutionaryConfig {
  store: PopulationStore;
  populationSize: number;
  mutationBatch: number;
  mutationAgent: string;
  fitnessAgents: string[];
  judgeAgent?: string;
  paretoObjectives: string[];
  fitnessWeights: Record<string, number>;
  maxGenerations: number;
  convergencePatience?: number;
  convergenceThreshold?: number;
  escalationThreshold?: number;
  topKAdversarial?: number;
  model?: string;
  instructions?: string;
  /** Per-generation timeout in milliseconds. Default: 600_000 (10 minutes). */
  generationTimeoutMs?: number;
  /**
   * Durable execution engine. If omitted, an in-process `InMemoryEngine` is
   * used — preserves the pre-durable behavior (state lost on restart). Pass a
   * `DeltaEngine` (or other backend) to survive restarts and redeploys.
   */
  engine?: WorkflowEngine;
  /**
   * If set, resume an existing run with this ID. On resume, the agent rebuilds
   * `history` and `currentGeneration` from the persisted step log and picks up
   * on the first uncompleted generation.
   */
  runId?: string;
  /** Workflow name used when creating engine run records. Default: `evolutionary`. */
  workflowName?: string;
}
type EvolutionState = 'idle' | 'running' | 'paused' | 'converged' | 'completed' | 'failed';
interface GenerationResult {
  generation: number;
  populationSize: number;
  bestFitness: number;
  avgFitness: number;
  paretoFrontierSize: number;
  escalated: Hypothesis[];
  wallTimeMs: number;
  converged: boolean;
}
declare class EvolutionaryAgent implements Runnable {
  private _state;
  private _currentGeneration;
  private _history;
  private _loopPromise;
  private _tools;
  /** Current evolution state. */
  get state(): EvolutionState;
  /** Current generation number. */
  get currentGeneration(): number;
  /** Completed generation results (read-only copy). */
  get history(): readonly GenerationResult[];
  private config;
  private patience;
  private threshold;
  private escalationThreshold;
  private topKAdversarial;
  private engine;
  private workflowName;
  private providedRunId;
  private runId;
  private initPromise;
  constructor(config: EvolutionaryConfig);
  run(_messages: Message[]): Promise<string>;
  /**
   * Open (or re-open) the run with the engine and, on resume, rebuild
   * `history` and `currentGeneration` from the persisted `finalize-*` steps.
   * Idempotent — safe to call multiple times; the work happens once.
   */
  private ensureInitialized;
  stream(messages: Message[]): AsyncGenerator<string>;
  collectTools(): AgentTool[];
  getState(): EvolutionState;
  startLoop(): void;
  pauseLoop(): void;
  resumeLoop(): void;
  /**
   * Check convergence: returns true when the last `patience` entries in
   * fitnessHistory have a range (max - min) smaller than threshold.
   */
  checkConvergence(fitnessHistory: Array<{
    generation: number;
    best: number;
    avg: number;
  }>): boolean;
  private runLoop;
  private runGeneration;
  private mutate;
  private evaluate;
  private judge;
  private callAgent;
  private buildTools;
  private stateSummary;
}
//#endregion
//#region src/workflows/pareto.d.ts
/**
 * Returns true when `a` Pareto-dominates `b` with respect to `objectives`.
 *
 * Dominance requires:
 *   1. a.fitness[obj] >= b.fitness[obj]  for ALL objectives
 *   2. a.fitness[obj] >  b.fitness[obj]  for AT LEAST ONE objective
 *
 * Missing fitness values are treated as 0.
 */
declare function paretoDominates(a: Hypothesis, b: Hypothesis, objectives: string[]): boolean;
/**
 * Returns the Pareto-optimal (non-dominated) subset of `population`.
 *
 * A hypothesis is non-dominated when no other hypothesis in the population
 * dominates it.  The algorithm is O(n²) — suitable for small populations.
 *
 * Returns an empty array for an empty population.
 */
declare function paretoFrontier(population: Hypothesis[], objectives: string[]): Hypothesis[];
/**
 * Selects at most `maxSize` survivors from `population`.
 *
 * Strategy:
 *  1. Compute the Pareto frontier.
 *  2. If the frontier already has >= maxSize members, return the top maxSize
 *     ranked by composite fitness (descending).
 *  3. Otherwise start with all frontier members, then fill remaining slots
 *     from the non-frontier population ordered by composite fitness (descending).
 *  4. If the whole population fits within maxSize, return all members
 *     (ordered by composite fitness).
 */
declare function selectSurvivors(population: Hypothesis[], objectives: string[], weights: Record<string, number>, maxSize: number): Hypothesis[];
//#endregion
//#region src/workflows/engine-memory.d.ts
declare class InMemoryEngine implements WorkflowEngine {
  private runs;
  startRun(workflowName: string, input: unknown, opts?: {
    runId?: string;
  }): Promise<string>;
  step<T>(runId: string, stepKey: string, handler: () => Promise<T>): Promise<T>;
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;
  getRun(runId: string): Promise<RunSnapshot | null>;
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}
//#endregion
//#region src/workflows/engine-delta.d.ts
interface DeltaEngineConfig {
  /**
   * Fully-qualified table prefix, e.g. `main.apx_agent.workflow`. The engine
   * writes to `${tablePrefix}_runs` and `${tablePrefix}_steps`.
   */
  tablePrefix: string;
  /** Databricks workspace host. Defaults to `DATABRICKS_HOST`. */
  host?: string;
  /** SQL warehouse ID. Defaults to `DATABRICKS_WAREHOUSE_ID`. */
  warehouseId?: string;
  /**
   * Whether to cache step lookups in-process. Default true. Disable for
   * tests that want every call to round-trip.
   */
  cacheEnabled?: boolean;
}
declare class DeltaEngine implements WorkflowEngine {
  private host;
  private warehouseId;
  private runsTable;
  private stepsTable;
  private cacheEnabled;
  private stepCache;
  private bootstrapPromise;
  constructor(config: DeltaEngineConfig);
  startRun(workflowName: string, input: unknown, opts?: {
    runId?: string;
  }): Promise<string>;
  step<T>(runId: string, stepKey: string, handler: () => Promise<T>): Promise<T>;
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;
  getRun(runId: string): Promise<RunSnapshot | null>;
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
  /** Drop all in-process caches. Useful for tests. */
  clearCache(): void;
  private bootstrap;
  private lookupStep;
  private persistStep;
  private executeSql;
}
//#endregion
//#region src/workflows/engine-inngest.d.ts
/** Minimal shape of Inngest's `step` object that this adapter needs. */
interface InngestStep {
  run<T>(id: string, handler: () => Promise<T>): Promise<T>;
}
declare class InngestEngine implements WorkflowEngine {
  private step$;
  private runs;
  constructor(step: InngestStep);
  startRun(workflowName: string, input: unknown, opts?: {
    runId?: string;
  }): Promise<string>;
  step<T>(_runId: string, stepKey: string, handler: () => Promise<T>): Promise<T>;
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;
  getRun(runId: string): Promise<RunSnapshot | null>;
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}
//#endregion
//#region src/eval/predict.d.ts
/**
 * predict.ts — Eval bridge: create a predict function for any /responses endpoint.
 *
 * TypeScript equivalent of Python's app_predict_fn().
 */
interface Message$1 {
  role: string;
  content: string;
}
interface PredictInput {
  messages: Message$1[];
}
type PredictFn = (input: PredictInput | string) => Promise<string>;
interface PredictOptions {
  /** Bearer token for Authorization header. */
  token?: string;
}
/**
 * Create a predict function that calls a /responses endpoint.
 *
 * @param url - Base URL of the agent (e.g. "http://localhost:8000")
 * @param options - Optional config (token for auth)
 * @returns Async function that accepts messages or a plain string and returns output_text
 *
 * @example
 * const predict = createPredictFn('http://localhost:8000', { token: process.env.TOKEN });
 * const output = await predict('What is 2+2?');
 * const output2 = await predict({ messages: [{ role: 'user', content: 'Hello' }] });
 */
declare function createPredictFn(url: string, options?: PredictOptions): PredictFn;
//#endregion
//#region src/eval/harness.d.ts
interface EvalCase {
  /** Input to send to the agent. */
  input: string;
  /** If provided, output must include this string to pass. */
  expected?: string;
  /** Optional tags for grouping/filtering. */
  tags?: string[];
}
interface EvalResult {
  input: string;
  output: string;
  expected?: string;
  /** True = output contains expected string. Undefined if no expected provided. */
  passed?: boolean;
  latency_ms: number;
  error?: string;
}
interface RunEvalOptions {
  /** Maximum number of cases to run in parallel. Defaults to 5. */
  concurrency?: number;
  /** If true, log each result to stdout as it completes. Defaults to false. */
  verbose?: boolean;
}
interface EvalSummary {
  total: number;
  passed: number;
  failed: number;
  errored: number;
  avg_latency_ms: number;
  results: EvalResult[];
}
/**
 * Run all eval cases against the given predict function.
 *
 * Cases are executed in batches respecting the concurrency limit.
 * Latency is measured per case. Pass/fail is determined by simple
 * string inclusion of `expected` in the output.
 *
 * @example
 * const predict = createPredictFn('http://localhost:8000');
 * const summary = await runEval(predict, [
 *   { input: 'What is 2+2?', expected: '4' },
 *   { input: 'Capital of France?', expected: 'Paris' },
 * ]);
 * console.log(`Passed: ${summary.passed}/${summary.total}`);
 */
declare function runEval(predictFn: PredictFn, cases: EvalCase[], options?: RunEvalOptions): Promise<EvalSummary>;
//#endregion
//#region src/genie.d.ts
interface GenieToolOptions {
  /** Tool name shown to the LLM. Defaults to `"ask_genie"`. */
  name?: string;
  /** Tool description shown to the LLM. */
  description?: string;
  /** Databricks workspace host. Falls back to `DATABRICKS_HOST` env var. */
  host?: string;
  /**
   * OBO headers forwarded from the incoming request.
   * Pass `req.headers` (as a plain object) to inherit the user's token.
   * Falls back to `DATABRICKS_TOKEN` env var for local dev.
   */
  oboHeaders?: Record<string, string>;
}
/**
 * Create an AgentTool that queries a Genie space by natural-language conversation.
 *
 * @param spaceId - Genie space ID (the UUID from the Genie space URL).
 * @param opts    - Optional overrides for name, description, host, and auth headers.
 */
declare function genieTool(spaceId: string, opts?: GenieToolOptions): AgentTool;
//#endregion
//#region src/catalog.d.ts
interface CatalogToolOptions {
  /** Tool name shown to the LLM. */
  name?: string;
  /** Tool description shown to the LLM. */
  description?: string;
  /** Databricks workspace host. Falls back to DATABRICKS_HOST env var. */
  host?: string;
  /** OBO headers forwarded from the incoming request. Falls back to request context or DATABRICKS_TOKEN. */
  oboHeaders?: Record<string, string>;
}
interface UcFunctionToolOptions extends CatalogToolOptions {
  /** SQL warehouse ID. Auto-discovered (prefers serverless) if not provided. */
  warehouseId?: string;
}
/**
 * Create a tool that lists tables in a Unity Catalog schema.
 *
 * The LLM calls this tool with no arguments — the catalog and schema are
 * baked in at construction time.
 *
 * @param catalog - UC catalog name.
 * @param schema  - Schema name within the catalog.
 * @param opts    - Optional name, description, host, and auth overrides.
 */
declare function catalogTool(catalog: string, schema: string, opts?: CatalogToolOptions): AgentTool;
/**
 * Create a tool that fetches upstream/downstream lineage for a UC table.
 *
 * The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
 *
 * @param opts - Optional name, description, host, and auth overrides.
 */
declare function lineageTool(opts?: CatalogToolOptions): AgentTool;
/**
 * Create a tool that describes the columns of a Unity Catalog table.
 *
 * The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
 *
 * @param opts - Optional name, description, host, and auth overrides.
 */
declare function schemaTool(opts?: CatalogToolOptions): AgentTool;
/**
 * Create a tool that executes a Unity Catalog function via SQL.
 *
 * The function definition is fetched from UC on the first call and cached —
 * parameter names, types, and order are derived automatically.
 *
 * @param functionName - Fully qualified UC function name: `catalog.schema.function`.
 * @param opts         - Optional overrides for name, description, host, warehouseId, and auth.
 *
 * @example
 * ucFunctionTool('main.tools.classify_intent', {
 *   description: 'Classify user intent. params: {text, min_confidence}',
 * })
 */
declare function ucFunctionTool(functionName: string, opts?: UcFunctionToolOptions): AgentTool;
//#endregion
//#region src/connectors/types.d.ts
interface FieldDef {
  name: string;
  type: string;
  key?: boolean;
  nullable?: boolean;
  default?: number | string;
  index?: boolean;
}
interface EntityDef {
  name: string;
  table: string;
  fields: FieldDef[];
  embedding_source?: string;
}
interface EdgeDef {
  name: string;
  table: string;
  from: string;
  to: string;
  fields: FieldDef[];
}
interface ExtractionConfig {
  prompt_template: string;
  chunk_size: number;
  chunk_overlap: number;
}
interface FitnessConfig {
  metric: string;
  evaluation: string;
  targets: Record<string, number>;
}
interface EvolutionConfig {
  population_size: number;
  mutation_rate: number;
  mutation_fields: string[];
  selection: string;
  max_generations: number;
}
interface EntitySchema {
  version: number;
  generation: number;
  entities: EntityDef[];
  edges: EdgeDef[];
  extraction: ExtractionConfig;
  fitness: FitnessConfig;
  evolution: EvolutionConfig;
}
interface ConnectorConfig {
  host?: string;
  catalog: string;
  schema: string;
  vectorSearchIndex?: string;
  volumePath?: string;
  entitySchema?: EntitySchema;
}
declare function parseEntitySchema(raw: unknown): EntitySchema;
/**
 * Resolve a Databricks bearer token for an outbound API call.
 *
 * Priority order — checked at call time so per-request OBO tokens are
 * always used when available, not captured at construction time:
 *   1. Explicit `oboHeaders` argument (e.g. passed from the incoming request)
 *   2. `AsyncLocalStorage` request context — set by the agent framework for
 *      every tool handler and sub-agent call; reads `x-forwarded-access-token`
 *      or `authorization` from the user's OBO headers
 *   3. `DATABRICKS_TOKEN` env var — static PAT for local dev
 *   4. M2M OAuth via `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` —
 *      service principal identity for jobs, workflows, and background loops
 *
 * Steps 1-3 are synchronous. Step 4 requires an async token exchange, so
 * this function returns `string | Promise<string>`. All call sites already
 * run in async handlers, so `await resolveToken()` works everywhere.
 */
declare function resolveToken(oboHeaders?: Record<string, string>): Promise<string>;
declare function resolveHost(host?: string): string;
interface SqlParam {
  name: string;
  value: string;
  type: 'STRING' | 'INT' | 'FLOAT' | 'BOOLEAN';
}
declare function buildSqlParams(filters: Record<string, unknown>): {
  clause: string;
  params: SqlParam[];
};
interface DbFetchOptions {
  token: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
}
declare function dbFetch<T = unknown>(url: string, opts: DbFetchOptions): Promise<T>;
//#endregion
//#region src/connectors/lakebase.d.ts
/**
 * Create a Lakebase query tool that executes SELECT statements with
 * optional parameterized filters.
 */
declare function createLakebaseQueryTool(config: ConnectorConfig): AgentTool;
/**
 * Create a Lakebase mutate tool that executes INSERT, UPDATE, or DELETE
 * statements.
 */
declare function createLakebaseMutateTool(config: ConnectorConfig): AgentTool;
/**
 * Create a Lakebase schema inspect tool that queries information_schema.columns
 * for the configured catalog.schema.
 */
declare function createLakebaseSchemaInspectTool(config: ConnectorConfig): AgentTool;
//#endregion
//#region src/connectors/vector-search.d.ts
/**
 * Create a similarity-search tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
declare function createVSQueryTool(config: ConnectorConfig): AgentTool;
/**
 * Create an upsert tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
declare function createVSUpsertTool(config: ConnectorConfig): AgentTool;
/**
 * Create a delete tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
declare function createVSDeleteTool(config: ConnectorConfig): AgentTool;
//#endregion
//#region src/connectors/doc-parser.d.ts
interface Chunk {
  chunk_id: string;
  text: string;
  position: number;
}
/**
 * Split `text` into overlapping chunks.
 *
 * @param text         - Input text to chunk
 * @param chunkSize    - Maximum characters per chunk
 * @param chunkOverlap - Number of characters to overlap between consecutive chunks
 * @returns Array of Chunk objects with sequential chunk_ids and byte positions
 */
declare function chunkText(text: string, chunkSize: number, chunkOverlap: number): Chunk[];
/**
 * Create a tool that uploads a document to a UC Volume via the Files API.
 * Requires `config.volumePath` to be set.
 */
declare function createDocUploadTool(config: ConnectorConfig): AgentTool;
/**
 * Create a tool that splits text into chunks using schema-configured settings.
 */
declare function createDocChunkTool(config: ConnectorConfig): AgentTool;
/**
 * Create a tool that extracts entities from text chunks using an LLM via FMAPI.
 * Requires `config.entitySchema` to be set.
 */
declare function createDocExtractEntitiesTool(config: ConnectorConfig): AgentTool;
//#endregion
export { type AgentCard, type AgentConfig, type AgentExports, AgentState, type AgentTool, type CatalogToolOptions, type ConnectorConfig, DeltaEngine, type DeltaEngineConfig, type DevUIConfig, type DiscoveryConfig, type EdgeDef, type EntityDef, type EntitySchema, type EvalCase, type EvalResult, type EvalSummary, type EvolutionConfig, type EvolutionState, EvolutionaryAgent, type EvolutionaryConfig, type ExtractionConfig, type FieldDef, type FitnessConfig, type FunctionSchema, type GenerationResult, type GenieToolOptions, HandoffAgent, type HandoffConfig, type Hypothesis, InMemoryEngine, InMemorySessionStore, InngestEngine, type InngestStep, LoopAgent, type McpAuthContext, type McpConfig, type Message, ParallelAgent, PopulationStore, type PopulationStoreConfig, type PredictFn, type PredictInput, type PredictOptions, RemoteAgent, type RemoteAgentConfig, type RequestContext, type Route, RouterAgent, type RouterConfig, type RunEvalOptions, type RunFilter, type RunSnapshot, type RunStatus, type RunSummary, type Runnable, SequentialAgent, Session, type SessionSnapshot, type SessionStore, StepFailedError, type StepRecord, type StopPredicate, type Trace, type TraceContext, type TraceSpan, type UcFunctionToolOptions, type WorkflowEngine, addSpan, buildSqlParams, catalogTool, chunkText, compositeFitness, createAgentPlugin, createDevPlugin, createDiscoveryPlugin, createDocChunkTool, createDocExtractEntitiesTool, createDocUploadTool, createHypothesis, createLakebaseMutateTool, createLakebaseQueryTool, createLakebaseSchemaInspectTool, createMcpPlugin, createPredictFn, createTrace, createVSDeleteTool, createVSQueryTool, createVSUpsertTool, dbFetch, defineTool, endSpan, endTrace, genieTool, getDefaultSessionStore, getMcpAuth, getRequestContext, getTrace, getTraces, initDatabricksClient, lineageTool, mcpAuthStore, paretoDominates, paretoFrontier, parseEntitySchema, resolveHost, resolveToken, runEval, runViaSDK, runWithContext, schemaTool, selectSurvivors, setDefaultSessionStore, storeTrace, streamViaSDK, toFunctionTool, toStrictSchema, toSubAgentTool, toolsToFunctionSchemas, truncate, ucFunctionTool, zodToJsonSchema };
//# sourceMappingURL=index.d.mts.map
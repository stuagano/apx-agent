/**
 * chat-agent — TS port of `python/src/apx_agent/_chat_agent.py`.
 *
 * Python wraps an apx-agent as an MLflow `ChatAgent` (the canonical Mosaic AI
 * deploy path) and exposes:
 *
 *   - `compile_to_chat_agent` / `chat_agent_for` — wrap a BaseAgent
 *   - `log_agent` — run `mlflow.pyfunc.log_model` with the wrapped agent and
 *     auto-derived `resources=[...]` from the agent's tools + sub-agents.
 *
 * TypeScript has no MLflow JS SDK, so this port is **injectable**: the caller
 * wires the MLflow surface (`mlflowLogModel`, `mlflowSetExperiment`,
 * `materializeResources`) and we own the orchestration + agent-shape
 * compilation. Same pattern as `evaluate.ts`.
 *
 * What we own here:
 *   - `ChatAgentMessage` / `ChatAgentResponse` / `ChatContext` shapes that
 *     mirror MLflow's `mlflow.types.agent` Pydantic models.
 *   - `compileToChatAgent({agent, model, sessionStore?, runViaSDK?})` →
 *     returns a `ChatAgent`-like object with `predict` / `predictStream`.
 *     Session prepending + persistence, OBO header translation
 *     (`customInputs.user_token` / `workspace_host` → `Authorization`
 *     + `X-Databricks-Host`), and message marshalling between
 *     `ChatAgentMessage` and the runner's `{role, content}` shape.
 *   - `logAgent({agent, model, mlflowLogModel, ...})` → orchestrates the
 *     experiment-set → collect resources → optional materialise → compile →
 *     log sequence.
 *
 * What the caller owns:
 *   - `mlflowLogModel(opts)` — REQUIRED. The JS-side MLflow client / REST
 *     call / test fake that performs the actual `pyfunc.log_model`.
 *   - `mlflowSetExperiment(name)` — optional, mirrors `mlflow.set_experiment`.
 *   - `materializeResources(specs)` — optional. When provided, the caller
 *     turns each `ResourceSpec` into a concrete `mlflow.models.resources.Databricks*`
 *     object before it lands in `mlflowLogModel`. When omitted, the raw
 *     spec array is forwarded — useful for tests and for callers that
 *     handle materialisation inside `mlflowLogModel`.
 *   - `toolIterator(agent)` + `subAgentIterator(agent)` — see `resources.ts`
 *     for the rationale.
 *
 * Known gaps vs. Python:
 *
 *   * **predictStream streams the final-turn text, not per-message updates.**
 *     Python's `predict_stream` yields one `ChatAgentChunk` per langgraph
 *     node update (one chunk per new message — assistant, tool_call,
 *     tool_response). The TS streaming primitive (`streamViaSDK`) buffers
 *     tool-calling turns and streams text chunks for the final assistant
 *     turn only. Each chunk lands as a `ChatAgentChunk` with
 *     `delta.content` containing the incremental text; the same `id` is
 *     used across chunks so consumers can concatenate. Mid-flight tool
 *     calls are not visible to the stream consumer — only via traces.
 *
 *   * **runViaSDK returns a string, not a message list.** Python's predict
 *     returns ALL new messages from the compiled langgraph (assistant +
 *     tool_call + tool_response). The TS runner only surfaces the final
 *     assistant text, so `predict` returns a single assistant message —
 *     the tool-call trail is not reconstructed. Callers that need
 *     tool-message visibility should look at the trace, not the response.
 *
 *   * **Session is `{role, content}`-only.** The `SessionStore` /
 *     `Session` types in `workflows/session.ts` carry `Message[]` (no `id`,
 *     `toolCalls`, `toolCallId`). When prepending history we downcast to
 *     `ChatAgentMessage`; when persisting we store only `{role, content}`.
 *     Tool-call round-tripping through sessions is not supported.
 */

import { z } from 'zod';
import {
  runViaSDK as defaultRunViaSDK,
  streamViaSDK as defaultStreamViaSDK,
} from './agent/runner.js';
import type { RunViaSDKFn, StreamViaSDKFn } from './run-once.js';
import type { AgentConfig, AgentExports } from './agent/plugin.js';
import type { AgentTool } from './agent/tools.js';
import { Session, type SessionStore } from './workflows/session.js';
import { collectResourceSpecs, type ResourceSpec } from './resources.js';

// ---------------------------------------------------------------------------
// MLflow ChatAgent message shapes (mirror mlflow.types.agent)
// ---------------------------------------------------------------------------

/**
 * Mirror of MLflow's `ChatAgentMessage`. Pydantic-style optional fields;
 * `toolCalls` / `toolCallId` use camelCase on the TS side but the Python
 * wire shape is `tool_calls` / `tool_call_id` — the boundary is at the
 * caller's serialisation layer.
 */
export interface ChatAgentMessage {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  id?: string;
  toolCalls?: unknown[];
  toolCallId?: string;
}

/** Mirror of MLflow's `ChatAgentResponse`. */
export interface ChatAgentResponse {
  messages: ChatAgentMessage[];
  customOutputs?: Record<string, unknown>;
}

/**
 * Mirror of MLflow's `ChatContext`. Opaque on the TS side — the field set
 * varies by MLflow version, and apx-agent never inspects it.
 */
export type ChatContext = Record<string, unknown>;

/** A single streaming chunk yielded by `predictStream`. */
export interface ChatAgentChunk {
  delta: ChatAgentMessage;
}

// ---------------------------------------------------------------------------
// Predict input shape
// ---------------------------------------------------------------------------

export interface PredictInput {
  messages: ChatAgentMessage[];
  context?: ChatContext;
  customInputs?: Record<string, unknown>;
}

/** A `ChatAgent`-like object — the value `compileToChatAgent` returns. */
export interface CompiledChatAgent {
  predict(input: PredictInput): Promise<ChatAgentResponse>;
  predictStream(input: PredictInput): AsyncIterable<ChatAgentChunk>;
}

// ---------------------------------------------------------------------------
// Agent shape narrowing (duck-typed match against run-once.ts / evaluate.ts)
// ---------------------------------------------------------------------------

function isExports(agent: unknown): agent is AgentExports {
  return (
    typeof agent === 'object' &&
    agent !== null &&
    typeof (agent as AgentExports).getConfig === 'function'
  );
}

function isConfig(agent: unknown): agent is AgentConfig {
  return (
    typeof agent === 'object' &&
    agent !== null &&
    typeof (agent as AgentConfig).model === 'string'
  );
}

function resolveConfig(agent: unknown): AgentConfig | null {
  if (isExports(agent)) return agent.getConfig();
  if (isConfig(agent)) return agent;
  return null;
}

function resolveTools(agent: unknown, config: AgentConfig | null): AgentTool[] {
  if (isExports(agent)) return agent.getTools();
  return config?.tools ?? [];
}

// ---------------------------------------------------------------------------
// OBO header translation — mirrors evaluate.ts buildOboHeaders.
// Python passes `custom_inputs={"user_token", "workspace_host"}`; the TS
// runner takes an `oboHeaders` bag. We translate both fields out of the
// snake_case `customInputs` keys (matching Python's public surface).
// ---------------------------------------------------------------------------

function buildOboHeaders(
  customInputs: Record<string, unknown> | undefined,
): Record<string, string> {
  if (!customInputs) return {};
  const token = customInputs['user_token'];
  if (typeof token !== 'string' || token.length === 0) return {};
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  const host = customInputs['workspace_host'];
  if (typeof host === 'string' && host.length > 0) {
    headers['X-Databricks-Host'] = host;
  }
  return headers;
}

// ---------------------------------------------------------------------------
// Message conversion helpers
// ---------------------------------------------------------------------------

/** Convert a `ChatAgentMessage` to the `{role, content}` shape runViaSDK expects. */
function toRunnerMessage(m: ChatAgentMessage): { role: string; content: string } {
  return {
    role: m.role,
    content: typeof m.content === 'string' ? m.content : String(m.content ?? ''),
  };
}

// ---------------------------------------------------------------------------
// compileToChatAgent
// ---------------------------------------------------------------------------

const CompileOptionsSchema = z.object({
  model: z.string().min(1, 'model is required and must be non-empty'),
});

export interface CompileToChatAgentOptions {
  /**
   * The agent to wrap. Accepts `AgentExports`, a raw `AgentConfig`, or any
   * shape with `.getConfig()` / `.getTools()` accessors. The agent's
   * configured `instructions`, `tools`, `subAgents`, and `maxIterations`
   * are used by the underlying runner.
   */
  agent: AgentExports | AgentConfig | unknown;
  /**
   * LLM endpoint name. Forwarded to the runner. Unlike `run-once.ts`,
   * `compileToChatAgent` does not auto-fall-back to the agent's configured
   * model — the model is the per-deployment artefact contract.
   */
  model: string;
  /**
   * Optional pluggable session store. When provided AND
   * `customInputs.session_id` is set per-request, history is prepended to
   * the messages handed to the runner, and the new turn (user input +
   * assistant reply) is persisted afterwards.
   */
  sessionStore?: SessionStore;
  /**
   * Injectable runner. Defaults to the real `runViaSDK` from
   * `./agent/runner.js`. Tests pass a mock to avoid the real FMAPI call.
   */
  runViaSDK?: RunViaSDKFn;
  /**
   * Injectable streaming runner. Defaults to `streamViaSDK` from
   * `./agent/runner.js`, which yields text chunks for the final assistant
   * turn (tool-calling turns are buffered, only the final response streams).
   * Tests pass a mock to avoid the real FMAPI streaming call.
   */
  streamViaSDK?: StreamViaSDKFn;
}

/**
 * Compile an apx-agent into a `ChatAgent`-like object with `predict` /
 * `predictStream`. The returned object is the JS analog of Python's
 * `compile_to_chat_agent(agent, model=...)` — typically handed to
 * `logAgent` (or directly invoked from a route handler).
 *
 * The compiled object is **lazy** in the same sense as the Python version:
 * the underlying runner is called per request, so per-request OBO headers
 * (sourced from `customInputs.user_token`) flow through to FMAPI without
 * baking the auth at compile time.
 *
 * @throws ZodError when `model` is missing / empty.
 */
export function compileToChatAgent(opts: CompileToChatAgentOptions): CompiledChatAgent {
  CompileOptionsSchema.parse({ model: opts.model });

  const config = resolveConfig(opts.agent);
  const tools = resolveTools(opts.agent, config);
  const instructions = config?.instructions ?? '';
  const subAgents = config?.subAgents;
  const maxTurns = config?.maxIterations;
  const sessionStore = opts.sessionStore;
  const model = opts.model;
  const runner: RunViaSDKFn = opts.runViaSDK ?? defaultRunViaSDK;
  const streamer: StreamViaSDKFn = opts.streamViaSDK ?? defaultStreamViaSDK;

  // -------------------------------------------------------------------
  // Session helpers — minimal load-or-create that matches Python's
  // `load_or_create_session` semantics on top of the TS SessionStore
  // interface (which is `save/load/delete/list`, no `put`).
  // -------------------------------------------------------------------

  const loadSession = async (
    customInputs: Record<string, unknown> | undefined,
  ): Promise<Session | null> => {
    if (!sessionStore || !customInputs) return null;
    const id = customInputs['session_id'];
    if (typeof id !== 'string' || id.length === 0) return null;

    const existing = await Session.load(id, sessionStore);
    if (existing) return existing;
    return new Session({ id, store: sessionStore });
  };

  const persistTurn = async (
    session: Session | null,
    inputMessages: ChatAgentMessage[],
    newMessages: ChatAgentMessage[],
  ): Promise<void> => {
    if (!session || !sessionStore) return;
    for (const m of inputMessages) {
      session.addMessage(m.role, m.content);
    }
    for (const m of newMessages) {
      session.addMessage(m.role, m.content);
    }
    await session.save();
  };

  const buildHistoryMessages = (session: Session | null): ChatAgentMessage[] => {
    if (!session) return [];
    return session.getHistory().map((m) => ({
      role: (m.role as ChatAgentMessage['role']) ?? 'user',
      content: m.content,
    }));
  };

  // -------------------------------------------------------------------
  // predict
  // -------------------------------------------------------------------

  const predict = async (input: PredictInput): Promise<ChatAgentResponse> => {
    const session = await loadSession(input.customInputs);
    const historyMessages = buildHistoryMessages(session);
    const prepended: ChatAgentMessage[] = [...historyMessages, ...input.messages];

    const oboHeaders = buildOboHeaders(input.customInputs);

    const finalText = await runner({
      model,
      instructions,
      messages: prepended.map(toRunnerMessage),
      tools,
      subAgents,
      maxTurns,
      oboHeaders,
    });

    // The runner returns a single assistant string. See module-level docs:
    // tool-call intermediate messages are not reconstructed here.
    const assistantMessage: ChatAgentMessage = {
      role: 'assistant',
      content: finalText,
      id: `msg-0`,
    };

    const response: ChatAgentResponse = { messages: [assistantMessage] };

    // Persist the original (non-prepended) input plus the new messages.
    await persistTurn(session, input.messages, response.messages);

    return response;
  };

  // -------------------------------------------------------------------
  // predictStream — yields one ChatAgentChunk per text chunk from the
  // underlying streaming runner. Tool-calling turns are still buffered by
  // the runner; only the final assistant turn streams. After the stream
  // closes, the assembled assistant message is persisted to the session
  // store (if any), matching `predict`'s persistence behavior.
  // -------------------------------------------------------------------

  async function* predictStream(input: PredictInput): AsyncIterable<ChatAgentChunk> {
    const session = await loadSession(input.customInputs);
    const historyMessages = buildHistoryMessages(session);
    const prepended: ChatAgentMessage[] = [...historyMessages, ...input.messages];

    const oboHeaders = buildOboHeaders(input.customInputs);

    const id = 'msg-0';
    let assembled = '';
    for await (const chunk of streamer({
      model,
      instructions,
      messages: prepended.map(toRunnerMessage),
      tools,
      subAgents,
      maxTurns,
      oboHeaders,
    })) {
      if (typeof chunk !== 'string' || chunk.length === 0) continue;
      assembled += chunk;
      yield { delta: { role: 'assistant', content: chunk, id } };
    }

    const finalMessage: ChatAgentMessage = {
      role: 'assistant',
      content: assembled,
      id,
    };
    await persistTurn(session, input.messages, [finalMessage]);
  }

  return { predict, predictStream };
}

// ---------------------------------------------------------------------------
// logAgent
// ---------------------------------------------------------------------------

const LogOptionsSchema = z.object({
  model: z.string().min(1, 'model is required and must be non-empty'),
  registeredModelName: z.string().min(1).optional(),
  artifactPath: z.string().min(1).optional(),
  experiment: z.string().min(1).optional(),
});

/**
 * Result mirroring Python's `mlflow.models.model.ModelInfo`. The shape is
 * intentionally loose — different MLflow surfaces (Python, JS shim, REST)
 * return different envelopes. Callers should narrow on the concrete
 * `mlflowLogModel` they wire in.
 */
export interface LogAgentResult {
  registeredModelVersion?: string;
  modelInfo?: unknown;
  [key: string]: unknown;
}

export interface MlflowLogModelArgs {
  artifactPath: string;
  pythonModel: unknown;
  resources: unknown[];
  registeredModelName?: string;
  inputExample?: unknown;
  pipRequirements?: string[];
  [key: string]: unknown;
}

export type MlflowLogModelFn = (args: MlflowLogModelArgs) => Promise<LogAgentResult>;

export type MlflowSetExperimentFn = (experiment: string) => void | Promise<void>;

export interface LogAgentOptions {
  /** The agent to log. Same shape acceptance as `compileToChatAgent`. */
  agent: AgentExports | AgentConfig | unknown;
  /** LLM endpoint name. Forwarded to the compiled ChatAgent and added as a `serving_endpoint` resource. */
  model: string;
  /** UC three-part name (e.g. `main.agents.triage`). When omitted, the model is logged but not registered. */
  registeredModelName?: string;
  /** Subdirectory in the run's artifact store. Default `agent`. */
  artifactPath?: string;
  /**
   * Extra resources — typically `ResourceSpec` instances the iterators
   * couldn't infer (SQL warehouses, UC tables, vector indices). The Python
   * version also accepts pre-built `mlflow.models.resources.Databricks*`
   * objects in this list; in TS that pre-built form is opaque, so it's the
   * caller's job to either pass `ResourceSpec`s or handle the pre-built
   * objects inside `materializeResources` / `mlflowLogModel`.
   */
  extraResources?: ResourceSpec[];
  /**
   * Optional MLflow experiment name. When set AND `mlflowSetExperiment`
   * is also set, the setter is called before resource collection so a
   * setter failure short-circuits cleanly.
   */
  experiment?: string;
  /** Optional session store passed straight through to `compileToChatAgent`. */
  sessionStore?: SessionStore;
  /** REQUIRED. The JS-side `mlflow.pyfunc.log_model` equivalent. */
  mlflowLogModel: MlflowLogModelFn;
  /** Optional `mlflow.set_experiment` equivalent. */
  mlflowSetExperiment?: MlflowSetExperimentFn;
  /**
   * Yields every raw tool callable in the agent tree. Forwarded to
   * `collectResourceSpecs`.
   */
  toolIterator: (agent: unknown) => Iterable<unknown>;
  /**
   * Yields every raw sub-agent URL / `$VAR` in the agent tree. Forwarded
   * to `collectResourceSpecs`.
   */
  subAgentIterator: (agent: unknown) => Iterable<string>;
  /**
   * Optional materialiser. When provided, called with the collected
   * `ResourceSpec[]` and its return value is forwarded as `resources` to
   * `mlflowLogModel`. When omitted, the raw `ResourceSpec[]` is forwarded
   * — useful for tests, and for callers that materialise inside
   * `mlflowLogModel`.
   */
  materializeResources?: (specs: ResourceSpec[]) => unknown[];
  /** Optional example payload passed through to `mlflowLogModel`. */
  inputExample?: unknown;
  /** Optional pip requirements passed through to `mlflowLogModel`. */
  pipRequirements?: string[];
  /** Extra kwargs forwarded verbatim to `mlflowLogModel`. */
  mlflowKwargs?: Record<string, unknown>;
}

/**
 * Log an apx-agent as an MLflow ChatAgent with auto-derived resources.
 *
 * Equivalent to Python's `log_agent(...)`. Returns whatever `mlflowLogModel`
 * returns (typically a `ModelInfo`-shaped object).
 *
 * Order of operations (matches Python):
 *   1. Validate options (Zod).
 *   2. Confirm `mlflowLogModel` is a function (fails loud — matches
 *      evaluate.ts pattern; the `mlflowEvaluate` injectable is the
 *      precedent).
 *   3. If `experiment` + `mlflowSetExperiment` are both set, call the
 *      setter (and propagate failures with a friendly wrapper).
 *   4. Collect resources via `collectResourceSpecs`.
 *   5. Materialise resources if `materializeResources` is provided.
 *   6. Compile the agent into a `CompiledChatAgent`.
 *   7. Call `mlflowLogModel`.
 *
 * @throws ZodError when required string options are missing / empty.
 * @throws Error when `mlflowLogModel` is missing or not a function.
 * @throws Error when `mlflowSetExperiment` fails (wrapped with context).
 */
export async function logAgent(opts: LogAgentOptions): Promise<LogAgentResult> {
  LogOptionsSchema.parse({
    model: opts.model,
    registeredModelName: opts.registeredModelName,
    artifactPath: opts.artifactPath,
    experiment: opts.experiment,
  });

  if (typeof opts.mlflowLogModel !== 'function') {
    throw new Error(
      'logAgent requires an `mlflowLogModel` injectable. Wire the JS mlflow ' +
        'client / REST call / test fake of your choice.',
    );
  }
  if (typeof opts.toolIterator !== 'function') {
    throw new Error(
      'logAgent requires a `toolIterator(agent)` callable. See ' +
        '`collectResourceSpecs` for the contract.',
    );
  }
  if (typeof opts.subAgentIterator !== 'function') {
    throw new Error(
      'logAgent requires a `subAgentIterator(agent)` callable. See ' +
        '`collectResourceSpecs` for the contract.',
    );
  }

  // ---- experiment routing -------------------------------------------------
  if (opts.experiment) {
    if (opts.mlflowSetExperiment) {
      try {
        await opts.mlflowSetExperiment(opts.experiment);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        throw new Error(
          `mlflowSetExperiment(${JSON.stringify(opts.experiment)}) failed: ${message}. ` +
            `For Databricks-hosted MLflow, experiment names are workspace ` +
            `paths (e.g. '/Users/you@company.com/agents/my_agent').`,
        );
      }
    } else {
      // No-op with a visible warning — matches evaluate.ts.
      // eslint-disable-next-line no-console
      console.warn(
        `logAgent: experiment="${opts.experiment}" was set but no ` +
          `mlflowSetExperiment was provided; skipping experiment routing.`,
      );
    }
  }

  // ---- resource collection ------------------------------------------------
  const specs = collectResourceSpecs({
    agent: opts.agent,
    model: opts.model,
    extra: opts.extraResources,
    toolIterator: opts.toolIterator,
    subAgentIterator: opts.subAgentIterator,
  });

  const resources: unknown[] = opts.materializeResources
    ? opts.materializeResources(specs)
    : specs;

  // ---- compile + log ------------------------------------------------------
  const pythonModel = compileToChatAgent({
    agent: opts.agent,
    model: opts.model,
    sessionStore: opts.sessionStore,
  });

  const artifactPath = opts.artifactPath ?? 'agent';

  const logArgs: MlflowLogModelArgs = {
    artifactPath,
    pythonModel,
    resources,
    ...(opts.mlflowKwargs ?? {}),
  };
  if (opts.registeredModelName !== undefined) {
    logArgs.registeredModelName = opts.registeredModelName;
  }
  if (opts.inputExample !== undefined) {
    logArgs.inputExample = opts.inputExample;
  }
  if (opts.pipRequirements !== undefined) {
    logArgs.pipRequirements = opts.pipRequirements;
  }

  return await opts.mlflowLogModel(logArgs);
}

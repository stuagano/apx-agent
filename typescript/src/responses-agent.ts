/**
 * Compile an apx-agent into the Databricks Apps ResponsesAgent contract —
 * TS port of `python/src/apx_agent/_responses_agent.py`.
 *
 * This is the **Apps-native peer** of {@link compileToChatAgent}. Same agent
 * definition, different runtime contract:
 *
 *   * **Model Serving** (ChatAgent): `mlflow.pyfunc.log_model` + UC + container
 *     build queue + `databricks.agents.deploy`. Wire shape is
 *     `ChatAgentRequest` (messages-style). See {@link compileToChatAgent}.
 *   * **Databricks Apps** (this module): a Node.js process serves
 *     `POST /invocations` directly via {@link mountResponsesAgent}, deployed
 *     via `databricks bundle deploy && bundle run`. Wire shape is
 *     `ResponsesAgentRequest` (input items + customInputs).
 *
 * Author writes the agent once; the `--target` flag picks the runtime at
 * deploy time. The single load-bearing requirement is that **OBO header
 * passthrough is identical** — both runtimes route through
 * {@link extractOboHeaders}, so a tool sees the calling user's token
 * regardless of which runtime served the request.
 *
 * # Decorator vs object shape
 *
 * The Python `_responses_agent.py` returns a `(non_streaming_fn,
 * streaming_fn)` tuple because mlflow's `@invoke()` / `@stream()` decorators
 * register globally on import and would clobber each other if called twice.
 * TypeScript has no such constraint — we return a clean `{invoke, stream}`
 * object that the caller hands to {@link mountResponsesAgent} (or wires into
 * any HTTP framework of their choice).
 *
 * # Streaming caveat
 *
 * The TS streaming runner yields incremental text deltas for assistant turns,
 * but tool-call rounds are not surfaced as stream events. The `stream()`
 * method therefore yields the assembled assistant text as ONE
 * `response.output_item.done`, followed by `response.completed`. Mid-flight
 * tool events are not surfaced (matches the chat-agent.ts streaming gap; see
 * that module header for the rationale).
 *
 * # Non-streaming output-shape caveat (parity gap)
 *
 * `invoke()` returns a SINGLE assistant message item built from the runner's
 * final text. It does NOT emit the intermediate `function_call` /
 * `function_call_output` items that the Python `_responses_agent.py` includes
 * in its `output` array. This is structural: the TS runner's return type is
 * `Promise<string>` (see {@link RunViaSDKFn}), so it cannot surface
 * intermediate tool items without a runner-signature change. Callers that
 * depend on inspecting tool-call steps from the non-streaming output array
 * will see only the final assistant message in the TS runtime.
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
import { extractOboHeaders, type HeadersInput } from './obo.js';

// ---------------------------------------------------------------------------
// Wire types — mirror mlflow.types.responses
// ---------------------------------------------------------------------------

/**
 * A single ResponsesAgent input item. The Apps API accepts user / assistant
 * turns plus tool messages (per the OpenAI Responses spec).
 */
export interface ResponsesAgentInputItem {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string | Array<{ text?: string; type?: string; [k: string]: unknown }>;
  /** Tool message linkage. */
  tool_call_id?: string;
  /** Free-form (e.g. id, status) — opaque on the TS side. */
  [k: string]: unknown;
}

/** A ResponsesAgent request body. */
export interface ResponsesAgentRequest {
  input: ResponsesAgentInputItem[];
  customInputs?: Record<string, unknown>;
  /** Apps sometimes uses snake_case; we accept either. */
  custom_inputs?: Record<string, unknown>;
  context?: Record<string, unknown>;
  /** Per-request headers — populated by the route layer, not by the user. */
  headers?: HeadersInput;
}

/** A single output item — message, function_call, or function_call_output. */
export interface ResponsesAgentOutputItem {
  type: string;
  [k: string]: unknown;
}

export interface ResponsesAgentResponse {
  id?: string;
  object?: 'response';
  output: ResponsesAgentOutputItem[];
}

export interface ResponsesAgentStreamEvent {
  type: 'response.output_item.done' | 'response.completed' | string;
  [k: string]: unknown;
}

// ---------------------------------------------------------------------------
// Agent shape narrowing (same heuristics as chat-agent.ts)
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
// Input-item helpers
// ---------------------------------------------------------------------------

function itemContent(item: ResponsesAgentInputItem): string {
  const c = item.content;
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c
      .map((p) => (typeof p === 'object' && p && typeof p.text === 'string' ? p.text : ''))
      .join('');
  }
  return '';
}

function inputToRunnerMessages(
  items: ResponsesAgentInputItem[],
): Array<{ role: string; content: string }> {
  return items.map((item) => ({
    role: item.role ?? 'user',
    content: itemContent(item),
  }));
}

function makeMessageOutputItem(id: string, text: string): ResponsesAgentOutputItem {
  return {
    type: 'message',
    role: 'assistant',
    id,
    status: 'completed',
    content: [{ type: 'output_text', text, annotations: [] }],
  };
}

function shortId(prefix: string): string {
  // Lightweight `crypto.randomUUID().slice(0, 12)` equivalent — avoids the
  // node:crypto import overhead in hot paths.
  return `${prefix}-${Math.random().toString(36).slice(2, 14)}`;
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const CompileOptionsSchema = z.object({
  model: z.string().min(1, 'model is required and must be non-empty'),
});

export interface CompileToResponsesAgentOptions {
  /**
   * The agent to wrap. Accepts `AgentExports`, a raw `AgentConfig`, or any
   * shape with `.getConfig()` / `.getTools()` accessors.
   */
  agent: AgentExports | AgentConfig | unknown;
  /** LLM endpoint name. */
  model: string;
  /**
   * Optional session store. When set AND `customInputs.thread_id` (or
   * `session_id`, for symmetry with the ChatAgent target) is present per
   * request, history is prepended to the input and the new turn is
   * persisted after the run.
   */
  sessionStore?: SessionStore;
  /** Injectable runner. Defaults to the real `runViaSDK`. */
  runViaSDK?: RunViaSDKFn;
  /** Injectable streaming runner. */
  streamViaSDK?: StreamViaSDKFn;
}

/** Return type of {@link compileToResponsesAgent}. */
export interface CompiledResponsesAgent {
  invoke(request: ResponsesAgentRequest): Promise<ResponsesAgentResponse>;
  stream(request: ResponsesAgentRequest): AsyncIterable<ResponsesAgentStreamEvent>;
}

// ---------------------------------------------------------------------------
// compileToResponsesAgent
// ---------------------------------------------------------------------------

/**
 * Compile an apx-agent into a ResponsesAgent-shaped object. The returned
 * `{invoke, stream}` pair is suitable for direct mounting via
 * {@link mountResponsesAgent} or for use inside any HTTP framework.
 *
 * Both `invoke` and `stream` resolve OBO context through {@link
 * extractOboHeaders}, looking at both `request.customInputs` and
 * `request.headers` (precedence: customInputs wins). The resolved
 * `userToken` becomes the `Authorization: Bearer ...` passed to the runner.
 */
export function compileToResponsesAgent(
  opts: CompileToResponsesAgentOptions,
): CompiledResponsesAgent {
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
  // Session helpers — thread_id is the Apps convention; we also accept
  // session_id for symmetry with the ChatAgent target.
  // -------------------------------------------------------------------

  const loadSession = async (
    customInputs: Record<string, unknown> | undefined,
  ): Promise<Session | null> => {
    if (!sessionStore || !customInputs) return null;
    const tid = customInputs['thread_id'] ?? customInputs['session_id'];
    if (typeof tid !== 'string' || tid.length === 0) return null;
    const existing = await Session.load(tid, sessionStore);
    if (existing) return existing;
    return new Session({ id: tid, store: sessionStore });
  };

  const persistTurn = async (
    session: Session | null,
    inputItems: ResponsesAgentInputItem[],
    outputItems: ResponsesAgentOutputItem[],
  ): Promise<void> => {
    if (!session || !sessionStore) return;
    for (const item of inputItems) {
      session.addMessage(item.role ?? 'user', itemContent(item));
    }
    for (const item of outputItems) {
      // Reduce each output item to a {role, content} for storage. We only
      // store assistant text messages — function_call / function_call_output
      // items are not (yet) round-tripped through sessions, matching
      // chat-agent.ts which also doesn't store tool messages.
      if (item.type === 'message') {
        const contentArr = item['content'] as unknown;
        let text = '';
        if (Array.isArray(contentArr)) {
          for (const piece of contentArr as Array<Record<string, unknown>>) {
            if (typeof piece['text'] === 'string') text += piece['text'];
          }
        }
        session.addMessage('assistant', text);
      }
    }
    await session.save();
  };

  const buildHistoryMessages = (
    session: Session | null,
  ): Array<{ role: string; content: string }> => {
    if (!session) return [];
    return session.getHistory().map((m) => ({ role: m.role, content: m.content }));
  };

  // -------------------------------------------------------------------
  // OBO → fetch-header bag. Mirrors chat-agent.ts buildOboHeaders so the
  // wire shape into runViaSDK is identical between runtimes.
  // -------------------------------------------------------------------

  const buildOboFetchHeaders = (request: ResponsesAgentRequest): Record<string, string> => {
    const customInputs = (request.customInputs ?? request.custom_inputs) as
      | Record<string, unknown>
      | undefined;
    const obo = extractOboHeaders({ customInputs, headers: request.headers });
    if (!obo.userToken) return {};
    const headers: Record<string, string> = {
      Authorization: `Bearer ${obo.userToken}`,
    };
    if (obo.workspaceHost) headers['X-Databricks-Host'] = obo.workspaceHost;
    return headers;
  };

  // -------------------------------------------------------------------
  // invoke (non-streaming)
  // -------------------------------------------------------------------

  const invoke = async (
    request: ResponsesAgentRequest,
  ): Promise<ResponsesAgentResponse> => {
    const customInputs = (request.customInputs ?? request.custom_inputs) as
      | Record<string, unknown>
      | undefined;

    const session = await loadSession(customInputs);
    const history = buildHistoryMessages(session);
    const incoming = inputToRunnerMessages(request.input ?? []);
    const messages = [...history, ...incoming];

    const oboHeaders = buildOboFetchHeaders(request);

    const finalText = await runner({
      model,
      instructions,
      messages,
      tools,
      subAgents,
      maxTurns,
      oboHeaders,
    });

    const messageItem = makeMessageOutputItem('msg-0', finalText);
    const output: ResponsesAgentOutputItem[] = [messageItem];

    await persistTurn(session, request.input ?? [], output);

    return {
      id: shortId('resp'),
      object: 'response',
      output,
    };
  };

  // -------------------------------------------------------------------
  // stream
  // -------------------------------------------------------------------

  async function* stream(
    request: ResponsesAgentRequest,
  ): AsyncIterable<ResponsesAgentStreamEvent> {
    const customInputs = (request.customInputs ?? request.custom_inputs) as
      | Record<string, unknown>
      | undefined;

    const session = await loadSession(customInputs);
    const history = buildHistoryMessages(session);
    const incoming = inputToRunnerMessages(request.input ?? []);
    const messages = [...history, ...incoming];

    const oboHeaders = buildOboFetchHeaders(request);

    let assembled = '';
    for await (const chunk of streamer({
      model,
      instructions,
      messages,
      tools,
      subAgents,
      maxTurns,
      oboHeaders,
    })) {
      if (typeof chunk === 'string' && chunk.length > 0) assembled += chunk;
    }

    const messageItem = makeMessageOutputItem('msg-0', assembled);

    yield {
      type: 'response.output_item.done',
      item: messageItem,
      output_index: 0,
    };

    const response: ResponsesAgentResponse = {
      id: shortId('resp'),
      object: 'response',
      output: [messageItem],
    };
    yield { type: 'response.completed', response };

    await persistTurn(session, request.input ?? [], [messageItem]);
  }

  return { invoke, stream };
}

// ---------------------------------------------------------------------------
// mountResponsesAgent — Express POST /invocations bridge
// ---------------------------------------------------------------------------

/** Minimal Express-like surface so this file doesn't hard-depend on Express. */
interface ExpressLike {
  post(path: string, handler: (req: unknown, res: unknown) => unknown): unknown;
}

export interface MountResponsesAgentOptions {
  /** Express app or Router. */
  app: ExpressLike;
  /** A compiled responses agent. */
  agent: CompiledResponsesAgent;
  /** Mount path. Default `"/invocations"`. */
  path?: string;
  /**
   * API prefix used for the mirrored mount. Mounted at both `path` and
   * `${apiPrefix}${path}`. Default `"/api"`.
   */
  apiPrefix?: string;
}

/**
 * Mount `POST /invocations` on an Express app/Router. Mirrors the Python
 * Apps invoke endpoint:
 *
 *   POST /invocations
 *   { "input": [{ "role": "user", "content": "..." }],
 *     "customInputs": {...} }
 *
 * The `X-Forwarded-Access-Token` header (injected by the Databricks Apps
 * proxy) is read directly by the compiled agent through
 * {@link extractOboHeaders}; no caller bridging is needed.
 *
 * Set `customInputs.stream = true` (or top-level `stream: true`) for an
 * NDJSON stream — one `ResponsesAgentStreamEvent` per line.
 *
 * @returns true on success, false if the agent isn't shaped correctly.
 */
export function mountResponsesAgent(opts: MountResponsesAgentOptions): boolean {
  const path = opts.path ?? '/invocations';
  const apiPrefix = opts.apiPrefix ?? '/api';
  const agent = opts.agent;

  if (!agent || typeof agent.invoke !== 'function') {
    return false;
  }
  const hasStream = typeof agent.stream === 'function';

  const handler = async (rawReq: unknown, rawRes: unknown): Promise<void> => {
    const req = rawReq as {
      body?: Record<string, unknown>;
      headers?: Record<string, string | string[] | undefined>;
    };
    const res = rawRes as {
      status: (code: number) => typeof res;
      json: (body: unknown) => typeof res;
      setHeader: (name: string, value: string) => typeof res;
      write: (chunk: string) => boolean;
      end: () => void;
    };
    const body = (req.body ?? {}) as Record<string, unknown>;

    const inputRaw = body['input'];
    if (!Array.isArray(inputRaw)) {
      res.status(400).json({
        error:
          'Request body must include an "input" array of ResponsesAgent input items.',
      });
      return;
    }

    const customInputs =
      (body['customInputs'] as Record<string, unknown> | undefined) ??
      (body['custom_inputs'] as Record<string, unknown> | undefined) ??
      undefined;

    const streamFlag =
      (customInputs && customInputs['stream'] === true) || body['stream'] === true;

    // Strip the meta-flag from the customInputs forwarded into the agent.
    const cleanedCustom = customInputs ? { ...customInputs } : undefined;
    if (cleanedCustom && 'stream' in cleanedCustom) delete cleanedCustom['stream'];

    const request: ResponsesAgentRequest = {
      input: inputRaw as ResponsesAgentInputItem[],
      customInputs: cleanedCustom,
      headers: req.headers ?? undefined,
    };

    if (streamFlag && hasStream) {
      res.status(200);
      res.setHeader('Content-Type', 'application/x-ndjson');
      res.setHeader('Cache-Control', 'no-cache');
      try {
        for await (const event of agent.stream(request)) {
          res.write(`${JSON.stringify(event)}\n`);
        }
        res.end();
      } catch (err) {
        try {
          res.write(`${JSON.stringify({ error: 'Internal error during stream.' })}\n`);
        } catch {
          /* connection may be closed */
        }
        res.end();
        // Swallow err — already reported on the wire.
        void err;
      }
      return;
    }

    try {
      const response = await agent.invoke(request);
      res.status(200).json(response);
    } catch {
      res.status(500).json({ error: 'Internal error processing invocation.' });
    }
  };

  const mirror = `${apiPrefix}${path}`;
  opts.app.post(path, handler as (req: unknown, res: unknown) => unknown);
  if (mirror !== path) {
    opts.app.post(mirror, handler as (req: unknown, res: unknown) => unknown);
  }
  return true;
}

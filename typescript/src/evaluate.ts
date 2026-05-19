/**
 * evaluate — Mosaic AI Agent Evaluation bridge for apx-agent (TS port).
 *
 * Python's `apx_agent.evaluate` wraps `mlflow.genai.evaluate`. JS has no
 * `mlflow.genai` SDK, so this module ports the *orchestration* logic and
 * exposes an injectable `mlflowEvaluate` primitive — the caller wires
 * whatever they have (REST call, mlflow JS shim, fake for tests).
 *
 * What this module owns:
 *   - Build a `predict_fn(inputs)` that normalises the eval-row shape
 *     and dispatches each row through a `runViaSDK`-compatible runner
 *     against the agent's tools + model.
 *   - Thread `userToken` / `workspaceHost` through as OBO headers.
 *   - Optionally call `mlflowSetExperiment` before the eval.
 *   - Forward arbitrary extra kwargs to the injected `mlflowEvaluate`.
 *
 * What the caller owns:
 *   - The `mlflowEvaluate` implementation (mandatory).
 *   - An `mlflowSetExperiment` if they want experiment routing (optional).
 *   - A `defaultScorers` list if they want a "no scorers passed" fallback
 *     (optional — Python imports Correctness + RelevanceToQuery here; in
 *     TS there's no equivalent so the caller wires the default).
 *
 * OBO mapping (Python → TS): Python uses `custom_inputs={"user_token", "workspace_host"}`
 * passed to a compiled ChatAgent. TS `runViaSDK` instead takes an
 * `oboHeaders: Record<string, string>` bag. We translate:
 *   - userToken set       → oboHeaders.Authorization = `Bearer ${userToken}`
 *   - workspaceHost set   → oboHeaders['X-Databricks-Host'] = workspaceHost
 *   - userToken unset     → oboHeaders = {}
 */

import { z } from 'zod';
import { runViaSDK as defaultRunViaSDK } from './agent/runner.js';
import type { RunViaSDKFn } from './run-once.js';
import type { AgentConfig, AgentExports } from './agent/plugin.js';
import type { AgentTool } from './agent/tools.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Minimal message shape used by extractMessages / extractResponseText. */
export interface EvalMessage {
  role?: string;
  content?: string;
  id?: string;
  [key: string]: unknown;
}

/**
 * Shape of `mlflow.genai.evaluate` (or any test fake) accepted by `evaluate`.
 *
 * The caller decides what backs this: a JS mlflow client, a REST call to a
 * Databricks endpoint, or a stub. The first three args are positional in
 * spirit but passed as a single options object for JS ergonomics; any
 * extra kwargs are forwarded verbatim under `extraKwargs`.
 */
export interface MlflowEvaluateArgs {
  /** The evalset as accepted by mlflow (DataFrame-like, list, path, URI). */
  data: unknown;
  /** The predict function the eval runner will call per row. */
  predict_fn: (inputs: unknown) => Promise<string>;
  /** Scorers list, or undefined when the caller didn't pass any. */
  scorers?: unknown[];
  /** Extra kwargs forwarded verbatim (experiment_id, model_id, etc.). */
  [key: string]: unknown;
}

export type MlflowEvaluateFn = (args: MlflowEvaluateArgs) => Promise<unknown>;

/** Optional experiment-setter — Python's `mlflow.set_experiment`. */
export type MlflowSetExperimentFn = (experiment: string) => void | Promise<void>;

// ---------------------------------------------------------------------------
// EvalOptions
// ---------------------------------------------------------------------------

const EvalOptionsSchema = z.object({
  model: z.string().min(1, 'model is required and must be non-empty'),
  experiment: z.string().min(1).optional(),
  userToken: z.string().min(1).optional(),
  workspaceHost: z.string().min(1).optional(),
});

export interface EvalOptions {
  /**
   * The apx-agent to evaluate. Duck-typed: accepts `AgentExports`
   * (returned by `createAgentPlugin(...).exports()`), a raw `AgentConfig`,
   * or any shape with `.getConfig()` / `.getTools()` accessors. Used to
   * source tools, instructions, sub-agents, and a fallback model.
   */
  agent: AgentExports | AgentConfig | unknown;
  /** LLM endpoint name (e.g. 'databricks-claude-sonnet-4-6'). */
  model: string;
  /**
   * Eval dataset. Passed straight through to `mlflowEvaluate` — accept
   * whatever your `mlflowEvaluate` impl accepts (array of dicts,
   * DataFrame-like, path / URI string).
   */
  evalset: unknown;
  /**
   * Scorers list to forward to `mlflowEvaluate`.
   *   - undefined  → use `defaultScorers` (which may itself be undefined,
   *                  in which case `scorers` is omitted from the call).
   *   - []         → preserved verbatim (means "no scorers", distinct
   *                  from undefined).
   *   - non-empty  → preserved verbatim.
   */
  scorers?: unknown[];
  /**
   * Fallback scorer list used when `scorers` is undefined. Python imports
   * Correctness + RelevanceToQuery from `mlflow.genai.scorers`; in TS the
   * caller wires the equivalent (or leaves this unset to let the
   * `mlflowEvaluate` impl pick its own default).
   */
  defaultScorers?: unknown[];
  /**
   * Optional OBO token. When set, every eval row runs as that user — the
   * agent's tools see the user's UC grants. Translated to an
   * `Authorization: Bearer ...` header on the underlying runner.
   */
  userToken?: string;
  /**
   * Optional workspace host paired with `userToken`. Translated to an
   * `X-Databricks-Host` header.
   */
  workspaceHost?: string;
  /**
   * Optional MLflow experiment name (workspace path or numeric id). When
   * set, `mlflowSetExperiment(experiment)` is called before the eval.
   * Requires `mlflowSetExperiment` to be provided — otherwise a warning
   * is logged and the call is skipped.
   */
  experiment?: string;
  /**
   * Required injectable. The caller wires the mlflow.genai.evaluate
   * equivalent for their environment (JS mlflow client, REST call, fake).
   */
  mlflowEvaluate: MlflowEvaluateFn;
  /**
   * Optional injectable for routing the eval into an MLflow experiment.
   */
  mlflowSetExperiment?: MlflowSetExperimentFn;
  /**
   * Injectable runner. Defaults to the real `runViaSDK` from
   * `./agent/runner.js`. Tests pass a mock to avoid the real FMAPI call.
   */
  runViaSDK?: RunViaSDKFn;
  /**
   * Extra kwargs forwarded verbatim to `mlflowEvaluate`. Mirrors Python's
   * `**mlflow_kwargs`. Use for experiment_id, model_id, run_name, etc.
   */
  mlflowKwargs?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Helpers — exported for direct use + parity with Python's _extract_*
// ---------------------------------------------------------------------------

const COMMON_PROMPT_KEYS = ['request', 'input', 'prompt', 'query', 'question'] as const;

/**
 * Normalise an mlflow.genai eval-row to a list of message dicts.
 *
 * Eval datasets vary: some have `request`, some `input`, some `messages`,
 * some are bare strings. This pulls the user prompt out without forcing
 * the dataset shape.
 *
 * Mirrors Python's `_extract_messages` semantics:
 *   - bare string → [{role: 'user', content: <string>}]
 *   - dict with `messages: [...]` → returned as-is
 *   - dict with any of request/input/prompt/query/question → user msg
 *   - any other dict → fallback to stringified dict
 *   - anything else → fallback to String(inputs)
 */
export function extractMessages(inputs: unknown): EvalMessage[] {
  if (typeof inputs === 'string') {
    return [{ role: 'user', content: inputs }];
  }
  if (inputs !== null && typeof inputs === 'object') {
    const obj = inputs as Record<string, unknown>;
    if (Array.isArray(obj.messages)) {
      return obj.messages as EvalMessage[];
    }
    for (const key of COMMON_PROMPT_KEYS) {
      if (key in obj && obj[key] !== null && obj[key] !== undefined) {
        return [{ role: 'user', content: String(obj[key]) }];
      }
    }
    // Unknown shape — stringify the whole thing so the failure is visible
    // in the trace (matches Python's `str(inputs)` fallback).
    return [{ role: 'user', content: stringifyForFallback(obj) }];
  }
  return [{ role: 'user', content: String(inputs) }];
}

/** Match Python's `str(dict)` shape closely enough that keys appear in output. */
function stringifyForFallback(obj: Record<string, unknown>): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}

/**
 * Pull the final assistant message text out of a response object.
 *
 * Used when the caller has a `ChatAgentResponse`-shape object (from a
 * compiled mlflow ChatAgent or similar). In the in-process eval path
 * here, `runViaSDK` already returns the final assistant string directly,
 * so this helper is exported for external use rather than called inside
 * `evaluate()`.
 *
 * Walks messages from the end and returns the first assistant message's
 * content. Empty string when none is found or no messages present.
 */
export function extractResponseText(response: unknown): string {
  if (response === null || typeof response !== 'object') return '';
  const messages = (response as { messages?: unknown }).messages;
  if (!Array.isArray(messages)) return '';
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg === null || typeof msg !== 'object') continue;
    const role = (msg as { role?: unknown }).role;
    if (role === 'assistant') {
      const content = (msg as { content?: unknown }).content;
      if (content !== null && content !== undefined && content !== '') {
        return String(content);
      }
    }
  }
  return '';
}

// ---------------------------------------------------------------------------
// Agent shape narrowing — duck-typed match against run-once.ts
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
// OBO header translation — Python custom_inputs → TS oboHeaders
// ---------------------------------------------------------------------------

function buildOboHeaders(
  userToken: string | undefined,
  workspaceHost: string | undefined,
): Record<string, string> {
  if (!userToken) return {};
  const headers: Record<string, string> = {
    Authorization: `Bearer ${userToken}`,
  };
  if (workspaceHost) {
    headers['X-Databricks-Host'] = workspaceHost;
  }
  return headers;
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Run Mosaic AI Agent Evaluation against an apx-agent.
 *
 * Builds a `predict_fn` that drives each eval row through `runViaSDK`
 * with the agent's tools + the configured LLM endpoint, then delegates
 * to the injected `mlflowEvaluate`.
 *
 * @returns Whatever the injected `mlflowEvaluate` returns.
 *
 * @throws ZodError when `model` is missing/empty.
 * @throws Error wrapping the underlying error when `mlflowSetExperiment`
 *   is provided and throws.
 */
export async function evaluate(opts: EvalOptions): Promise<unknown> {
  EvalOptionsSchema.parse({
    model: opts.model,
    experiment: opts.experiment,
    userToken: opts.userToken,
    workspaceHost: opts.workspaceHost,
  });

  if (typeof opts.mlflowEvaluate !== 'function') {
    throw new Error(
      'evaluate requires an `mlflowEvaluate` injectable. Wire the JS mlflow ' +
        'client / REST call / test fake of your choice.',
    );
  }

  // Experiment routing — call the injected setter if provided, warn otherwise
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
      // No-op with a visible warning — matches the docstring contract.
      // eslint-disable-next-line no-console
      console.warn(
        `evaluate: experiment="${opts.experiment}" was set but no ` +
          `mlflowSetExperiment was provided; skipping experiment routing.`,
      );
    }
  }

  const config = resolveConfig(opts.agent);
  const tools = resolveTools(opts.agent, config);
  const instructions = config?.instructions ?? '';
  const subAgents = config?.subAgents;
  const maxTurns = config?.maxIterations;
  const oboHeaders = buildOboHeaders(opts.userToken, opts.workspaceHost);
  const runner: RunViaSDKFn = opts.runViaSDK ?? defaultRunViaSDK;

  // predict_fn signature mirrors Python's: takes a single eval-row, returns
  // the final assistant text as a string.
  const predict_fn = async (inputs: unknown): Promise<string> => {
    const msgs = extractMessages(inputs);
    const messages = msgs.map((m) => ({
      role: typeof m.role === 'string' ? m.role : 'user',
      content: typeof m.content === 'string' ? m.content : String(m.content ?? ''),
    }));

    return await runner({
      model: opts.model,
      instructions,
      messages,
      tools,
      subAgents,
      maxTurns,
      oboHeaders,
    });
  };

  // Scorer trichotomy:
  //   - opts.scorers === undefined  → fall back to defaultScorers (which
  //                                    may itself be undefined → omit key)
  //   - opts.scorers === []         → preserve verbatim (means "none")
  //   - opts.scorers non-empty      → preserve verbatim
  const resolvedScorers =
    opts.scorers === undefined ? opts.defaultScorers : opts.scorers;

  const args: MlflowEvaluateArgs = {
    data: opts.evalset,
    predict_fn,
    ...(opts.mlflowKwargs ?? {}),
  };
  if (resolvedScorers !== undefined) {
    args.scorers = resolvedScorers;
  }

  return await opts.mlflowEvaluate(args);
}

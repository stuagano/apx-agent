/**
 * run-once — invoke an agent in batch (no HTTP, no Express lifecycle).
 *
 * The third deployment shape, complementing serving endpoints and
 * Apps hosting. Designed for Databricks Job / cron / scheduled batch
 * contexts where the agent needs to run once with a known prompt and
 * return its final text — no incoming HTTP request, no serving
 * endpoint, just compile + invoke.
 *
 * The Python equivalent (`_run_once.py`) compiles the agent via
 * `compile_to_chat_agent` and calls `predict()`. In TS, the existing
 * `runViaSDK` runner already plays the role of the compiled chat agent:
 * it takes a messages array + model and returns the final assistant
 * text. `runOnce` just builds the single-prompt input and delegates.
 *
 * `runViaSDK` shape (from `./agent/runner.js`):
 *
 *   async function runViaSDK(params: {
 *     model: string;
 *     instructions: string;
 *     messages: Array<{ role: string; content: string }>;
 *     tools: AgentTool[];
 *     subAgents?: string[];
 *     maxTurns?: number;
 *     oboHeaders: Record<string, string>;
 *     ...
 *   }): Promise<string>
 *
 * `runViaSDK` itself already extracts the last assistant message and
 * returns its content as a string — so this module's job is just
 * argument marshaling + model resolution.
 *
 * Tests inject a mock via `opts.runViaSDK` to avoid the real FMAPI call.
 */

import { z } from 'zod';
import { runViaSDK as defaultRunViaSDK } from './agent/runner.js';
import type { AgentConfig, AgentExports } from './agent/plugin.js';
import type { AgentTool } from './agent/tools.js';

// ---------------------------------------------------------------------------
// Minimal facade — narrow enough that tests can inject a mock without
// reaching for the full runner module.
// ---------------------------------------------------------------------------

export interface RunViaSDKParams {
  model: string;
  instructions: string;
  messages: Array<{ role: string; content: string }>;
  tools: AgentTool[];
  subAgents?: string[];
  maxTurns?: number;
  oboHeaders: Record<string, string>;
}

export type RunViaSDKFn = (params: RunViaSDKParams) => Promise<string>;

export type StreamViaSDKFn = (params: RunViaSDKParams) => AsyncIterable<string>;

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const OptionsSchema = z.object({
  prompt: z.string().min(1, 'prompt is required and must be non-empty'),
  model: z.string().min(1).optional(),
});

export interface RunOnceOptions {
  /**
   * The agent to invoke. Accepts the `AgentExports` shape returned by
   * `createAgentPlugin(...).exports()`, a raw `AgentConfig`, or any
   * shape with `.getConfig()`/`.getTools()` accessors. The agent's
   * configured `model` is used as a fallback when `opts.model` is
   * unset.
   */
  agent: AgentExports | AgentConfig | unknown;
  /** User prompt string. Required and non-empty. */
  prompt: string;
  /** LLM endpoint. Defaults to the agent's configured `model`. */
  model?: string;
  /** Injectable runner for tests. Defaults to the real `runViaSDK`. */
  runViaSDK?: RunViaSDKFn;
}

// ---------------------------------------------------------------------------
// Agent shape narrowing
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
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Invoke an agent once with a single user prompt and return its final
 * assistant text.
 *
 * @throws ZodError when `prompt` is missing or empty.
 * @throws Error when no model can be resolved from `opts.model` or
 *   the agent's configured model.
 */
export async function runOnce(opts: RunOnceOptions): Promise<string> {
  // Validate prompt + optional model
  OptionsSchema.parse({ prompt: opts.prompt, model: opts.model });

  const config = resolveConfig(opts.agent);

  const effectiveModel = opts.model ?? config?.model;
  if (!effectiveModel) {
    throw new Error(
      'runOnce requires a model. Pass `model` explicitly or supply an ' +
        'agent with a configured `model`.',
    );
  }

  const tools = resolveTools(opts.agent, config);
  const instructions = config?.instructions ?? '';

  const runner: RunViaSDKFn = opts.runViaSDK ?? defaultRunViaSDK;

  return await runner({
    model: effectiveModel,
    instructions,
    messages: [{ role: 'user', content: opts.prompt }],
    tools,
    subAgents: config?.subAgents,
    maxTurns: config?.maxIterations,
    oboHeaders: {},
  });
}

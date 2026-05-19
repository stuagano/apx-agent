/**
 * agentTool — wrap any agent (local config or remote URL) as a callable tool.
 *
 * Mirrors Google ADK's `AgentTool` pattern as a first-class composition
 * primitive. Lets one agent delegate to another based on the LLM's
 * tool-calling decision, rather than via a fixed workflow position.
 *
 * Workflow agents (SequentialAgent, ParallelAgent, ...) compose along
 * *deterministic* edges. `agentTool` composes along *LLM-driven* edges —
 * the parent's LLM picks when and with what input.
 *
 * Both local in-process and remote agents are supported transparently:
 *
 *     // Local in-process
 *     const specialist = createAgentPlugin({ model, instructions, tools: [...] });
 *     const orchestrator = createAgentPlugin({
 *       tools: [agentTool(specialist, { name: 'specialist', description: '...' })],
 *     });
 *
 *     // Remote (URL string)
 *     const orchestrator = createAgentPlugin({
 *       tools: [agentTool('https://billing.example.databricksapps.com', {
 *         name: 'billing', description: 'Answer billing questions',
 *       })],
 *     });
 */

import { z } from 'zod';
import type { AgentTool } from './tools.js';
import type { AgentConfig, AgentExports } from './plugin.js';
import { callSubAgent, runViaSDK } from './runner.js';
import { getRequestContext } from './request-context.js';
import { agentNameFromUrl } from '../trace.js';

export interface AgentToolOptions {
  /** Tool name the parent LLM sees. Required to be descriptive — LLMs pick tools by name. */
  name?: string;
  /** Tool description. Strongly recommend providing this; the default is generic. */
  description?: string;
}

type AgentTarget = AgentExports | AgentConfig | string;

function isUrl(target: AgentTarget): target is string {
  return typeof target === 'string';
}

function isExports(target: AgentTarget): target is AgentExports {
  return (
    typeof target === 'object' &&
    target !== null &&
    typeof (target as AgentExports).getConfig === 'function'
  );
}

function resolveConfig(target: AgentTarget): AgentConfig | null {
  if (isUrl(target)) return null;
  if (isExports(target)) return target.getConfig();
  return target as AgentConfig;
}

function inferName(target: AgentTarget): string {
  if (isUrl(target)) return agentNameFromUrl(target);
  const cfg = resolveConfig(target);
  return cfg?.name ?? 'agent';
}

/**
 * Wrap an agent as a tool callable from another agent's LLM loop.
 *
 * The returned `AgentTool` is suitable for the `tools: [...]` field of any
 * `AgentConfig`. When the parent LLM picks this tool, the framework invokes
 * the wrapped agent (in-process via `runViaSDK` for local agents, over HTTP
 * via `callSubAgent` for remote URLs) and returns the result.
 */
export function agentTool(target: AgentTarget, options: AgentToolOptions = {}): AgentTool {
  const name = options.name ?? inferName(target);
  const description =
    options.description ??
    `Delegate the user's request to the ${name} agent. Pass the relevant question or instruction as "message".`;

  return {
    name,
    description,
    parameters: z.object({
      message: z.string().describe('The question or instruction to send to the agent'),
    }),
    handler: async (args: unknown): Promise<string> => {
      const { message } = args as { message: string };
      const ctx = getRequestContext();
      const oboHeaders = ctx?.oboHeaders ?? {};

      if (isUrl(target)) {
        return await callSubAgent(target, message, oboHeaders);
      }

      const config = resolveConfig(target)!;
      const tools = isExports(target) ? target.getTools() : (config.tools ?? []);
      return await runViaSDK({
        model: config.model,
        instructions: config.instructions ?? '',
        messages: [{ role: 'user', content: message }],
        tools,
        subAgents: config.subAgents,
        maxTurns: config.maxIterations,
        oboHeaders,
      });
    },
  };
}

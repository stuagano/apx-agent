/**
 * Runner — bridges appkit-agent tools to OpenAI Agents SDK Runner.run().
 *
 * Converts AgentTool instances into FunctionTool objects the SDK understands,
 * dispatches tool calls through Express for OBO auth preservation, and
 * handles sub-agent calls via direct HTTP POST.
 */

import { Agent, Runner, FunctionTool } from '@openai/agents';
import { setDefaultOpenAIClient, setOpenAIAPI } from '@openai/agents';
import OpenAI from 'openai';
import type { Express, Request } from 'express';
import inject from 'light-my-request';

import type { AgentTool } from './tools.js';
import { toStrictSchema, zodToJsonSchema } from './tools.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RunParams {
  model: string;
  instructions: string;
  messages: Array<{ role: string; content: string }>;
  tools: AgentTool[];
  subAgents?: string[];
  maxTurns?: number;
  app: Express;
  oboHeaders: Record<string, string>;
  apiPrefix?: string;
}

// ---------------------------------------------------------------------------
// Client setup
// ---------------------------------------------------------------------------

let _clientInitialized = false;

/** Configure the OpenAI Agents SDK to use Databricks Model Serving. */
export function initDatabricksClient(): OpenAI {
  const host = process.env.DATABRICKS_HOST;
  const token = process.env.DATABRICKS_TOKEN;

  if (!host) {
    throw new Error('DATABRICKS_HOST env var required');
  }

  const client = new OpenAI({
    baseURL: `${host.replace(/\/$/, '')}/serving-endpoints`,
    apiKey: token || 'no-token', // OBO token comes from request headers at runtime
  });

  if (!_clientInitialized) {
    setDefaultOpenAIClient(client);
    setOpenAIAPI('chat_completions');
    _clientInitialized = true;
  }

  return client;
}

// ---------------------------------------------------------------------------
// Tool adapters
// ---------------------------------------------------------------------------

/**
 * Wrap an AgentTool as an OpenAI Agents SDK FunctionTool.
 *
 * Dispatches through the Express app via light-my-request so that the full
 * middleware chain (body parsing, OBO auth, telemetry) is preserved — the
 * same pattern as Python's ASGI dispatch.
 */
export function toFunctionTool(
  tool: AgentTool,
  app: Express,
  oboHeaders: Record<string, string>,
  apiPrefix: string = '/api/agent',
): FunctionTool {
  return new FunctionTool({
    name: tool.name,
    description: tool.description,
    params_json_schema: toStrictSchema(zodToJsonSchema(tool.parameters)),
    strict_json_schema: true,
    on_invoke_tool: async (_ctx: unknown, argsJson: string): Promise<string> => {
      try {
        const res = await inject(app, {
          method: 'POST',
          url: `${apiPrefix}/tools/${tool.name}`,
          payload: argsJson ? JSON.parse(argsJson) : {},
          headers: oboHeaders,
        });

        if (res.statusCode >= 400) {
          return `Tool error (${res.statusCode}): ${res.body}`;
        }

        const result = res.json();
        return typeof result === 'string' ? result : JSON.stringify(result);
      } catch (e) {
        return `Tool error: ${e instanceof Error ? e.message : String(e)}`;
      }
    },
  });
}

/**
 * Wrap a sub-agent URL as a FunctionTool that calls the remote agent
 * via direct HTTP POST with explicit OBO token forwarding.
 *
 * When databricks-openai ships for TypeScript, this should be upgraded
 * to use model="apps/<name>" for automatic OBO through the gateway.
 */
export function toSubAgentTool(
  name: string,
  description: string,
  url: string,
  oboHeaders: Record<string, string>,
): FunctionTool {
  return new FunctionTool({
    name,
    description,
    params_json_schema: {
      type: 'object',
      properties: {
        message: { type: 'string', description: 'The message to send to the agent' },
      },
      required: ['message'],
      additionalProperties: false,
    },
    strict_json_schema: true,
    on_invoke_tool: async (_ctx: unknown, argsJson: string): Promise<string> => {
      try {
        const args = argsJson ? JSON.parse(argsJson) : {};
        const message = args.message ?? JSON.stringify(args);

        const res = await fetch(`${url.replace(/\/$/, '')}/responses`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...oboHeaders,
          },
          body: JSON.stringify({
            input: [{ role: 'user', content: message }],
          }),
        });

        if (!res.ok) {
          return `Sub-agent error (${res.status}): ${await res.text()}`;
        }

        const data = await res.json() as Record<string, unknown>;

        // Extract from Responses API format
        if (data.output_text && typeof data.output_text === 'string') {
          return data.output_text;
        }

        // Fallback: try InvocationResponse format
        const output = data.output as Array<{ content: Array<{ text: string }> }> | undefined;
        if (output?.[0]?.content?.[0]?.text) {
          return output[0].content[0].text;
        }

        return JSON.stringify(data);
      } catch (e) {
        return `Sub-agent error: ${e instanceof Error ? e.message : String(e)}`;
      }
    },
  });
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/** Run the agent loop and return the final text. */
export async function runViaSDK(params: RunParams): Promise<string> {
  initDatabricksClient();

  const functionTools = params.tools.map((t) =>
    toFunctionTool(t, params.app, params.oboHeaders, params.apiPrefix),
  );

  const subAgentTools = (params.subAgents ?? []).map((url, i) =>
    toSubAgentTool(
      `sub_agent_${i}`,
      `Remote agent at ${url}`,
      url,
      params.oboHeaders,
    ),
  );

  const agent = new Agent({
    name: 'agent',
    model: params.model,
    instructions: params.instructions || 'You are a helpful assistant.',
    tools: [...functionTools, ...subAgentTools],
  });

  const result = await Runner.run(agent, params.messages, {
    maxTurns: params.maxTurns ?? 10,
  });

  if (result.finalOutput != null) {
    return String(result.finalOutput);
  }

  // Fallback: extract from new_items
  for (const item of [...result.newItems].reverse()) {
    if ('text' in item && typeof item.text === 'string') return item.text;
    if ('content' in item) return String(item.content);
  }

  return '';
}

/** Stream the agent loop, yielding text chunks. */
export async function* streamViaSDK(params: RunParams): AsyncGenerator<string> {
  initDatabricksClient();

  const functionTools = params.tools.map((t) =>
    toFunctionTool(t, params.app, params.oboHeaders, params.apiPrefix),
  );

  const subAgentTools = (params.subAgents ?? []).map((url, i) =>
    toSubAgentTool(
      `sub_agent_${i}`,
      `Remote agent at ${url}`,
      url,
      params.oboHeaders,
    ),
  );

  const agent = new Agent({
    name: 'agent',
    model: params.model,
    instructions: params.instructions || 'You are a helpful assistant.',
    tools: [...functionTools, ...subAgentTools],
  });

  const result = Runner.run_streamed(agent, params.messages, {
    maxTurns: params.maxTurns ?? 10,
  });

  for await (const event of result.streamEvents()) {
    // Handle text delta events
    const data = event as Record<string, unknown>;
    if (data.data && typeof data.data === 'object') {
      const d = data.data as Record<string, unknown>;
      if (typeof d.delta === 'string' && d.delta) {
        yield d.delta;
      }
    }
    if (typeof data.delta === 'string' && data.delta) {
      yield data.delta;
    }
  }
}

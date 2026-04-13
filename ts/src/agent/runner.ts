/**
 * Runner — bridges appkit-agent tools to OpenAI Agents JS SDK.
 *
 * Uses `tool()` from @openai/agents to create tools, `run()` to execute
 * the agent loop, all via DatabricksOpenAI (standard OpenAI client with
 * baseURL pointed at Databricks Model Serving).
 */

import { Agent, run, tool, setDefaultOpenAIClient, setOpenAIAPI } from '@openai/agents';
import type { Tool } from '@openai/agents';
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
    apiKey: token || 'no-token',
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
 * Wrap an AgentTool as an OpenAI Agents SDK tool.
 *
 * Dispatches through the Express app via light-my-request so that the full
 * middleware chain (body parsing, OBO auth, telemetry) is preserved.
 */
export function toFunctionTool(
  agentTool: AgentTool,
  app: Express,
  oboHeaders: Record<string, string>,
  apiPrefix: string = '/api/agent',
): Tool {
  const schema = toStrictSchema(zodToJsonSchema(agentTool.parameters));

  return tool({
    name: agentTool.name,
    description: agentTool.description,
    parameters: schema as any,
    execute: async (args: unknown): Promise<string> => {
      try {
        const res = await inject(app, {
          method: 'POST',
          url: `${apiPrefix}/tools/${agentTool.name}`,
          payload: args as Record<string, unknown>,
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
 * Wrap a sub-agent URL as a tool that calls the remote agent
 * via direct HTTP POST with explicit OBO token forwarding.
 */
export function toSubAgentTool(
  name: string,
  description: string,
  url: string,
  oboHeaders: Record<string, string>,
): Tool {
  return tool({
    name,
    description,
    parameters: {
      type: 'object',
      properties: {
        message: { type: 'string', description: 'The message to send to the agent' },
      },
      required: ['message'],
      additionalProperties: false,
    } as any,
    execute: async (args: any): Promise<string> => {
      try {
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

        if (data.output_text && typeof data.output_text === 'string') {
          return data.output_text;
        }

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

  const result = await run(agent, params.messages as any, {
    maxTurns: params.maxTurns ?? 10,
  });

  if (result.finalOutput != null) {
    return String(result.finalOutput);
  }

  for (const item of [...result.newItems].reverse()) {
    if ('text' in item && typeof (item as Record<string, unknown>).text === 'string') {
      return (item as Record<string, unknown>).text as string;
    }
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

  const streamResult = await run(agent, params.messages as any, {
    maxTurns: params.maxTurns ?? 10,
    stream: true,
  });

  for await (const event of streamResult) {
    const data = event as unknown as Record<string, unknown>;
    if (data.type === 'raw_model_stream_event') {
      const inner = data.data as Record<string, unknown> | undefined;
      if (inner?.delta && typeof inner.delta === 'string') {
        yield inner.delta;
      }
    }
  }
}

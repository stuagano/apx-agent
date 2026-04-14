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

/**
 * Sanitize messages for Databricks model serving compatibility.
 *
 * The @openai/agents SDK sometimes embeds `role` and `tool_calls` inside
 * content array items (Anthropic-style), which Databricks rejects. This
 * function normalizes messages to the standard chat completions format.
 */
function sanitizeMessages(messages: any[]): any[] {
  return messages.map((msg) => {
    if (!msg || typeof msg !== 'object') return msg;

    // If content is an array, strip invalid fields from each item
    if (Array.isArray(msg.content)) {
      const cleaned = msg.content.map((item: any) => {
        if (typeof item !== 'object' || !item) return item;
        const { role: _r, tool_calls: _tc, ...rest } = item;
        return rest;
      }).filter((item: any) => {
        // Keep items that have text or actual content
        if (typeof item === 'string') return true;
        if (item.type === 'text' && item.text) return true;
        return Object.keys(item).length > 0;
      });
      return { ...msg, content: cleaned.length > 0 ? cleaned : msg.content };
    }

    return msg;
  });
}

/**
 * Configure the OpenAI Agents SDK to use Databricks Model Serving.
 *
 * On Databricks Apps the token comes per-request via X-Forwarded-Access-Token
 * or the Authorization bearer header. Pass oboToken to create a per-request
 * client; falls back to DATABRICKS_TOKEN env var.
 *
 * Note: Databricks Apps injects DATABRICKS_HOST without the https:// scheme,
 * so we normalize it here.
 */
export function initDatabricksClient(oboToken?: string): OpenAI {
  const host = process.env.DATABRICKS_HOST;
  const token = oboToken || process.env.DATABRICKS_TOKEN;

  if (!host) {
    throw new Error('DATABRICKS_HOST env var required');
  }

  // Databricks Apps strips the scheme — ensure https://
  const normalizedHost = host.startsWith('http') ? host : `https://${host}`;

  const client = new OpenAI({
    baseURL: `${normalizedHost.replace(/\/$/, '')}/serving-endpoints`,
    apiKey: token || 'no-token',
    // Intercept requests to sanitize message format for Databricks compatibility.
    // The @openai/agents SDK embeds role/tool_calls inside content arrays, which
    // Databricks model serving rejects with "Extra inputs are not permitted".
    fetch: async (url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body && typeof init.body === 'string') {
        try {
          const body = JSON.parse(init.body);
          if (Array.isArray(body.messages)) {
            body.messages = sanitizeMessages(body.messages);
            init = { ...init, body: JSON.stringify(body) };
          }
        } catch { /* not JSON, pass through */ }
      }
      return globalThis.fetch(url, init);
    },
  });

  // Update the default client — on Databricks Apps this refreshes per-request
  // with the OBO token from the incoming request headers
  setDefaultOpenAIClient(client as any);
  if (!_clientInitialized) {
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
 * Calls the tool handler directly (no Express loopback) to avoid
 * compatibility issues between light-my-request and Express 5.
 */
export function toFunctionTool(
  agentTool: AgentTool,
  _app: Express,
  _oboHeaders: Record<string, string>,
  _apiPrefix: string = '/api/agent',
): Tool {
  const schema = toStrictSchema(zodToJsonSchema(agentTool.parameters));

  return tool({
    name: agentTool.name,
    description: agentTool.description,
    parameters: schema as any,
    execute: async (args: unknown): Promise<string> => {
      try {
        const result = await agentTool.handler(args);
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
// Helpers
// ---------------------------------------------------------------------------

/** Extract the best available auth token from OBO headers. */
function extractOboToken(headers: Record<string, string>): string | undefined {
  return (
    headers['x-forwarded-access-token'] ||
    (headers['authorization'] ?? '').replace(/^Bearer\s+/i, '') ||
    undefined
  );
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/** Run the agent loop and return the final text. */
export async function runViaSDK(params: RunParams): Promise<string> {
  initDatabricksClient(extractOboToken(params.oboHeaders));

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
  initDatabricksClient(extractOboToken(params.oboHeaders));

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

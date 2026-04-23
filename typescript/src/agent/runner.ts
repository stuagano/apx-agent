/**
 * Runner — calls Databricks FMAPI (model serving) directly.
 *
 * No @openai/agents SDK, no OpenAI client. Uses native fetch() with the
 * standard chat completions format. This matches the Python SDK's pattern
 * where DatabricksOpenAI wraps the serving API.
 *
 * Auth: DATABRICKS_TOKEN env var, or OBO token from request headers.
 * Host: DATABRICKS_HOST env var (Databricks Apps strips https://, we normalize).
 */

import type { Express } from 'express';
import type { AgentTool } from './tools.js';
import { toStrictSchema, zodToJsonSchema } from './tools.js';
import { runWithContext, getRequestContext } from './request-context.js';
import { addSpan, endSpan, truncate } from '../trace.js';
import { resolveToken as resolveTokenFull } from '../connectors/types.js';

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
  /** @deprecated No longer used — tools are called directly. */
  app?: Express;
  oboHeaders: Record<string, string>;
  /** @deprecated No longer used. */
  apiPrefix?: string;
}

interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

interface ToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

interface ToolDef {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
  };
}

interface ChatResponse {
  choices: Array<{
    message: ChatMessage;
    finish_reason: string;
    delta?: { content?: string };
  }>;
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function getHost(): string {
  const host = process.env.DATABRICKS_HOST;
  if (!host) throw new Error('DATABRICKS_HOST env var required');
  return host.startsWith('http') ? host.replace(/\/$/, '') : `https://${host}`;
}

/**
 * Resolve auth token for FMAPI calls.
 *
 * For FMAPI (model serving), the app should use its own identity — NOT the
 * caller's OBO token, which may be another app's SP that lacks FMAPI access.
 *
 * Priority: DATABRICKS_TOKEN env → M2M OAuth (app's own SP credentials).
 * OBO headers are intentionally skipped here; they are used for data
 * operations (UC, SQL) where the caller's identity matters.
 */
function resolveToken(_oboHeaders: Record<string, string>): string | Promise<string> | undefined {
  try {
    // Pass no OBO headers so the chain falls through to DATABRICKS_TOKEN or M2M OAuth
    return resolveTokenFull();
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// Fetch with AbortController timeout
// ---------------------------------------------------------------------------

function fetchWithTimeout(url: string, opts: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...opts, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------------------
// FMAPI call
// ---------------------------------------------------------------------------

async function chatCompletions(
  model: string,
  messages: ChatMessage[],
  token?: string,
  tools?: ToolDef[],
): Promise<ChatResponse> {
  const host = getHost();

  const body: Record<string, unknown> = { model, messages };
  if (tools && tools.length > 0) {
    body.tools = tools;
    body.tool_choice = 'auto';
  }

  const ctx = getRequestContext();
  const span = ctx?.trace ? addSpan(ctx.trace, { type: 'llm', name: model, input: truncate(messages), metadata: { model, tool_count: tools?.length ?? 0 } }) : null;

  try {
    const res = await fetchWithTimeout(
      `${host}/serving-endpoints/chat/completions`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
      },
      120_000,
    );

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`FMAPI ${res.status}: ${text}`);
    }

    const result = await res.json() as ChatResponse;
    if (span) { span.output = truncate(result); endSpan(span); }
    return result;
  } catch (err) {
    if (span) { span.output = String(err); span.metadata = { ...span.metadata, error: true }; endSpan(span); }
    throw err;
  }
}

async function chatCompletionsStream(
  model: string,
  messages: ChatMessage[],
  token?: string,
  tools?: ToolDef[],
): Promise<Response> {
  const host = getHost();

  const body: Record<string, unknown> = { model, messages, stream: true };
  if (tools && tools.length > 0) {
    body.tools = tools;
    body.tool_choice = 'auto';
  }

  const ctx = getRequestContext();
  const span = ctx?.trace ? addSpan(ctx.trace, { type: 'llm', name: model, input: truncate(messages), metadata: { model, tool_count: tools?.length ?? 0, streaming: true } }) : null;

  try {
    const res = await fetchWithTimeout(
      `${host}/serving-endpoints/chat/completions`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
      },
      180_000,
    );

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`FMAPI ${res.status}: ${text}`);
    }

    if (span) { span.output = '[streaming response]'; endSpan(span); }
    return res;
  } catch (err) {
    if (span) { span.output = String(err); span.metadata = { ...span.metadata, error: true }; endSpan(span); }
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Tool adapters
// ---------------------------------------------------------------------------

function toToolDef(tool: AgentTool): ToolDef {
  const params = toStrictSchema(zodToJsonSchema(tool.parameters));
  return {
    type: 'function',
    function: {
      name: tool.name,
      description: tool.description,
      parameters: params,
    },
  };
}

function subAgentToolDef(name: string, description: string): ToolDef {
  return {
    type: 'function',
    function: {
      name,
      description,
      parameters: {
        type: 'object',
        properties: {
          message: { type: 'string', description: 'The message to send to the agent' },
        },
        required: ['message'],
        additionalProperties: false,
      },
    },
  };
}

async function callSubAgent(
  url: string,
  message: string,
  oboHeaders: Record<string, string>,
): Promise<string> {
  const res = await fetch(`${url.replace(/\/$/, '')}/responses`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...oboHeaders,
    },
    body: JSON.stringify({ input: [{ role: 'user', content: message }] }),
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
}

// ---------------------------------------------------------------------------
// Backward-compat exports (used by plugin.ts)
// ---------------------------------------------------------------------------

/**
 * @deprecated FMAPI runner handles auth internally. This is a no-op kept
 * for backward compatibility with plugin.ts setup().
 */
export function initDatabricksClient(): void {
  // Validate env at startup
  getHost();
}

/**
 * @deprecated Kept for backward compatibility. Tools are called directly now.
 */
export function toFunctionTool(agentTool: AgentTool, ..._rest: any[]): any {
  return { name: agentTool.name, handler: agentTool.handler };
}

/**
 * @deprecated Kept for backward compatibility.
 */
export function toSubAgentTool(name: string, description: string, url: string, oboHeaders: Record<string, string>): any {
  return {
    name,
    execute: async (args: any) => callSubAgent(url, args.message ?? JSON.stringify(args), oboHeaders),
  };
}

// ---------------------------------------------------------------------------
// Runner — agent tool-calling loop via FMAPI
// ---------------------------------------------------------------------------

/** Run the agent loop and return the final text. */
export async function runViaSDK(params: RunParams): Promise<string> {
  const token = await resolveToken(params.oboHeaders);
  const toolMap = new Map(params.tools.map((t) => [t.name, t]));
  const subAgentMap = new Map(
    (params.subAgents ?? []).map((url, i) => [`sub_agent_${i}`, url]),
  );

  // Build tool definitions
  const toolDefs = [
    ...params.tools.map(toToolDef),
    ...(params.subAgents ?? []).map((url, i) =>
      subAgentToolDef(`sub_agent_${i}`, `Remote agent at ${url}`),
    ),
  ];

  const messages: ChatMessage[] = [
    { role: 'system', content: params.instructions || 'You are a helpful assistant.' },
    ...params.messages.map((m) => ({
      role: m.role as ChatMessage['role'],
      content: m.content,
    })),
  ];

  const maxTurns = params.maxTurns ?? 10;

  for (let turn = 0; turn < maxTurns; turn++) {
    const response = await chatCompletions(
      params.model,
      messages,
      token,
      toolDefs.length > 0 ? toolDefs : undefined,
    );

    const choice = response.choices?.[0];
    if (!choice) return '';

    const assistantMsg = choice.message;
    messages.push(assistantMsg);

    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
      return assistantMsg.content ?? '';
    }

    // Execute tool calls
    for (const tc of assistantMsg.tool_calls) {
      let result: string;
      const tool = toolMap.get(tc.function.name);
      const subAgentUrl = subAgentMap.get(tc.function.name);

      const ctx = getRequestContext();
      const toolSpan = ctx?.trace ? addSpan(ctx.trace, { type: 'tool', name: tc.function.name, input: truncate(tc.function.arguments) }) : null;

      if (tool) {
        try {
          const args = JSON.parse(tc.function.arguments);
          const output = await runWithContext({ oboHeaders: params.oboHeaders, trace: ctx?.trace }, () => tool.handler(args));
          result = typeof output === 'string' ? output : JSON.stringify(output);
        } catch (e) {
          result = `Tool error: ${e instanceof Error ? e.message : String(e)}`;
        }
      } else if (subAgentUrl) {
        try {
          const args = JSON.parse(tc.function.arguments);
          result = await callSubAgent(subAgentUrl, args.message ?? JSON.stringify(args), params.oboHeaders);
        } catch (e) {
          result = `Sub-agent error: ${e instanceof Error ? e.message : String(e)}`;
        }
      } else {
        result = `Tool not found: ${tc.function.name}`;
      }

      if (toolSpan) { toolSpan.output = truncate(result); endSpan(toolSpan); }
      messages.push({ role: 'tool', content: result, tool_call_id: tc.id });
    }
  }

  const last = [...messages].reverse().find((m) => m.role === 'assistant');
  return last?.content ?? '[Max tool-calling turns exceeded]';
}

/** Stream the agent loop, yielding text chunks. */
export async function* streamViaSDK(params: RunParams): AsyncGenerator<string> {
  const token = await resolveToken(params.oboHeaders);
  const toolMap = new Map(params.tools.map((t) => [t.name, t]));
  const subAgentMap = new Map(
    (params.subAgents ?? []).map((url, i) => [`sub_agent_${i}`, url]),
  );

  const toolDefs = [
    ...params.tools.map(toToolDef),
    ...(params.subAgents ?? []).map((url, i) =>
      subAgentToolDef(`sub_agent_${i}`, `Remote agent at ${url}`),
    ),
  ];

  const messages: ChatMessage[] = [
    { role: 'system', content: params.instructions || 'You are a helpful assistant.' },
    ...params.messages.map((m) => ({
      role: m.role as ChatMessage['role'],
      content: m.content,
    })),
  ];

  const maxTurns = params.maxTurns ?? 10;

  for (let turn = 0; turn < maxTurns; turn++) {
    // Use non-streaming for tool-calling turns, stream only the final turn
    const response = await chatCompletions(
      params.model,
      messages,
      token,
      toolDefs.length > 0 ? toolDefs : undefined,
    );

    const choice = response.choices?.[0];
    if (!choice) return;

    const assistantMsg = choice.message;
    messages.push(assistantMsg);

    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
      // Final response — yield the text
      if (assistantMsg.content) {
        yield assistantMsg.content;
      }
      return;
    }

    // Execute tool calls (same as runViaSDK)
    for (const tc of assistantMsg.tool_calls) {
      let result: string;
      const tool = toolMap.get(tc.function.name);
      const subAgentUrl = subAgentMap.get(tc.function.name);

      const ctx = getRequestContext();
      const toolSpan = ctx?.trace ? addSpan(ctx.trace, { type: 'tool', name: tc.function.name, input: truncate(tc.function.arguments) }) : null;

      if (tool) {
        try {
          const args = JSON.parse(tc.function.arguments);
          const output = await runWithContext({ oboHeaders: params.oboHeaders, trace: ctx?.trace }, () => tool.handler(args));
          result = typeof output === 'string' ? output : JSON.stringify(output);
        } catch (e) {
          result = `Tool error: ${e instanceof Error ? e.message : String(e)}`;
        }
      } else if (subAgentUrl) {
        try {
          const args = JSON.parse(tc.function.arguments);
          result = await callSubAgent(subAgentUrl, args.message ?? JSON.stringify(args), params.oboHeaders);
        } catch (e) {
          result = `Sub-agent error: ${e instanceof Error ? e.message : String(e)}`;
        }
      } else {
        result = `Tool not found: ${tc.function.name}`;
      }

      if (toolSpan) { toolSpan.output = truncate(result); endSpan(toolSpan); }
      messages.push({ role: 'tool', content: result, tool_call_id: tc.id });
    }
  }

  yield '[Max tool-calling turns exceeded]';
}

/**
 * FMAPI agent runner — calls Databricks model serving directly.
 *
 * No @openai/agents SDK, no OpenAI client.
 * Uses native chat completions format via fetch().
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

export interface ToolCall {
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
  }>;
}

export interface AgentTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  handler: (args: any) => Promise<unknown>;
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function getConfig() {
  const rawHost = process.env.DATABRICKS_HOST?.replace(/\/$/, '');
  if (!rawHost) throw new Error('DATABRICKS_HOST env var required');
  const host = rawHost.startsWith('http') ? rawHost : `https://${rawHost}`;
  const token = process.env.DATABRICKS_TOKEN;
  return { host, token };
}

// ---------------------------------------------------------------------------
// FMAPI call
// ---------------------------------------------------------------------------

async function chatCompletions(
  model: string,
  messages: ChatMessage[],
  tools?: ToolDef[],
): Promise<ChatResponse> {
  const { host, token } = getConfig();

  const body: Record<string, unknown> = { model, messages };
  if (tools && tools.length > 0) {
    body.tools = tools;
    body.tool_choice = 'auto';
  }

  const res = await fetch(`${host}/serving-endpoints/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`FMAPI ${res.status}: ${text}`);
  }

  return res.json() as Promise<ChatResponse>;
}

// ---------------------------------------------------------------------------
// Tool schema
// ---------------------------------------------------------------------------

function toToolDef(tool: AgentTool): ToolDef {
  const params = { ...tool.parameters };
  delete params['$schema'];
  return {
    type: 'function',
    function: {
      name: tool.name,
      description: tool.description,
      parameters: params,
    },
  };
}

// ---------------------------------------------------------------------------
// Agent loop
// ---------------------------------------------------------------------------

export interface RunOptions {
  model: string;
  instructions: string;
  messages: ChatMessage[];
  tools: AgentTool[];
  maxTurns?: number;
}

/**
 * Run the agent tool-calling loop.
 *
 * 1. Send messages + tool defs to FMAPI
 * 2. If response has tool_calls, execute them
 * 3. Repeat until model responds with text or maxTurns
 */
export async function runAgent(opts: RunOptions): Promise<string> {
  const { model, instructions, tools, maxTurns = 10 } = opts;
  const toolMap = new Map(tools.map((t) => [t.name, t]));
  const toolDefs = tools.map(toToolDef);

  const messages: ChatMessage[] = [
    { role: 'system', content: instructions },
    ...opts.messages.filter((m) => m.role !== 'system'),
  ];

  for (let turn = 0; turn < maxTurns; turn++) {
    const response = await chatCompletions(
      model,
      messages,
      toolDefs.length > 0 ? toolDefs : undefined,
    );

    const choice = response.choices?.[0];
    if (!choice) return '';

    const assistantMsg = choice.message;
    messages.push(assistantMsg);

    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
      return assistantMsg.content ?? '';
    }

    for (const tc of assistantMsg.tool_calls) {
      const tool = toolMap.get(tc.function.name);
      let result: string;

      if (!tool) {
        result = `Tool not found: ${tc.function.name}`;
      } else {
        try {
          const args = JSON.parse(tc.function.arguments);
          const output = await tool.handler(args);
          result = typeof output === 'string' ? output : JSON.stringify(output);
        } catch (e) {
          result = `Tool error: ${e instanceof Error ? e.message : String(e)}`;
        }
      }

      messages.push({ role: 'tool', content: result, tool_call_id: tc.id });
    }
  }

  const last = [...messages].reverse().find((m) => m.role === 'assistant');
  return last?.content ?? '[Max tool-calling turns exceeded]';
}

/**
 * MCP client — consume remote MCP servers as AgentTools.
 *
 * Connects to external MCP endpoints via StreamableHTTP, discovers their
 * tool manifests, and wraps each tool as an AgentTool so it can be passed
 * directly into the agent plugin's tool list.
 *
 * Supports Databricks managed MCP URLs:
 *   /api/2.0/mcp/genie/{space_id}          — Genie Space
 *   /api/2.0/mcp/functions/{catalog}/{schema} — UC Functions
 *
 * Usage:
 *   const genieTools = await discoverMcpTools(
 *     'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
 *     { token: process.env.DATABRICKS_TOKEN! },
 *   );
 *
 *   const allTools = await createMcpToolProvider([
 *     'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
 *     'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
 *   ]);
 */

import { z } from 'zod';
import type { AgentTool } from './tools.js';
import { getRequestContext } from './request-context.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface McpAuthOptions {
  /** Bearer token for Authorization header. */
  token: string;
}

// ---------------------------------------------------------------------------
// JSON Schema → Zod
// ---------------------------------------------------------------------------

/**
 * Convert a JSON Schema object (as returned by MCP tools/list) into a Zod schema.
 *
 * MCP inputSchema is always `{ type: 'object', properties: {...}, required: [...] }`.
 * We convert it structurally so that the AgentTool's parameter validation works,
 * and so downstream consumers (OpenAI function calling, MCP server re-export) get
 * the correct shape.
 */
function jsonSchemaToZod(schema: Record<string, unknown>): z.ZodType {
  const type = schema.type as string | undefined;

  if (type === 'object') {
    const properties = (schema.properties ?? {}) as Record<string, Record<string, unknown>>;
    const required = (schema.required ?? []) as string[];

    const shape: Record<string, z.ZodType> = {};
    for (const [key, propSchema] of Object.entries(properties)) {
      const zodProp = jsonSchemaToZod(propSchema);
      shape[key] = required.includes(key) ? zodProp : zodProp.optional();
    }

    // Preserve the full JSON schema as metadata on the Zod object for callers
    // that need the raw schema (e.g. OpenAI function calling via zodToJsonSchema).
    // z.object() is the correct base type; additionalProperties handling is done
    // downstream by toStrictSchema().
    return z.object(shape);
  }

  if (type === 'array') {
    const items = (schema.items ?? {}) as Record<string, unknown>;
    return z.array(jsonSchemaToZod(items));
  }

  if (type === 'string') {
    let s = z.string();
    if (schema.description) s = s.describe(schema.description as string);
    if (schema.enum) {
      const values = schema.enum as [string, ...string[]];
      return z.enum(values);
    }
    return s;
  }

  if (type === 'number' || type === 'integer') {
    let n = z.number();
    if (type === 'integer') n = n.int();
    return n;
  }

  if (type === 'boolean') {
    return z.boolean();
  }

  if (Array.isArray(type)) {
    // Union of types — use z.union or z.any for complex cases
    if (type.includes('null') && type.length === 2) {
      const nonNull = type.find((t) => t !== 'null')!;
      return jsonSchemaToZod({ ...schema, type: nonNull }).nullable();
    }
    return z.unknown();
  }

  // Fallback for anyOf / oneOf / allOf / unknown shapes
  return z.unknown();
}

// ---------------------------------------------------------------------------
// MCP tool result → string
// ---------------------------------------------------------------------------

type McpToolContent = {
  type?: string;
  text?: string;
  data?: string;
  mimeType?: string;
};

function extractToolResultText(result: unknown): string {
  if (typeof result === 'string') return result;

  const r = result as { content?: McpToolContent[]; isError?: boolean; toolResult?: unknown };

  if (r.content && Array.isArray(r.content)) {
    return r.content
      .filter((c) => c.type === 'text' && c.text)
      .map((c) => c.text!)
      .join('\n') || JSON.stringify(result);
  }

  if (r.toolResult !== undefined) {
    return typeof r.toolResult === 'string' ? r.toolResult : JSON.stringify(r.toolResult);
  }

  return JSON.stringify(result);
}

// ---------------------------------------------------------------------------
// discoverMcpTools
// ---------------------------------------------------------------------------

/**
 * Connect to a remote MCP endpoint, call tools/list, and return an AgentTool
 * for each discovered tool.
 *
 * Each returned AgentTool's handler:
 * 1. Opens a fresh MCP client connection (stateless — matches Databricks managed MCP behavior)
 * 2. Calls the tool via the client
 * 3. Closes the connection
 *
 * @param url  Full URL of the MCP endpoint (e.g. https://host/api/2.0/mcp/genie/abc)
 * @param auth Optional bearer token for authenticated endpoints
 */
export async function discoverMcpTools(url: string, auth?: McpAuthOptions): Promise<AgentTool[]> {
  const { Client } = await import('@modelcontextprotocol/sdk/client/index.js');
  const { StreamableHTTPClientTransport } = await import(
    '@modelcontextprotocol/sdk/client/streamableHttp.js'
  );

  // Build request init for discovery — fall back to DATABRICKS_TOKEN env var
  const discoveryToken = auth?.token ?? process.env.DATABRICKS_TOKEN;
  const requestInit: RequestInit = discoveryToken
    ? { headers: { Authorization: `Bearer ${discoveryToken}` } }
    : {};

  // Connect to list tools
  const discoverClient = new Client(
    { name: 'appkit-agent-discovery', version: '1.0.0' },
    { capabilities: {} },
  );

  const discoverTransport = new StreamableHTTPClientTransport(new URL(url), { requestInit });

  try {
    await discoverClient.connect(discoverTransport);
  } catch (err) {
    throw new Error(
      `Failed to connect to MCP server at ${url}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  let mcpTools: Array<{
    name: string;
    description?: string;
    inputSchema: Record<string, unknown>;
  }>;

  try {
    const result = await discoverClient.listTools();
    mcpTools = result.tools as typeof mcpTools;
  } finally {
    await discoverClient.close();
  }

  // Convert each MCP tool to an AgentTool
  return mcpTools.map((mcpTool) => {
    const parameters = jsonSchemaToZod(
      (mcpTool.inputSchema ?? { type: 'object', properties: {} }) as Record<string, unknown>,
    );

    const agentTool: AgentTool = {
      name: mcpTool.name,
      description: mcpTool.description ?? mcpTool.name,
      parameters,
      handler: async (args: unknown): Promise<unknown> => {
        // Fresh client per call — stateless, matches Databricks managed MCP pattern
        const callClient = new Client(
          { name: 'appkit-agent', version: '1.0.0' },
          { capabilities: {} },
        );

        // Resolve OBO token at call time: request context → static auth → env var
        const ctx = getRequestContext();
        const oboToken = ctx
          ? (ctx.oboHeaders['x-forwarded-access-token'] ||
             (ctx.oboHeaders['authorization'] ?? '').replace(/^Bearer\s+/i, ''))
          : undefined;
        const callToken = oboToken || auth?.token || process.env.DATABRICKS_TOKEN;
        const callRequestInit: RequestInit = callToken
          ? { headers: { Authorization: `Bearer ${callToken}` } }
          : {};

        const callTransport = new StreamableHTTPClientTransport(new URL(url), { requestInit: callRequestInit });

        try {
          await callClient.connect(callTransport);
          const result = await callClient.callTool({
            name: mcpTool.name,
            arguments: (args ?? {}) as Record<string, unknown>,
          });
          return extractToolResultText(result);
        } finally {
          await callClient.close();
        }
      },
    };

    return agentTool;
  });
}

// ---------------------------------------------------------------------------
// createMcpToolProvider
// ---------------------------------------------------------------------------

/**
 * Discover tools from multiple MCP server URLs and return a combined AgentTool[].
 *
 * Connects to all servers in parallel. If a single server fails discovery,
 * a warning is emitted and discovery continues with the remaining servers.
 *
 * @param urls  Array of MCP server URLs
 * @param auth  Optional shared bearer token (applies to all URLs)
 */
export async function createMcpToolProvider(
  urls: string[],
  auth?: McpAuthOptions,
): Promise<AgentTool[]> {
  const results = await Promise.allSettled(urls.map((url) => discoverMcpTools(url, auth)));

  const tools: AgentTool[] = [];

  for (let i = 0; i < results.length; i++) {
    const result = results[i];
    if (result.status === 'fulfilled') {
      tools.push(...result.value);
    } else {
      // Emit warning without crashing — other servers may still work
      console.warn(`[mcp-client] Failed to discover tools from ${urls[i]}:`, result.reason);
    }
  }

  return tools;
}

// ---------------------------------------------------------------------------
// Databricks managed MCP URL helpers
// ---------------------------------------------------------------------------

/**
 * Build the MCP URL for a Databricks Genie Space.
 *
 * @param host     Databricks workspace host (e.g. https://my-workspace.databricks.com)
 * @param spaceId  Genie Space ID
 */
export function genieSpaceMcpUrl(host: string, spaceId: string): string {
  return `${host.replace(/\/$/, '')}/api/2.0/mcp/genie/${spaceId}`;
}

/**
 * Build the MCP URL for Databricks UC Functions in a catalog/schema.
 *
 * @param host    Databricks workspace host
 * @param catalog Unity Catalog catalog name
 * @param schema  Schema name within the catalog
 */
export function ucFunctionsMcpUrl(host: string, catalog: string, schema: string): string {
  return `${host.replace(/\/$/, '')}/api/2.0/mcp/functions/${catalog}/${schema}`;
}

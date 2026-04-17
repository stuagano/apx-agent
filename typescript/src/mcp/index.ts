/**
 * MCP server plugin for Databricks AppKit.
 *
 * Exposes the agent's tools as an MCP server so Supervisor Agent,
 * Claude Desktop, Cursor, and Genie Code can connect.
 *
 * Uses @modelcontextprotocol/sdk with StreamableHTTPServerTransport
 * in stateless mode (fresh server per request).
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import type { Request, Response } from 'express';
import type { AgentExports } from '../agent/index.js';
import { zodToJsonSchema } from '../agent/tools.js';
import { runWithContext } from '../agent/request-context.js';
import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface McpConfig {
  /** Path to mount the MCP endpoint. Defaults to '/mcp'. */
  path?: string;
}

export interface McpAuthContext {
  authorization: string;
  oboToken: string;
}

// ---------------------------------------------------------------------------
// Zod shape extraction
// ---------------------------------------------------------------------------

/**
 * Extract the raw Zod shape from a ZodType so it can be passed as
 * `inputSchema` to McpServer.registerTool().
 *
 * The MCP SDK expects a `ZodRawShapeCompat` — a `Record<string, ZodType>` —
 * which is the shape argument of `z.object({...})`.
 *
 * Returns undefined for non-object schemas (primitive, array, etc.)
 * so the tool is still registered but without parameter definitions.
 */
function extractZodShape(schema: z.ZodType): Record<string, z.ZodType> | undefined {
  // Zod v4: shape is in `._zod.def.shape` or directly `.shape`
  const v4Internal = schema as { _zod?: { def?: { shape?: Record<string, z.ZodType> | (() => Record<string, z.ZodType>) } } };
  if (v4Internal._zod?.def?.shape) {
    const shape = v4Internal._zod.def.shape;
    return typeof shape === 'function' ? shape() : shape;
  }

  // Zod v3: shape is in `._def.shape` or `.shape`
  const v3Internal = schema as {
    _def?: { shape?: Record<string, z.ZodType> | (() => Record<string, z.ZodType>); typeName?: string };
    shape?: Record<string, z.ZodType> | (() => Record<string, z.ZodType>);
  };

  if (v3Internal._def?.typeName === 'ZodObject') {
    const shape = v3Internal._def?.shape ?? v3Internal.shape;
    if (shape) {
      return typeof shape === 'function' ? shape() : shape;
    }
  }

  if (v3Internal.shape) {
    const shape = v3Internal.shape;
    return typeof shape === 'function' ? shape() : shape;
  }

  return undefined;
}

// ---------------------------------------------------------------------------
// Auth context
// ---------------------------------------------------------------------------

export const mcpAuthStore = new AsyncLocalStorage<McpAuthContext>();

export function getMcpAuth(): McpAuthContext | undefined {
  return mcpAuthStore.getStore();
}

// ---------------------------------------------------------------------------
// Plugin factory
// ---------------------------------------------------------------------------

export function createMcpPlugin(config: McpConfig, agentExports: () => AgentExports | null) {
  const mcpPath = config.path ?? '/mcp';

  /** Create a fresh MCP server with tools registered. */
  async function createServer() {
    const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp.js');

    const server = new McpServer(
      { name: 'appkit-agent', version: '1.0.0' },
      { capabilities: { tools: {} } },
    );

    const exports = agentExports();
    if (exports) {
      for (const t of exports.getTools()) {
        // Extract the Zod shape from the tool's parameters schema so MCP
        // clients receive a full inputSchema.  z.object() stores its shape
        // in `.shape` (Zod v3) or `._zod.def.shape` (Zod v4).
        const zodShape = extractZodShape(t.parameters);

        // Use registerTool (preferred API) with inputSchema so MCP
        // clients see parameter definitions.
        server.registerTool(
          t.name,
          {
            description: t.description,
            ...(zodShape ? { inputSchema: zodShape } : {}),
          },
          async (args: unknown) => {
            try {
              // Resolve OBO headers from the mcpAuthStore context set in the route handler.
              // This bridges the MCP auth context into the shared request context so
              // resolveToken() works identically whether a tool is called via the agent
              // loop, HTTP routes, or MCP.
              const mcpAuth = mcpAuthStore.getStore();
              const oboHeaders: Record<string, string> = {};
              if (mcpAuth?.oboToken) oboHeaders['x-forwarded-access-token'] = mcpAuth.oboToken;
              if (mcpAuth?.authorization) oboHeaders['authorization'] = mcpAuth.authorization;

              const result = await runWithContext({ oboHeaders }, () => t.handler(args));
              return {
                content: [{
                  type: 'text' as const,
                  text: typeof result === 'string' ? result : JSON.stringify(result),
                }],
              };
            } catch (e) {
              return {
                content: [{
                  type: 'text' as const,
                  text: `Tool error: ${e instanceof Error ? e.message : String(e)}`,
                }],
                isError: true,
              };
            }
          },
        );
      }
    }

    return server;
  }

  return {
    name: 'mcp' as const,
    displayName: 'MCP Server',
    description: 'Model Context Protocol server for agent tool access',

    async setup() {
      // Validate MCP SDK is available
      try {
        await import('@modelcontextprotocol/sdk/server/mcp.js');
      } catch (e) {
        console.warn('MCP SDK not available:', e);
      }
    },

    injectRoutes(router: { all: Function; get: Function; post: Function }) {
      router.all(mcpPath, async (req: Request, res: Response) => {
        const authCtx: McpAuthContext = {
          authorization: (req.headers.authorization as string) ?? '',
          oboToken: (req.headers['x-forwarded-access-token'] as string) ?? '',
        };

        try {
          const { StreamableHTTPServerTransport } = await import(
            '@modelcontextprotocol/sdk/server/streamableHttp.js'
          );

          // Fresh server + transport per request (stateless mode)
          const server = await createServer();
          const transport = new StreamableHTTPServerTransport({
            sessionIdGenerator: undefined,
          });

          await mcpAuthStore.run(authCtx, async () => {
            await server.connect(transport);
            await transport.handleRequest(req, res, req.body);
          });
        } catch (e) {
          if (!res.headersSent) {
            res.status(500).json({
              error: `MCP error: ${e instanceof Error ? e.message : String(e)}`,
            });
          }
        }
      });
    },
  };
}

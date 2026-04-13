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
        // Use the 3-arg overload (no schema) — compatible with Zod v4
        (server as any).tool(
          t.name,
          t.description,
          async (args: unknown) => {
            try {
              const result = await t.handler(args);
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

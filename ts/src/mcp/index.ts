/**
 * MCP server plugin for Databricks AppKit.
 *
 * Exposes the agent's tools as an MCP server so Supervisor Agent,
 * Claude Desktop, Cursor, and Genie Code can connect.
 *
 * Uses @modelcontextprotocol/sdk with StreamableHTTPServerTransport
 * in stateless mode.
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
// Auth context (AsyncLocalStorage — equivalent of Python contextvars)
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

  let mcpServer: unknown = null;
  let initialized = false;

  async function ensureInitialized() {
    if (initialized) return;
    initialized = true;

    try {
      const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp.js');

      mcpServer = new McpServer(
        { name: 'appkit-agent', version: '1.0.0' },
        { capabilities: { tools: {} } },
      );

      // Register tools from agent plugin
      const exports = agentExports();
      if (exports) {
        for (const tool of exports.getTools()) {
          const server = mcpServer as {
            tool: (name: string, description: string, schema: unknown, handler: (args: unknown) => Promise<unknown>) => void;
          };
          server.tool(
            tool.name,
            tool.description,
            tool.parameters,
            async (args: unknown) => {
              try {
                const result = await tool.handler(args);
                return {
                  content: [
                    {
                      type: 'text' as const,
                      text: typeof result === 'string' ? result : JSON.stringify(result),
                    },
                  ],
                };
              } catch (e) {
                return {
                  content: [
                    {
                      type: 'text' as const,
                      text: `Tool error: ${e instanceof Error ? e.message : String(e)}`,
                    },
                  ],
                  isError: true,
                };
              }
            },
          );
        }
      }
    } catch (e) {
      console.warn('Failed to initialize MCP server:', e);
      mcpServer = null;
    }
  }

  return {
    name: 'mcp' as const,
    displayName: 'MCP Server',
    description: 'Model Context Protocol server for agent tool access',

    async setup() {
      await ensureInitialized();
    },

    injectRoutes(router: { all: Function; get: Function; post: Function }) {
      // Streamable HTTP transport
      router.all(mcpPath, async (req: Request, res: Response) => {
        await ensureInitialized();

        if (!mcpServer) {
          res.status(503).json({
            error: 'MCP server not available',
            hint: 'Check that @modelcontextprotocol/sdk is installed',
          });
          return;
        }

        // Capture OBO auth context
        const authCtx: McpAuthContext = {
          authorization: (req.headers.authorization as string) ?? '',
          oboToken: (req.headers['x-forwarded-access-token'] as string) ?? '',
        };

        try {
          const { StreamableHTTPServerTransport } = await import(
            '@modelcontextprotocol/sdk/server/streamableHttp.js'
          );

          // Stateless transport — new for each request
          const transport = new StreamableHTTPServerTransport({
            sessionIdGenerator: undefined, // stateless
          });

          // Run with auth context so tool handlers can access OBO headers
          await mcpAuthStore.run(authCtx, async () => {
            const server = mcpServer as { connect: (t: unknown) => Promise<void> };
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

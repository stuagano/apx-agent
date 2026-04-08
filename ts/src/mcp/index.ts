/**
 * MCP server plugin for Databricks AppKit.
 *
 * Exposes the agent's tools as an MCP server so Supervisor Agent,
 * Claude Desktop, Cursor, and Genie Code can connect.
 *
 * Usage:
 *   import { mcp } from 'appkit-agent';
 *
 *   createApp({
 *     plugins: [
 *       agent({ model: '...', tools: [...] }),
 *       mcp(),
 *     ],
 *   });
 */

import type { IAppRouter } from '@databricks/appkit';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface McpConfig {
  /** Path to mount the MCP endpoint. Defaults to '/mcp'. */
  path?: string;
}

// ---------------------------------------------------------------------------
// MCP plugin factory
// ---------------------------------------------------------------------------

export function mcp(config: McpConfig = {}) {
  const mcpPath = config.path ?? '/mcp';

  return {
    name: 'mcp',
    displayName: 'MCP Server',
    description: 'Model Context Protocol server for agent tool access',

    async setup() {
      // TODO: Initialize MCP server from @modelcontextprotocol/sdk
      // using tool schemas from the agent plugin exports.
    },

    injectRoutes(router: IAppRouter) {
      // Streamable HTTP transport (stateless)
      router.all(mcpPath, async (req, res) => {
        // TODO: Wire up StreamableHTTPSessionManager from @modelcontextprotocol/sdk
        // For now, return a descriptive error so consumers know MCP is planned.
        res.status(501).json({
          error: 'MCP server not yet implemented',
          hint: 'MCP support is planned — tools are available at /api/tools/:name',
        });
      });

      // SSE transport for Claude Desktop / Cursor
      router.get(`${mcpPath}/sse`, async (req, res) => {
        res.status(501).json({
          error: 'MCP SSE transport not yet implemented',
        });
      });
    },
  };
}

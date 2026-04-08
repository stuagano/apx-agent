/**
 * appkit-agent — AppKit plugins for building AI agents on Databricks Apps.
 *
 * @example
 * import { createApp, server, genie } from '@databricks/appkit';
 * import { agent, discovery, mcp, devUI, defineTool } from 'appkit-agent';
 * import { z } from 'zod';
 *
 * const getLineage = defineTool({
 *   name: 'get_table_lineage',
 *   description: 'Get upstream sources for a table',
 *   parameters: z.object({ tableName: z.string() }),
 *   handler: async ({ tableName }) => {
 *     // query Unity Catalog lineage
 *   },
 * });
 *
 * createApp({
 *   plugins: [
 *     server(),
 *     genie(),
 *     agent({
 *       model: 'databricks-claude-sonnet-4-6',
 *       instructions: 'You investigate missing data.',
 *       tools: [getLineage],
 *     }),
 *     discovery({ registry: '$AGENT_HUB_URL' }),
 *     mcp(),
 *     devUI(),
 *   ],
 * });
 */

// Agent plugin — tool registration, agent loop, /invocations
export { agent, defineTool } from './agent/index.js';
export type { AgentConfig, AgentTool } from './agent/index.js';

// Discovery plugin — A2A card, registry auto-registration
export { discovery } from './discovery/index.js';
export type { DiscoveryConfig, AgentCard } from './discovery/index.js';

// MCP plugin — MCP server exposure for Supervisor / Claude Desktop / Cursor
export { mcp } from './mcp/index.js';
export type { McpConfig } from './mcp/index.js';

// Dev UI plugin — chat testing + tool inspector
export { devUI } from './dev/index.js';
export type { DevUIConfig } from './dev/index.js';

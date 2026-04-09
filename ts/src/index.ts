/**
 * appkit-agent — AppKit plugins for building AI agents on Databricks Apps.
 *
 * @example
 * import { createApp, server, genie } from '@databricks/appkit';
 * import { createAgentPlugin, createDiscoveryPlugin, createMcpPlugin, createDevPlugin, defineTool } from 'appkit-agent';
 * import { z } from 'zod';
 *
 * const getLineage = defineTool({
 *   name: 'get_table_lineage',
 *   description: 'Get upstream sources for a table',
 *   parameters: z.object({ tableName: z.string() }),
 *   handler: async ({ tableName }) => { ... },
 * });
 *
 * const agentPlugin = createAgentPlugin({
 *   model: 'databricks-claude-sonnet-4-6',
 *   instructions: 'You investigate missing data.',
 *   tools: [getLineage],
 * });
 *
 * const agentExports = () => agentPlugin.exports();
 *
 * createApp({
 *   plugins: [
 *     server(),
 *     genie(),
 *     agentPlugin,
 *     createDiscoveryPlugin({ registry: '$AGENT_HUB_URL' }, agentExports),
 *     createMcpPlugin({}, agentExports),
 *     createDevPlugin({}, agentExports),
 *   ],
 * });
 */

// Agent plugin — tool registration, agent loop, /responses
export { createAgentPlugin, defineTool, zodToJsonSchema, toStrictSchema, toolsToFunctionSchemas } from './agent/index.js';
export type { AgentConfig, AgentExports, AgentTool, FunctionSchema } from './agent/index.js';
export { runViaSDK, streamViaSDK, initDatabricksClient, toFunctionTool, toSubAgentTool } from './agent/index.js';

// Discovery plugin — A2A card, registry auto-registration
export { createDiscoveryPlugin } from './discovery/index.js';
export type { DiscoveryConfig, AgentCard } from './discovery/index.js';

// MCP plugin — MCP server exposure for Supervisor / Claude Desktop / Cursor
export { createMcpPlugin, mcpAuthStore, getMcpAuth } from './mcp/index.js';
export type { McpConfig, McpAuthContext } from './mcp/index.js';

// Dev UI plugin — chat testing + tool inspector
export { createDevPlugin } from './dev/index.js';
export type { DevUIConfig } from './dev/index.js';

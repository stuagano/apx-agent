/**
 * Agent plugin — tool registration, LLM loop via OpenAI Agents SDK, /responses endpoint.
 */

export { defineTool, zodToJsonSchema, toStrictSchema, toolsToFunctionSchemas } from './tools.js';
export type { AgentTool, FunctionSchema } from './tools.js';

export { createAgentPlugin } from './plugin.js';
export type { AgentConfig, AgentExports } from './plugin.js';

export { runViaSDK, streamViaSDK, initDatabricksClient, toFunctionTool, toSubAgentTool } from './runner.js';

export { discoverMcpTools, createMcpToolProvider, genieSpaceMcpUrl, ucFunctionsMcpUrl } from './mcp-client.js';
export type { McpAuthOptions } from './mcp-client.js';

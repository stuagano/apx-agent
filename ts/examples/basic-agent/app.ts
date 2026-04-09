/**
 * Example: minimal agent on Databricks AppKit.
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   npx tsx app.ts
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
} from '../../src/index.js';

// ---------------------------------------------------------------------------
// Define tools
// ---------------------------------------------------------------------------

const getCurrentTime = defineTool({
  name: 'get_current_time',
  description: 'Get the current date and time.',
  parameters: z.object({}),
  handler: async () => new Date().toISOString(),
});

const calculate = defineTool({
  name: 'calculate',
  description: 'Evaluate a mathematical expression.',
  parameters: z.object({
    expression: z.string().describe('The math expression to evaluate, e.g. "2 + 2"'),
  }),
  handler: async ({ expression }) => {
    // Simple safe eval for demo purposes
    const result = Function(`"use strict"; return (${expression})`)();
    return { expression, result };
  },
});

// ---------------------------------------------------------------------------
// Create plugins
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'You are a helpful assistant. Use your tools when asked.',
  tools: [getCurrentTime, calculate],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Wire up Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

// Initialize plugins
agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin({}, agentExports);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(console.error);

const devPlugin = createDevPlugin({}, agentExports);

// Mount routes
agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`Agent running at http://localhost:${port}`);
  console.log(`  /responses          — agent endpoint (Responses API)`);
  console.log(`  /.well-known/agent.json — A2A discovery card`);
  console.log(`  /mcp                — MCP server`);
  console.log(`  /_apx/agent         — dev chat UI`);
  console.log(`  /_apx/tools         — tool inspector`);
});

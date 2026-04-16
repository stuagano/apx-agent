/**
 * Example: 3-step pipeline agent with state interpolation.
 *
 * Demonstrates:
 * - 3 step agents with output_key
 * - SequentialAgent composing them
 * - State interpolation ({variable} in instructions)
 * - Wired into createAgentPlugin via the workflow option
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   npx tsx app.ts
 */

import express from 'express';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  SequentialAgent,
  AgentState,
} from '@databricks/appkit-agent';
import type { Runnable, Message } from '@databricks/appkit-agent';

// ---------------------------------------------------------------------------
// Step agents — each has an output_key so its result is stored in state
// ---------------------------------------------------------------------------

/**
 * Step 1: Analyze — extracts the topic from the user's message.
 */
const analyzerAgent: Runnable = {
  outputKey: 'analysis',
  async run(messages: Message[]): Promise<string> {
    const userMsg = messages.find((m) => m.role === 'user')?.content ?? '';
    return `Topic identified: "${userMsg}". Key aspects: data quality, completeness, freshness.`;
  },
};

/**
 * Step 2: Plan — creates an action plan based on the analysis.
 * The SequentialAgent passes the previous agent's output in the context.
 */
const plannerAgent: Runnable = {
  outputKey: 'plan',
  async run(messages: Message[]): Promise<string> {
    const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
    const analysis = lastAssistant?.content ?? 'no analysis';
    return [
      `Plan based on: ${analysis}`,
      '1. Check data freshness in Unity Catalog',
      '2. Validate row counts against source',
      '3. Run quality checks on key columns',
    ].join('\n');
  },
};

/**
 * Step 3: Execute — produces a final report.
 * Uses state interpolation to reference earlier steps.
 */
const executorAgent: Runnable = {
  outputKey: 'report',
  async run(messages: Message[], state?: AgentState): Promise<string> {
    const analysis = state?.get<string>('analysis') ?? 'N/A';
    const plan = state?.get<string>('plan') ?? 'N/A';
    return [
      '=== Investigation Report ===',
      '',
      `Analysis: ${analysis}`,
      '',
      `Plan executed: ${plan}`,
      '',
      'Result: All checks passed. Data is fresh and complete.',
    ].join('\n');
  },
};

// ---------------------------------------------------------------------------
// Compose into a pipeline
// ---------------------------------------------------------------------------

const pipeline = new SequentialAgent(
  [analyzerAgent, plannerAgent, executorAgent],
  'You are investigating a data issue. Current state: analysis={analysis}, plan={plan}',
);

// ---------------------------------------------------------------------------
// Wire into createAgentPlugin
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'You are a data investigation pipeline.',
  workflow: pipeline,
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  { name: 'pipeline-agent', description: 'Data investigation pipeline' },
  agentExports,
);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(() => {});

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`Pipeline agent running at http://localhost:${port}`);
  console.log(`  POST /responses — pipeline endpoint`);
  console.log(`  GET  /.well-known/agent.json — A2A card`);
});

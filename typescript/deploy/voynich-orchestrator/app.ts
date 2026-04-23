/**
 * Voynich Orchestrator — evolutionary decipherment agent on Databricks AppKit.
 *
 * Manages a multi-generation population of Voynich decipherment hypotheses,
 * scoring them against statistical, perplexity, semantic, consistency, and
 * adversarial fitness criteria across configurable remote sub-agents.
 *
 * Required environment variables:
 *   DATABRICKS_HOST          Workspace URL (e.g. https://adb-xxx.azuredatabricks.net)
 *   DATABRICKS_TOKEN         PAT or OAuth token
 *   DATABRICKS_WAREHOUSE_ID  SQL warehouse for PopulationStore queries
 *   MUTATION_AGENT_URL       Base URL of the mutation sub-agent
 *   FITNESS_AGENT_URLS       Comma-separated list of fitness sub-agent base URLs
 *   JUDGE_AGENT_URL          (optional) Base URL of the judge sub-agent
 *
 * Optional:
 *   PORT                     HTTP listen port (default 8000)
 *   POPULATION_SIZE          Number of survivors per generation (default 50)
 *   MUTATION_BATCH           Mutations per generation (default 20)
 *   MAX_GENERATIONS          Maximum generations to run (default 500)
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.azuredatabricks.net \
 *   DATABRICKS_TOKEN=your-token \
 *   DATABRICKS_WAREHOUSE_ID=your-warehouse-id \
 *   MUTATION_AGENT_URL=http://localhost:8001 \
 *   FITNESS_AGENT_URLS=http://localhost:8002,http://localhost:8003 \
 *   npx tsx app.ts
 */

import express from 'express';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  PopulationStore,
  EvolutionaryAgent,
  DeltaEngine,
  InMemoryEngine,
} from './appkit-agent/index.mjs';
import {
  VOYNICH_FITNESS_WEIGHTS,
  VOYNICH_PARETO_OBJECTIVES,
  DEFAULT_POPULATION_TABLE,
} from './voynich-config.ts';

// ---------------------------------------------------------------------------
// Population store
// ---------------------------------------------------------------------------

const store = new PopulationStore({
  populationTable:
    process.env.POPULATION_TABLE ?? DEFAULT_POPULATION_TABLE,
});

// ---------------------------------------------------------------------------
// Evolutionary agent
// ---------------------------------------------------------------------------

const mutationAgentUrl = process.env.MUTATION_AGENT_URL;
if (!mutationAgentUrl) {
  throw new Error('MUTATION_AGENT_URL env var is required');
}

const fitnessAgentUrls = (process.env.FITNESS_AGENT_URLS ?? '')
  .split(',')
  .map((u) => u.trim())
  .filter(Boolean);

if (fitnessAgentUrls.length === 0) {
  throw new Error('FITNESS_AGENT_URLS env var is required (comma-separated list of URLs)');
}

// Durable execution: if WORKFLOW_TABLE_PREFIX is set, the evolution run
// persists each generation phase to Delta so it survives app restarts and
// can resume via RUN_ID. Otherwise fall back to the in-memory engine
// (original behavior — state lost on restart).
const engine = process.env.WORKFLOW_TABLE_PREFIX
  ? new DeltaEngine({
      tablePrefix: process.env.WORKFLOW_TABLE_PREFIX,
      warehouseId: process.env.DATABRICKS_WAREHOUSE_ID!,
    })
  : new InMemoryEngine();

const evolutionaryAgent = new EvolutionaryAgent({
  store,
  populationSize: parseInt(process.env.POPULATION_SIZE ?? '50'),
  mutationBatch: parseInt(process.env.MUTATION_BATCH ?? '20'),
  mutationAgent: mutationAgentUrl,
  fitnessAgents: fitnessAgentUrls,
  judgeAgent: process.env.JUDGE_AGENT_URL,
  paretoObjectives: VOYNICH_PARETO_OBJECTIVES,
  fitnessWeights: VOYNICH_FITNESS_WEIGHTS,
  maxGenerations: parseInt(process.env.MAX_GENERATIONS ?? '500'),
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich decipherment orchestrator. ' +
    'Manage and summarise the evolutionary search for a valid decipherment of the Voynich manuscript. ' +
    'Use your tools to inspect generation results, escalate top hypotheses, and pause or resume the loop on request.',
  engine,
  runId: process.env.RUN_ID,
  workflowName: 'voynich-evolution',
});

// ---------------------------------------------------------------------------
// Agent plugin
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: evolutionaryAgent.collectTools().length > 0
    ? 'You are the Voynich decipherment orchestrator. Use your tools to manage the evolutionary loop and answer questions about decipherment progress.'
    : 'You are the Voynich decipherment orchestrator.',
  tools: evolutionaryAgent.collectTools(),
  workflow: evolutionaryAgent,
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-orchestrator',
    description: 'Evolutionary decipherment orchestrator for the Voynich manuscript',
  },
  agentExports,
);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(console.error);

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8000');
const server = app.listen(port, () => {
  console.log(`Voynich Orchestrator running at http://localhost:${port}`);
  console.log(`  POST /responses              — agent endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json — A2A discovery card`);
  console.log(`  /mcp                         — MCP server`);
  console.log(`  /_apx/agent                  — dev chat UI`);
  console.log(`  /_apx/tools                  — tool inspector`);
});
server.timeout = 300_000;         // 5 min max request time (orchestrator calls sub-agents in sequence)
server.keepAliveTimeout = 90_000;  // longer than typical proxy keepalive (60s)

// Mode: 'theory' runs the debate loop, 'evolution' runs the EA loop
const mode = process.env.LOOP_MODE ?? 'theory';

if (mode === 'theory') {
  console.log('[orchestrator] starting theory-driven debate loop');
  import('./theory-loop.ts').then((m) => {
    m.runTheoryLoop(50).then((theories) => {
      console.log(`[orchestrator] theory loop complete: ${theories.length} theories generated`);
    }).catch((err) => {
      console.error('[orchestrator] theory loop crashed:', err);
    });
  });
} else if (process.env.AUTO_START_LOOP !== 'false') {
  console.log('[orchestrator] auto-starting evolutionary loop');
  evolutionaryAgent.startLoop();
}

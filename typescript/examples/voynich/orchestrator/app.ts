/**
 * Voynich Orchestrator — LLM-routed decipherment agent on Databricks AppKit.
 *
 * Uses RouterAgent with LLM-based routing to dispatch user requests to:
 *   1. EA Management  — evolutionary loop control, generation status, escalation
 *   2. Theory Investigation — targeted theory-driven decoding with cross-folio
 *      validation, skeptic challenge, and critic analysis
 *
 * Deterministic conditions catch simple keywords; when the intent is ambiguous,
 * the LLM classifier picks the best route based on the conversation.
 *
 * Required environment variables:
 *   DATABRICKS_HOST          Workspace URL
 *   DATABRICKS_TOKEN         PAT or OAuth token
 *   DATABRICKS_WAREHOUSE_ID  SQL warehouse for PopulationStore queries
 *   MUTATION_AGENT_URL       Base URL of the mutation sub-agent
 *   FITNESS_AGENT_URLS       Comma-separated list of fitness sub-agent base URLs
 *
 * Optional:
 *   JUDGE_AGENT_URL          Base URL of the judge sub-agent
 *   PORT                     HTTP listen port (default 8000)
 *   POPULATION_SIZE          Number of survivors per generation (default 50)
 *   MUTATION_BATCH           Mutations per generation (default 20)
 *   MAX_GENERATIONS          Maximum generations to run (default 500)
 *   WORKFLOW_TABLE_PREFIX     Delta table prefix for durable execution
 *   RUN_ID                   Resume an existing evolutionary run
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
  RouterAgent,
} from '../../../src/index.js';
import {
  VOYNICH_FITNESS_WEIGHTS,
  VOYNICH_PARETO_OBJECTIVES,
  DEFAULT_POPULATION_TABLE,
} from '../voynich-config.js';
import { TheoryInvestigator } from './theory-investigator.js';

// ---------------------------------------------------------------------------
// Population store
// ---------------------------------------------------------------------------

const store = new PopulationStore({
  populationTable:
    process.env.POPULATION_TABLE ?? DEFAULT_POPULATION_TABLE,
});

// ---------------------------------------------------------------------------
// Route 1: Evolutionary Agent (EA loop management)
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
    'You are the Voynich EA manager. ' +
    'Manage and summarise the evolutionary search for a valid decipherment. ' +
    'Use your tools to inspect generation results, escalate top hypotheses, and pause or resume the loop.',
  engine,
  runId: process.env.RUN_ID,
  workflowName: 'voynich-evolution',
});

// ---------------------------------------------------------------------------
// Route 2: Theory Investigator (targeted decoding + validation)
// ---------------------------------------------------------------------------

const theoryInvestigator = new TheoryInvestigator();

// ---------------------------------------------------------------------------
// LLM-routed orchestrator
// ---------------------------------------------------------------------------

const EA_KEYWORDS = ['generation', 'evolut', 'population', 'pareto', 'fitness', 'escalat', 'pause', 'resume', 'converge'];
const THEORY_KEYWORDS = ['theory', 'theor', 'decode', 'decipher', 'cipher', 'symbol map', 'folio', 'propose', 'investigate', 'skeptic', 'cross-folio', 'latin', 'polyalphabetic', 'substitution'];

const router = new RouterAgent({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are routing requests for a Voynich manuscript decipherment system.',
    'Choose between two specialist agents:',
    '',
    '- "ea_management": For managing the evolutionary algorithm loop — checking generation',
    '  status, viewing fitness scores, pausing/resuming the loop, escalating hypotheses,',
    '  or asking about population-level statistics.',
    '',
    '- "theory_investigation": For targeted decipherment work — proposing new decoding',
    '  theories, testing symbol maps against folios, running cross-folio consistency checks,',
    '  challenging theories with the skeptic, or investigating specific cipher types and languages.',
    '',
    'If the user asks a general question about Voynich progress or "what\'s working",',
    'route to ea_management. If they want to try a new approach or test a specific idea,',
    'route to theory_investigation.',
  ].join('\n'),
  routes: [
    {
      name: 'ea_management',
      description: 'Evolutionary algorithm loop management — generation status, fitness scores, population stats, pause/resume, escalation',
      agent: evolutionaryAgent,
      condition: (msgs) => {
        const last = msgs[msgs.length - 1]?.content?.toLowerCase() ?? '';
        return EA_KEYWORDS.some((kw) => last.includes(kw));
      },
    },
    {
      name: 'theory_investigation',
      description: 'Targeted decipherment — propose theories, test symbol maps, cross-folio validation, skeptic challenge, cipher type exploration',
      agent: theoryInvestigator,
      condition: (msgs) => {
        const last = msgs[msgs.length - 1]?.content?.toLowerCase() ?? '';
        return THEORY_KEYWORDS.some((kw) => last.includes(kw));
      },
    },
  ],
  fallback: evolutionaryAgent,
});

// ---------------------------------------------------------------------------
// Agent plugin — exposes tools from both routes
// ---------------------------------------------------------------------------

const allTools = [
  ...evolutionaryAgent.collectTools(),
  ...theoryInvestigator.collectTools(),
];

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich decipherment orchestrator.',
    'You manage both an evolutionary search loop and a targeted theory investigation system.',
    '',
    'You have two categories of tools:',
    '  EA Management: evolution_status, best_hypothesis, generation_summary, pause_evolution, resume_evolution, force_escalate',
    '  Theory Investigation: propose_theory, challenge_theory, run_theory_loop, list_theories',
    '',
    'When users ask about progress, status, generations, or fitness — use the EA tools.',
    'When users want to try decoding theories, test cipher types, or investigate specific folios — use the theory tools.',
    'Always call the relevant tool(s) to answer the question. Never guess — use tools to get real data.',
  ].join('\n'),
  tools: allTools,
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
    description: 'LLM-routed Voynich decipherment orchestrator — evolutionary search + theory investigation',
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
app.listen(port, () => {
  console.log(`Voynich Orchestrator running at http://localhost:${port}`);
  console.log(`  Routing: LLM-based (${router.collectTools().length} tools across 2 routes)`);
  console.log(`  POST /responses              — agent endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json — A2A discovery card`);
  console.log(`  /mcp                         — MCP server`);
  console.log(`  /_apx/agent                  — dev chat UI`);
  console.log(`  /_apx/tools                  — tool inspector`);
});

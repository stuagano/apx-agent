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
// Agent trace dashboard
// ---------------------------------------------------------------------------

import { mountTraceDashboard } from './trace-dashboard.ts';
mountTraceDashboard(app);

// ---------------------------------------------------------------------------
// Results dashboard
// ---------------------------------------------------------------------------

app.get('/_apx/results', async (_req, res) => {
  const host = process.env.DATABRICKS_HOST || '';
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID || '';

  // The dashboard is a self-contained HTML page that fetches data client-side
  // via the Databricks SQL API (using the app's auth token passed as a cookie)
  res.setHeader('Content-Type', 'text/html');
  res.send(`<!DOCTYPE html>
<html>
<head>
  <title>Voynich Theory Results</title>
  <meta http-equiv="refresh" content="30">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 24px; margin-bottom: 8px; color: #fff; }
    .subtitle { color: #888; margin-bottom: 24px; font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 16px; }
    .card .label { font-size: 11px; text-transform: uppercase; color: #888; margin-bottom: 4px; }
    .card .value { font-size: 28px; font-weight: 600; color: #4dd0e1; }
    .card .value.green { color: #4caf50; }
    .card .value.red { color: #ef5350; }
    .card .value.amber { color: #ffb74d; }
    table { width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }
    th { background: #252540; padding: 10px 12px; text-align: left; font-size: 12px; text-transform: uppercase; color: #888; }
    td { padding: 10px 12px; border-top: 1px solid #252540; font-size: 13px; }
    tr:hover { background: #252540; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
    .tag.rejected { background: #3e1a1a; color: #ef5350; }
    .tag.plausible { background: #1a3e1a; color: #4caf50; }
    .tag.weak { background: #3e3a1a; color: #ffb74d; }
    .bar { height: 6px; border-radius: 3px; background: #333; }
    .bar-fill { height: 100%; border-radius: 3px; background: #4dd0e1; }
    .section { margin-bottom: 24px; }
    .section h2 { font-size: 16px; margin-bottom: 12px; color: #ccc; }
    .refreshing { position: fixed; top: 12px; right: 12px; font-size: 11px; color: #555; }
  </style>
</head>
<body>
  <h1>Voynich Manuscript — Theory-Driven Decoding</h1>
  <p class="subtitle">Auto-refreshes every 30 seconds. Theories tested against 38 herbal folio illustrations.</p>
  <span class="refreshing">Last refresh: <span id="ts"></span></span>

  <div class="grid" id="summary"></div>

  <div class="section">
    <h2>By Language</h2>
    <div id="by-lang"></div>
  </div>

  <div class="section">
    <h2>Top Theories</h2>
    <table>
      <thead><tr><th>Folio</th><th>Plant</th><th>Language</th><th>Grounding</th><th>Consistency</th><th>Verdict</th><th>Decoded Text</th></tr></thead>
      <tbody id="theories"></tbody>
    </table>
  </div>

  <script>
    document.getElementById('ts').textContent = new Date().toLocaleTimeString();

    async function query(sql) {
      const res = await fetch('/api/sql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ statement: sql })
      });
      const d = await res.json();
      const cols = (d.manifest?.schema?.columns || []).map(c => c.name);
      return (d.result?.data_array || []).map(row => {
        const obj = {};
        cols.forEach((c, i) => obj[c] = row[i]);
        return obj;
      });
    }

    async function load() {
      // Summary
      const summary = await query(\`
        SELECT COUNT(*) total,
          COUNT(CASE WHEN grounding_score > 0 THEN 1 END) grounded,
          ROUND(MAX(grounding_score),3) best_grd,
          ROUND(MAX(consistency_score),3) best_cons,
          COUNT(CASE WHEN verdict = 'plausible' THEN 1 END) plausible,
          COUNT(CASE WHEN verdict = 'rejected' THEN 1 END) rejected
        FROM serverless_stable_qh44kx_catalog.voynich.theories
      \`);
      if (summary[0]) {
        const s = summary[0];
        document.getElementById('summary').innerHTML = \`
          <div class="card"><div class="label">Total Theories</div><div class="value">\${s.total}</div></div>
          <div class="card"><div class="label">Best Grounding</div><div class="value \${parseFloat(s.best_grd) > 0 ? 'green' : ''}">\${s.best_grd}</div></div>
          <div class="card"><div class="label">Best Consistency</div><div class="value \${parseFloat(s.best_cons) > 0 ? 'green' : 'red'}">\${s.best_cons}</div></div>
          <div class="card"><div class="label">Plausible</div><div class="value green">\${s.plausible}</div></div>
          <div class="card"><div class="label">Rejected</div><div class="value red">\${s.rejected}</div></div>
        \`;
      }

      // By language
      const langs = await query(\`
        SELECT source_language, COUNT(*) n, ROUND(AVG(grounding_score),3) avg_grd, ROUND(MAX(grounding_score),3) best_grd
        FROM serverless_stable_qh44kx_catalog.voynich.theories GROUP BY source_language ORDER BY avg_grd DESC
      \`);
      document.getElementById('by-lang').innerHTML = '<table><thead><tr><th>Language</th><th>Theories</th><th>Avg Grounding</th><th>Best Grounding</th></tr></thead><tbody>' +
        langs.map(l => \`<tr><td>\${l.source_language}</td><td>\${l.n}</td><td>\${l.avg_grd}</td><td>\${l.best_grd}</td></tr>\`).join('') +
        '</tbody></table>';

      // Top theories
      const theories = await query(\`
        SELECT target_folio, target_plant, source_language, ROUND(grounding_score,3) grd,
          ROUND(consistency_score,3) cons, verdict, SUBSTRING(decoded_text, 1, 60) decoded
        FROM serverless_stable_qh44kx_catalog.voynich.theories
        ORDER BY (grounding_score + consistency_score) DESC LIMIT 20
      \`);
      document.getElementById('theories').innerHTML = theories.map(t => \`
        <tr>
          <td>\${t.target_folio}</td>
          <td>\${t.target_plant}</td>
          <td>\${t.source_language}</td>
          <td><div class="bar"><div class="bar-fill" style="width:\${parseFloat(t.grd)*100}%"></div></div> \${t.grd}</td>
          <td><div class="bar"><div class="bar-fill" style="width:\${parseFloat(t.cons)*100}%"></div></div> \${t.cons}</td>
          <td><span class="tag \${t.verdict}">\${t.verdict}</span></td>
          <td style="font-family:monospace;font-size:12px">\${t.decoded}</td>
        </tr>
      \`).join('');
    }

    load();
  </script>
</body>
</html>`);
});

// SQL proxy for the dashboard
app.post('/api/sql', async (req, res) => {
  try {
    const { resolveToken: rt, resolveHost: rh } = await import('./appkit-agent/index.mjs');
    const tk = await rt();
    const h = rh();
    const url = h + '/api/2.0/sql/statements';
    const sqlRes = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tk },
      body: JSON.stringify({
        warehouse_id: process.env.DATABRICKS_WAREHOUSE_ID,
        statement: req.body.statement,
        wait_timeout: '30s',
      }),
    });
    const data = await sqlRes.json();
    res.json(data);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

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

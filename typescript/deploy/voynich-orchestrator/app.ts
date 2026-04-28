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
} from './appkit-agent/index.mjs';
import {
  VOYNICH_FITNESS_WEIGHTS,
  VOYNICH_PARETO_OBJECTIVES,
  DEFAULT_POPULATION_TABLE,
} from './voynich-config.ts';
import { TheoryInvestigator } from './theory-investigator.ts';

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
// Agent trace dashboard
// ---------------------------------------------------------------------------

import { mountTraceDashboard } from './trace-dashboard.ts';
mountTraceDashboard(app);

// ---------------------------------------------------------------------------
// Live activity ring buffer — recent theory results for the dashboard
// ---------------------------------------------------------------------------

interface ActivityEntry {
  time: string;
  round: number;
  batch: number;
  folio: string;
  plant: string;
  lang: string;
  cipher: string;
  grounding: number;
  consistency: number;
  combined: number;
  dictScore: number;
  improvements: number;
  seedOrigin: string;
  decoded: string;
}

const activityLog: ActivityEntry[] = [];
const ACTIVITY_LOG_SIZE = 50;
let currentBurst = 0;
let currentRound = 0;
let activeStrategy = '';
let allTimeBestCombined = 0;

/** Called by the theory loop to push live results to the dashboard. */
export function logActivity(entry: Omit<ActivityEntry, 'time'>) {
  activityLog.push({ ...entry, time: new Date().toISOString() });
  if (activityLog.length > ACTIVITY_LOG_SIZE) activityLog.shift();
  if (entry.combined > allTimeBestCombined) allTimeBestCombined = entry.combined;
  currentRound = entry.round;
  currentBurst = entry.batch;
  activeStrategy = `${entry.lang}|${entry.cipher}|${entry.seedOrigin}`;
}

// Expose activity as JSON endpoint
app.get('/api/activity', (_req, res) => {
  res.json({
    burst: currentBurst,
    round: currentRound,
    activeStrategy,
    allTimeBest: allTimeBestCombined,
    entries: activityLog.slice(-30),
  });
});

// Strategy stats endpoint — reads from Delta
app.get('/api/strategies', async (_req, res) => {
  try {
    const { resolveToken: rt, resolveHost: rh } = await import('./appkit-agent/index.mjs');
    const tk = await rt();
    const h = rh();
    const sqlRes = await fetch(h + '/api/2.0/sql/statements', {
      method: 'POST',
      headers: { Authorization: `Bearer ${tk}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        warehouse_id: process.env.DATABRICKS_WAREHOUSE_ID,
        statement: `SELECT strategy_key, attempts, ROUND(best_score,3) AS best_score,
                           CAST(last_attempted_at AS STRING) AS last_attempted_at, exhausted
                    FROM serverless_stable_qh44kx_catalog.voynich.strategy_stats
                    ORDER BY best_score DESC`,
        wait_timeout: '15s',
      }),
    });
    const j: any = await sqlRes.json();
    const cols = (j.manifest?.schema?.columns || []).map((c: any) => c.name);
    const rows = (j.result?.data_array || []).map((row: any[]) => {
      const obj: any = {};
      cols.forEach((c: string, i: number) => (obj[c] = row[i]));
      return obj;
    });
    res.json({ rows });
  } catch (err: any) {
    res.json({ rows: [], error: String(err?.message ?? err) });
  }
});

// ---------------------------------------------------------------------------
// Results dashboard
// ---------------------------------------------------------------------------

app.get('/_apx/results', async (_req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.send(`<!DOCTYPE html>
<html>
<head>
  <title>Voynich Theory Results</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 24px; margin-bottom: 4px; color: #fff; }
    .subtitle { color: #888; margin-bottom: 20px; font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 14px; }
    .card .label { font-size: 10px; text-transform: uppercase; color: #888; margin-bottom: 4px; }
    .card .value { font-size: 26px; font-weight: 600; color: #4dd0e1; }
    .card .value.green { color: #4caf50; }
    .card .value.amber { color: #ffb74d; }
    .card .value.red { color: #ef5350; }
    .card .sub { font-size: 11px; color: #666; margin-top: 2px; }
    table { width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 8px; overflow: hidden; margin-bottom: 20px; }
    th { background: #252540; padding: 8px 10px; text-align: left; font-size: 11px; text-transform: uppercase; color: #888; }
    td { padding: 8px 10px; border-top: 1px solid #252540; font-size: 12px; }
    tr:hover { background: #252540; }
    .bar { height: 6px; border-radius: 3px; background: #333; display: inline-block; width: 60px; vertical-align: middle; }
    .bar-fill { height: 100%; border-radius: 3px; background: #4dd0e1; }
    .section { margin-bottom: 20px; }
    .section h2 { font-size: 15px; margin-bottom: 10px; color: #ccc; display: flex; align-items: center; gap: 8px; }
    .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #4caf50; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
    .activity-row { font-family: 'SF Mono', Monaco, monospace; font-size: 11px; line-height: 1.6; padding: 2px 0; border-bottom: 1px solid #1a1a2e; }
    .activity-row:hover { background: #252540; }
    .activity-row .time { color: #555; }
    .activity-row .score { color: #4dd0e1; font-weight: 600; }
    .activity-row .score.high { color: #4caf50; }
    .activity-row .decoded { color: #aaa; }
    .activity-row .origin { color: #7c4dff; font-size: 10px; }
    .status-bar { position: fixed; top: 0; left: 0; right: 0; background: #1a1a2e; border-bottom: 1px solid #333; padding: 8px 24px; display: flex; gap: 24px; align-items: center; font-size: 12px; z-index: 100; }
    .status-bar .live { color: #4caf50; }
    body { padding-top: 52px; }
    .tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; }
    .tag.latin { background: #1a2e1a; color: #81c784; }
    .tag.italian { background: #1a1a2e; color: #64b5f6; }
  </style>
</head>
<body>
  <div class="status-bar">
    <div><span class="live-dot" style="display:inline-block"></span></div>
    <div>Burst <b id="sb-burst">-</b>/14 · Round <b id="sb-round">-</b>/20</div>
    <div>🎯 <b id="sb-strategy" style="color:#bb86fc">-</b></div>
    <div>Best: <b id="sb-best" style="color:#4caf50">-</b></div>
    <div>Total: <b id="sb-total">-</b></div>
    <div style="margin-left:auto;color:#555" id="sb-ts">-</div>
  </div>

  <h1>Voynich Manuscript — Decipherment Search</h1>
  <p class="subtitle">Strategy-rotating hill-climb · 8 strategies × 20-round bursts · cold mode escapes the consensus basin</p>

  <div class="grid" id="summary"></div>

  <div class="section">
    <h2><span class="live-dot"></span> Strategy Rotation</h2>
    <table id="strategies-table">
      <thead><tr><th>Strategy</th><th>Lang</th><th>Cipher</th><th>Seed</th><th>Attempts</th><th>Best</th><th>Last Run</th><th>Status</th></tr></thead>
      <tbody id="strategies"></tbody>
    </table>
  </div>

  <div class="section">
    <h2><span class="live-dot"></span> Live Activity</h2>
    <div id="activity" style="background:#0d0d1a;border:1px solid #252540;border-radius:8px;padding:12px;max-height:360px;overflow-y:auto;font-family:'SF Mono',Monaco,monospace"></div>
  </div>

  <div class="section">
    <h2>Top Theories (All Time)</h2>
    <table>
      <thead><tr><th>Folio</th><th>Plant</th><th>Lang</th><th>Grounding</th><th>Consistency</th><th>Combined</th><th>Decoded Text</th></tr></thead>
      <tbody id="theories"></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Recent Best (Last 100)</h2>
    <table>
      <thead><tr><th>Folio</th><th>Plant</th><th>Lang</th><th>Grounding</th><th>Consistency</th><th>Combined</th><th>Decoded Text</th></tr></thead>
      <tbody id="recent"></tbody>
    </table>
  </div>

  <script>
    async function query(sql) {
      const res = await fetch('/api/sql', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ statement: sql }) });
      const d = await res.json();
      const cols = (d.manifest?.schema?.columns || []).map(c => c.name);
      return (d.result?.data_array || []).map(row => { const obj = {}; cols.forEach((c, i) => obj[c] = row[i]); return obj; });
    }

    function barHtml(val) {
      const pct = Math.min(parseFloat(val) * 100, 100);
      const color = pct > 30 ? '#4caf50' : pct > 15 ? '#ffb74d' : '#4dd0e1';
      return '<div class="bar"><div class="bar-fill" style="width:'+pct+'%;background:'+color+'"></div></div> '+val;
    }

    async function loadActivity() {
      try {
        const res = await fetch('/api/activity');
        const data = await res.json();
        document.getElementById('sb-burst').textContent = (data.burst ?? 0) + 1;
        document.getElementById('sb-round').textContent = (data.round ?? 0) + 1;
        document.getElementById('sb-strategy').textContent = data.activeStrategy || 'idle';
        document.getElementById('sb-best').textContent = (data.allTimeBest || 0).toFixed(3);
        document.getElementById('sb-ts').textContent = new Date().toLocaleTimeString();

        const el = document.getElementById('activity');
        if (!data.entries || data.entries.length === 0) {
          el.innerHTML = '<div style="color:#555;padding:8px">Waiting for first result...</div>';
          return;
        }
        el.innerHTML = data.entries.slice().reverse().map(e => {
          const combined = (e.grounding + e.consistency).toFixed(3);
          const isHigh = parseFloat(combined) > 0.3;
          const t = new Date(e.time).toLocaleTimeString();
          const seedTag = e.seedOrigin === 'cold'
            ? '<span style="background:#3a1a2e;color:#ff6ec7;padding:1px 4px;border-radius:3px;font-size:9px;">COLD</span>'
            : '<span style="background:#1a2e3a;color:#64b5f6;padding:1px 4px;border-radius:3px;font-size:9px;">ELITE</span>';
          return '<div class="activity-row">' +
            '<span class="time">'+t+'</span> ' +
            'B'+(e.batch+1)+'/R'+(e.round+1)+' ' +
            '<span class="tag '+(e.lang||'')+'">'+e.lang+'</span> ' +
            '<span style="color:#aaa;font-size:10px">'+(e.cipher||'sub').slice(0,5)+'</span> ' +
            seedTag+' ' +
            e.folio+' ('+(e.plant||'').slice(0,18)+') ' +
            '<span class="score'+(isHigh?' high':'')+'">'+combined+'</span> ' +
            'grd='+e.grounding.toFixed(3)+' cons='+e.consistency.toFixed(3)+' ' +
            'dict='+e.dictScore.toFixed(3)+' ' +
            '<span class="decoded">"'+e.decoded.slice(0,50)+'"</span>' +
            '</div>';
        }).join('');
      } catch(e) {}
    }

    async function loadStrategies() {
      try {
        const res = await fetch('/api/strategies');
        const data = await res.json();
        const active = (document.getElementById('sb-strategy').textContent || '').trim();
        const tbody = document.getElementById('strategies');
        if (!data.rows || data.rows.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" style="color:#555;text-align:center;padding:12px">No strategies attempted yet — first burst will populate this table.</td></tr>';
          return;
        }
        tbody.innerHTML = data.rows.map(r => {
          const parts = (r.strategy_key||'').split('|');
          const isActive = r.strategy_key === active;
          const status = r.exhausted === 'true' || r.exhausted === true
            ? '<span style="color:#ef5350">⊘ exhausted</span>'
            : '<span style="color:#4caf50">✓ progressing</span>';
          const seedColor = parts[2] === 'cold' ? '#ff6ec7' : '#64b5f6';
          const lastRun = r.last_attempted_at ? new Date(r.last_attempted_at.replace(' ','T')+'Z').toLocaleTimeString() : '-';
          const rowStyle = isActive ? 'background:#2a1f3d;border-left:3px solid #bb86fc' : '';
          const activeMark = isActive ? '🎯 ' : '';
          return '<tr style="'+rowStyle+'">' +
            '<td><b>'+activeMark+(r.strategy_key||'')+'</b></td>' +
            '<td><span class="tag '+parts[0]+'">'+parts[0]+'</span></td>' +
            '<td style="color:#aaa">'+parts[1]+'</td>' +
            '<td style="color:'+seedColor+';font-weight:600">'+parts[2]+'</td>' +
            '<td>'+r.attempts+'</td>' +
            '<td>'+barHtml(r.best_score)+'</td>' +
            '<td style="color:#888;font-size:10px">'+lastRun+'</td>' +
            '<td>'+status+'</td>' +
            '</tr>';
        }).join('');
      } catch(e) {}
    }

    async function loadDb() {
      const summary = await query('SELECT COUNT(*) total, ROUND(MAX(grounding_score+consistency_score),3) best, ROUND(MAX(grounding_score),3) best_grd, ROUND(MAX(consistency_score),3) best_cons, ROUND(AVG(grounding_score+consistency_score),3) avg FROM serverless_stable_qh44kx_catalog.voynich.theories');
      if (summary[0]) {
        const s = summary[0];
        document.getElementById('sb-total').textContent = s.total;
        document.getElementById('summary').innerHTML =
          '<div class="card"><div class="label">Total Theories</div><div class="value">'+s.total+'</div></div>' +
          '<div class="card"><div class="label">Best Combined</div><div class="value green">'+s.best+'</div></div>' +
          '<div class="card"><div class="label">Best Grounding</div><div class="value">'+s.best_grd+'</div></div>' +
          '<div class="card"><div class="label">Best Consistency</div><div class="value">'+s.best_cons+'</div></div>' +
          '<div class="card"><div class="label">Avg Combined</div><div class="value amber">'+s.avg+'</div></div>';
      }

      const theories = await query('SELECT target_folio, target_plant, source_language, ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons, ROUND(grounding_score+consistency_score,3) combined, SUBSTRING(decoded_text, 1, 80) decoded FROM serverless_stable_qh44kx_catalog.voynich.theories ORDER BY (grounding_score+consistency_score) DESC LIMIT 15');
      document.getElementById('theories').innerHTML = theories.map(t =>
        '<tr><td>'+t.target_folio+'</td><td>'+t.target_plant+'</td><td><span class="tag '+t.source_language+'">'+t.source_language+'</span></td><td>'+barHtml(t.grd)+'</td><td>'+barHtml(t.cons)+'</td><td><b>'+t.combined+'</b></td><td style="font-family:monospace;font-size:11px">'+t.decoded+'</td></tr>'
      ).join('');

      const recent = await query('SELECT target_folio, target_plant, source_language, ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons, ROUND(grounding_score+consistency_score,3) combined, SUBSTRING(decoded_text, 1, 80) decoded FROM serverless_stable_qh44kx_catalog.voynich.theories ORDER BY proposed_at DESC LIMIT 15');
      document.getElementById('recent').innerHTML = recent.map(t =>
        '<tr><td>'+t.target_folio+'</td><td>'+t.target_plant+'</td><td><span class="tag '+t.source_language+'">'+t.source_language+'</span></td><td>'+barHtml(t.grd)+'</td><td>'+barHtml(t.cons)+'</td><td><b>'+t.combined+'</b></td><td style="font-family:monospace;font-size:11px">'+t.decoded+'</td></tr>'
      ).join('');
    }

    // Poll activity every 3s, strategies every 8s, DB summary every 30s
    loadActivity();
    loadStrategies();
    loadDb();
    setInterval(loadActivity, 3000);
    setInterval(loadStrategies, 8000);
    setInterval(loadDb, 30000);
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
    const sqlRes = await fetch(h + '/api/2.0/sql/statements', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tk },
      body: JSON.stringify({ warehouse_id: process.env.DATABRICKS_WAREHOUSE_ID, statement: req.body.statement, wait_timeout: '30s' }),
    });
    res.json(await sqlRes.json());
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
  console.log(`  Routing: LLM-based (${allTools.length} tools across 2 routes)`);
  console.log(`  POST /responses              — agent endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json — A2A discovery card`);
  console.log(`  /mcp                         — MCP server`);
  console.log(`  /_apx/agent                  — dev chat UI`);
  console.log(`  /_apx/tools                  — tool inspector`);
  console.log(`  /_apx/results                — theory results dashboard`);
  console.log(`  /_apx/traces                 — agent trace viewer`);
});
server.timeout = 300_000;
server.keepAliveTimeout = 90_000;

// Auto-start based on LOOP_MODE
const mode = process.env.LOOP_MODE ?? 'theory';

if (mode === 'theory') {
  console.log('[orchestrator] starting continuous theory-driven investigation');
  import('./theory-loop.ts').then(async (m) => {
    // Wire live activity reporting
    m.setOnTheoryResult((entry) => logActivity(entry));

    let batch = 0;
    while (true) {
      batch++;
      console.log(`[orchestrator] === BATCH ${batch} starting (14 bursts × 20 rounds) ===`);
      try {
        const theories = await m.runTheoryLoop(14, batch);
        const best = theories[0];
        const bestCombined = best ? (best.grounding_score + best.consistency_score).toFixed(3) : '0';
        console.log(`[orchestrator] === BATCH ${batch} complete: ${theories.length} theories, best_combined=${bestCombined} ===`);
      } catch (err) {
        console.error(`[orchestrator] batch ${batch} crashed:`, err);
        await new Promise((r) => setTimeout(r, 30_000));
      }
    }
  });
} else if (process.env.AUTO_START_LOOP !== 'false') {
  console.log('[orchestrator] auto-starting evolutionary loop');
  evolutionaryAgent.startLoop();
}

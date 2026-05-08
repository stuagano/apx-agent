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
import { StatEvolutionaryAgent } from './stat-evolutionary-agent.ts';

// ---------------------------------------------------------------------------
// Log ring buffer — intercept console.* so the dashboard can stream live logs
// ---------------------------------------------------------------------------

const LOG_RING: { ts: number; line: string }[] = [];
const LOG_RING_MAX = 500;
const _origLog = console.log.bind(console);
const _origWarn = console.warn.bind(console);
const _origError = console.error.bind(console);
function _capture(...args: unknown[]) {
  const line = args.map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
  LOG_RING.push({ ts: Date.now(), line });
  if (LOG_RING.length > LOG_RING_MAX) LOG_RING.shift();
}
console.log = (...args: unknown[]) => { _origLog(...args); _capture(...args); };
console.warn = (...args: unknown[]) => { _origWarn(...args); _capture(...args); };
console.error = (...args: unknown[]) => { _origError(...args); _capture(...args); };

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

// Critic agent URL for the per-round critique flow in runTheoryLoop.
// Prefer an explicit CRITIC_AGENT_URL env var; fall back to a URL in
// FITNESS_AGENT_URLS that contains 'critic'. Optional — if neither is
// present, runTheoryLoop falls back to skeptic-only critique.
const criticAgentUrl =
  process.env.CRITIC_AGENT_URL ??
  fitnessAgentUrls.find((u) => u.includes('critic')) ??
  '';
if (criticAgentUrl) {
  process.env.CRITIC_AGENT_URL = criticAgentUrl;
  console.log(`[orchestrator] critic agent: ${criticAgentUrl}`);
} else {
  console.warn('[orchestrator] CRITIC_AGENT_URL not set and no critic in FITNESS_AGENT_URLS — falling back to skeptic-only critique');
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
// Route 3: Statistical EA (hypothesis generation + execution + critic scoring)
// ---------------------------------------------------------------------------

const statMutationUrl = process.env.STAT_MUTATION_AGENT_URL ?? '';
const statExecutorUrl = process.env.STAT_EXECUTOR_AGENT_URL ?? '';
if (!statMutationUrl) {
  console.warn('[orchestrator] STAT_MUTATION_AGENT_URL not set — stat EA will be unavailable');
}
if (!statExecutorUrl) {
  console.warn('[orchestrator] STAT_EXECUTOR_AGENT_URL not set — stat EA will be unavailable');
}

const statAgent = new StatEvolutionaryAgent({
  mutationAgentUrl: statMutationUrl,
  executorAgentUrl: statExecutorUrl,
  criticAgentUrl: criticAgentUrl,
});

// ---------------------------------------------------------------------------
// LLM-routed orchestrator
// ---------------------------------------------------------------------------

const EA_KEYWORDS = ['generation', 'evolut', 'population', 'pareto', 'fitness', 'escalat', 'pause', 'resume', 'converge'];
const THEORY_KEYWORDS = ['theory', 'theor', 'decode', 'decipher', 'cipher', 'symbol map', 'folio', 'propose', 'investigate', 'skeptic', 'cross-folio', 'latin', 'polyalphabetic', 'substitution'];
const STAT_KEYWORDS = ['analyz', 'hypothesis', 'statistic', 'run stat', 'finding', 'test feature', 'test family', 'ea loop', 'stat loop'];

const router = new RouterAgent({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are routing requests for a Voynich manuscript decipherment system.',
    'Choose between three specialist agents:',
    '',
    '- "ea_management": For managing the evolutionary algorithm loop — checking generation',
    '  status, viewing fitness scores, pausing/resuming the loop, escalating hypotheses,',
    '  or asking about population-level statistics.',
    '',
    '- "theory_investigation": For targeted decipherment work — proposing new decoding',
    '  theories, testing symbol maps against folios, running cross-folio consistency checks,',
    '  challenging theories with the skeptic, or investigating specific cipher types and languages.',
    '',
    '- "statistical_analysis": For statistical hypothesis testing about EVA vocabulary — ',
    '  running the statistical EA loop, listing findings, testing features or family comparisons.',
    '',
    'If the user asks a general question about Voynich progress or "what\'s working",',
    'route to ea_management. If they want to try a new approach or test a specific idea,',
    'route to theory_investigation. If they want to analyze EVA features or run statistical tests,',
    'route to statistical_analysis.',
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
    {
      name: 'statistical_analysis',
      description: 'Statistical hypothesis generation — propose and test new features/family comparisons about EVA vocabulary',
      agent: statAgent,
      condition: (msgs) => {
        const last = msgs[msgs.length - 1]?.content?.toLowerCase() ?? '';
        return STAT_KEYWORDS.some((kw) => last.includes(kw));
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
  ...statAgent.collectTools(),
];

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich decipherment orchestrator.',
    'You manage an evolutionary search loop, a targeted theory investigation system, and a statistical analysis EA.',
    '',
    'You have three categories of tools:',
    '  EA Management: evolution_status, best_hypothesis, generation_summary, pause_evolution, resume_evolution, force_escalate',
    '  Theory Investigation: propose_theory, challenge_theory, run_theory_loop, list_theories',
    '  Statistical Analysis: run_stat_loop, list_findings',
    '',
    'When users ask about progress, status, generations, or fitness — use the EA tools.',
    'When users want to try decoding theories, test cipher types, or investigate specific folios — use the theory tools.',
    'When users ask about analyzing EVA features, testing hypotheses, or running statistical tests — use the stat tools.',
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

// Log stream endpoint — returns lines added after `since` timestamp
app.get('/api/logs', (req, res) => {
  const since = parseInt(String(req.query.since ?? '0')) || 0;
  const lines = since ? LOG_RING.filter(l => l.ts > since) : LOG_RING.slice(-100);
  res.json({ lines, lastTs: lines.length ? lines[lines.length - 1].ts : since });
});

// ---------------------------------------------------------------------------
// Results dashboard
// ---------------------------------------------------------------------------

app.get('/_apx/results', async (_req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.send(`<!DOCTYPE html>
<html>
<head>
  <title>Voynich — SA Search</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; padding: 24px; padding-top: 52px; }
    h1 { font-size: 22px; margin-bottom: 4px; color: #fff; }
    .subtitle { color: #666; margin-bottom: 20px; font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
    .card { background: #1a1a2e; border: 1px solid #2a2a45; border-radius: 8px; padding: 12px 14px; }
    .card .label { font-size: 10px; text-transform: uppercase; color: #666; margin-bottom: 4px; letter-spacing: .05em; }
    .card .value { font-size: 24px; font-weight: 700; color: #4dd0e1; }
    .card .value.green { color: #4caf50; }
    .card .value.amber { color: #ffb74d; }
    .card .sub { font-size: 11px; color: #555; margin-top: 3px; }
    table { width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 8px; overflow: hidden; margin-bottom: 20px; }
    th { background: #20203a; padding: 7px 10px; text-align: left; font-size: 10px; text-transform: uppercase; color: #666; letter-spacing: .05em; }
    td { padding: 7px 10px; border-top: 1px solid #20203a; font-size: 12px; }
    tr:hover td { background: #20203a; }
    .bar { height: 5px; border-radius: 3px; background: #252540; display: inline-block; width: 50px; vertical-align: middle; margin-right: 4px; }
    .bar-fill { height: 100%; border-radius: 3px; }
    .section { margin-bottom: 22px; }
    .section h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: #888; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
    .live-dot { width: 7px; height: 7px; border-radius: 50%; background: #4caf50; animation: pulse 1.5s infinite; flex-shrink: 0; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
    .status-bar { position: fixed; top: 0; left: 0; right: 0; background: #12122a; border-bottom: 1px solid #252540; padding: 7px 24px; display: flex; gap: 20px; align-items: center; font-size: 12px; z-index: 100; }
    .tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; }
    .tag.latin { background: #1a2e1a; color: #81c784; }
    .tag.italian { background: #1a1a2e; color: #64b5f6; }
    .mono { font-family: "SF Mono", Monaco, monospace; }
    /* SA Health */
    .sa-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
    .sa-panel { background: #1a1a2e; border: 1px solid #2a2a45; border-radius: 8px; padding: 14px; }
    .sa-panel h3 { font-size: 11px; text-transform: uppercase; color: #666; letter-spacing: .07em; margin-bottom: 10px; }
    .chip { display: inline-block; padding: 3px 7px; border-radius: 4px; font-size: 11px; font-family: "SF Mono", Monaco, monospace; margin: 2px; font-weight: 600; }
    .chip.elite { background: #0d2035; color: #64b5f6; border: 1px solid #1a4060; }
    .chip.cold  { background: #2a1535; color: #ce93d8; border: 1px solid #4a2560; }
    .chip.best  { border-color: #4caf50; }
    .avg-row { margin-top: 8px; font-size: 11px; font-family: "SF Mono", Monaco, monospace; }
    .avg-row .e { color: #64b5f6; } .avg-row .c { color: #ce93d8; }
    .warn { color: #ffb74d; font-size: 11px; margin-top: 6px; }
    /* Activity */
    .act-row { font-family: "SF Mono", Monaco, monospace; font-size: 11px; line-height: 1.7; padding: 1px 4px; border-radius: 3px; }
    .act-row:hover { background: #1e1e38; }
    /* Log */
    #live-log { background: #05050f; border: 1px solid #1a1a30; border-radius: 8px; padding: 10px 12px; height: 260px; overflow-y: auto; font-family: "SF Mono", Monaco, monospace; font-size: 11px; line-height: 1.55; white-space: pre-wrap; word-break: break-all; }
  </style>
</head>
<body>
  <div class="status-bar">
    <div><span class="live-dot" style="display:inline-block"></span></div>
    <div style="color:#888">Round <b id="sb-round" style="color:#ccc">-</b></div>
    <div style="color:#888">Strategy <b id="sb-strategy" style="color:#bb86fc">-</b></div>
    <div style="color:#888">Best <b id="sb-best" style="color:#4caf50">-</b></div>
    <div style="color:#888">Total <b id="sb-total" style="color:#ccc">-</b></div>
    <div style="margin-left:auto;color:#444;font-size:11px" id="sb-ts">-</div>
  </div>

  <h1>Voynich — Homophonic Search</h1>
  <p class="subtitle">8 restarts/round (2 elite + 6 cold) · 4000 SA steps each · CONSENSUS_LOCKED: ch→s y→t c→n</p>

  <div id="db-error"></div>
  <div class="grid" id="summary"></div>

  <div class="section">
    <h2><span class="live-dot"></span> SA Health</h2>
    <div class="sa-grid">
      <div class="sa-panel">
        <h3>Last Round — Restart Breakdown</h3>
        <div id="sa-chips"><span style="color:#444">Waiting for first round...</span></div>
        <div id="sa-avgs" class="avg-row"></div>
        <div id="sa-warn"></div>
      </div>
      <div class="sa-panel">
        <h3>Recent SA Runs (last 16)</h3>
        <div id="sa-runs" style="overflow-y:auto;max-height:140px">
          <span style="color:#444">Waiting...</span>
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2><span class="live-dot"></span> Live Activity</h2>
    <div id="activity" style="background:#0d0d1a;border:1px solid #20203a;border-radius:8px;padding:10px 12px;max-height:300px;overflow-y:auto"></div>
  </div>

  <div class="section" id="champ-section">
    <h2>Champion Decipherment</h2>
    <div id="champ-error"></div>
    <div class="sa-grid">
      <div class="sa-panel">
        <h3>Translation Key — EVA → Italian</h3>
        <div id="champ-key"><span style="color:#444">Loading...</span></div>
      </div>
      <div class="sa-panel">
        <h3>Best Theory</h3>
        <div id="champ-info"><span style="color:#444">Loading...</span></div>
      </div>
    </div>
    <div class="sa-panel" style="margin-top:12px">
      <h3>Decoded Text — word by word</h3>
      <div style="font-size:10px;color:#555;margin-bottom:10px">
        <span style="background:#1a3a1a;color:#4caf50;padding:1px 5px;border-radius:3px;margin-right:8px">expected botanical term</span>
        <span style="background:#172030;color:#64b5f6;padding:1px 5px;border-radius:3px;margin-right:8px">Italian dictionary word</span>
        <span style="color:#444;padding:1px 5px;border-radius:3px;border:1px solid #1a1a2e">unmatched — hover for EVA</span>
      </div>
      <div id="champ-decoded" style="line-height:2.2;font-family:monospace;font-size:12px"><span style="color:#444">Loading...</span></div>
    </div>
  </div>

  <div class="section">
    <h2>Top Theories (All Time)</h2>
    <table>
      <thead><tr><th>Folio</th><th>Plant</th><th>Lang</th><th>Grounding</th><th>Consistency</th><th>Combined</th><th>Decoded</th></tr></thead>
      <tbody id="theories"></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Recent Theories</h2>
    <table>
      <thead><tr><th>Folio</th><th>Plant</th><th>Lang</th><th>Grounding</th><th>Consistency</th><th>Combined</th><th>Decoded</th></tr></thead>
      <tbody id="recent"></tbody>
    </table>
  </div>

  <div class="section">
    <h2><span class="live-dot"></span> Live Log</h2>
    <div id="live-log"></div>
  </div>

  <script>
    async function query(sql) {
      const res = await fetch('/api/sql', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ statement: sql }) });
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      if (d.status?.state === 'FAILED') throw new Error(d.status?.error?.message || JSON.stringify(d.status));
      const cols = (d.manifest?.schema?.columns || []).map(c => c.name);
      return (d.result?.data_array || []).map(row => { const o = {}; cols.forEach((c,i) => o[c]=row[i]); return o; });
    }

    function barHtml(val) {
      const pct = Math.min(parseFloat(val||0) * 100, 100);
      const color = pct > 30 ? '#4caf50' : pct > 15 ? '#ffb74d' : '#4dd0e1';
      return '<div class="bar"><div class="bar-fill" style="width:'+pct+'%;background:'+color+'"></div></div>'+parseFloat(val||0).toFixed(3);
    }

    // ── SA Health state (parsed from log stream) ──────────────────────────
    const saState = {
      restarts: [],   // [{mode:'e'|'c', score:number}] from last restart line
      runs: [],       // [{impr, dict, lm, combined}] last 16 SA summary lines
    };

    const SA_RUN_RE   = /homophonic SA: (\d+) improvements in \d+ steps, dict=([\d.]+) lm=([\d.]+) combined=([\d.]+)/;
    const RESTART_RE  = /N=\d+ restarts → best=[\d.]+ \[([e\d.: c]+)\]/;

    function parseLogLine(line) {
      let m = RESTART_RE.exec(line);
      if (m) {
        saState.restarts = m[1].trim().split(/\s+/).map(tok => {
          const [mode, score] = tok.split(':');
          return { mode, score: parseFloat(score) };
        });
        renderSaChips();
      }
      m = SA_RUN_RE.exec(line);
      if (m) {
        saState.runs.unshift({ impr: parseInt(m[1]), dict: parseFloat(m[2]), lm: parseFloat(m[3]), combined: parseFloat(m[4]) });
        if (saState.runs.length > 16) saState.runs.length = 16;
        renderSaRuns();
      }
    }

    function renderSaChips() {
      const chips = saState.restarts;
      if (!chips.length) return;
      const best = Math.max(...chips.map(c => c.score));
      const eliteScores = chips.filter(c => c.mode === 'e').map(c => c.score);
      const coldScores  = chips.filter(c => c.mode === 'c').map(c => c.score);
      const avg = arr => arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : 0;
      const eAvg = avg(eliteScores), cAvg = avg(coldScores);

      document.getElementById('sa-chips').innerHTML = chips.map(c =>
        '<span class="chip '+(c.mode==='e'?'elite':'cold')+(c.score===best?' best':'')+'">'+
        (c.mode==='e'?'E':'C')+':'+c.score.toFixed(3)+'</span>'
      ).join('');

      document.getElementById('sa-avgs').innerHTML =
        '<span class="e">elite avg '+eAvg.toFixed(3)+'</span>  <span class="c">cold avg '+cAvg.toFixed(3)+'</span>' +
        '  best '+best.toFixed(3);

      const warn = document.getElementById('sa-warn');
      if (cAvg > eAvg + 0.02)
        warn.innerHTML = '<div class="warn">⚠ cold avg beating elite — elite pool may be anchoring a local optimum</div>';
      else if (eAvg > cAvg + 0.02)
        warn.innerHTML = '';
      else
        warn.innerHTML = '';
    }

    function renderSaRuns() {
      const runs = saState.runs;
      if (!runs.length) return;
      const maxComb = Math.max(...runs.map(r => r.combined));
      document.getElementById('sa-runs').innerHTML =
        '<table style="width:100%;border-collapse:collapse"><thead><tr>' +
        '<th style="color:#555;font-size:10px;text-align:right;padding:2px 6px">impr</th>' +
        '<th style="color:#555;font-size:10px;text-align:right;padding:2px 6px">dict</th>' +
        '<th style="color:#555;font-size:10px;text-align:right;padding:2px 6px">lm</th>' +
        '<th style="color:#555;font-size:10px;text-align:right;padding:2px 6px">combined</th>' +
        '</tr></thead><tbody>' +
        runs.map(r => {
          const isBest = r.combined === maxComb;
          const color = r.combined > 0.35 ? '#4caf50' : r.combined > 0.25 ? '#ffb74d' : '#4dd0e1';
          return '<tr style="'+(isBest?'background:#0d1f0d;':'')+'">' +
            '<td style="text-align:right;color:#888;padding:1px 6px;font-size:11px;font-family:monospace">'+r.impr+'</td>' +
            '<td style="text-align:right;color:#666;padding:1px 6px;font-size:11px;font-family:monospace">'+r.dict.toFixed(3)+'</td>' +
            '<td style="text-align:right;color:#666;padding:1px 6px;font-size:11px;font-family:monospace">'+r.lm.toFixed(3)+'</td>' +
            '<td style="text-align:right;color:'+color+';padding:1px 6px;font-size:11px;font-family:monospace;font-weight:'+(isBest?700:400)+'">'+(isBest?'★ ':'')+r.combined.toFixed(3)+'</td>' +
          '</tr>';
        }).join('') +
        '</tbody></table>';
    }

    // ── Activity ────────────────────────────────────────────────────────────
    async function loadActivity() {
      try {
        const res = await fetch('/api/activity');
        const data = await res.json();
        document.getElementById('sb-round').textContent = (data.round ?? 0) + 1;
        document.getElementById('sb-strategy').textContent = (data.activeStrategy || 'idle').replace(/\|/g, ' ');
        document.getElementById('sb-best').textContent = (data.allTimeBest || 0).toFixed(3);
        document.getElementById('sb-ts').textContent = new Date().toLocaleTimeString();

        const el = document.getElementById('activity');
        if (!data.entries?.length) {
          el.innerHTML = '<div style="color:#444;padding:6px;font-size:12px">Waiting for first result...</div>';
          return;
        }
        el.innerHTML = data.entries.slice().reverse().map(e => {
          const combined = (e.grounding + e.consistency).toFixed(3);
          const color = parseFloat(combined) > 0.35 ? '#4caf50' : parseFloat(combined) > 0.25 ? '#ffb74d' : '#4dd0e1';
          const t = new Date(e.time).toLocaleTimeString();
          return '<div class="act-row">' +
            '<span style="color:#444">'+t+'</span>  ' +
            '<span class="tag '+(e.lang||'')+'">'+e.lang+'</span> ' +
            e.folio+' <span style="color:#555;font-size:10px">('+((e.plant||'').slice(0,16))+')</span>  ' +
            '<b style="color:'+color+'">'+combined+'</b>  ' +
            '<span style="color:#555">grd='+e.grounding.toFixed(3)+' cons='+e.consistency.toFixed(3)+'</span>  ' +
            '<span style="color:#777;font-style:italic">"'+e.decoded.slice(0,60)+'"</span>' +
            '</div>';
        }).join('');
      } catch(e) {}
    }

    // ── DB tables ───────────────────────────────────────────────────────────
    async function loadDb() {
      try {
        const summary = await query(
          'SELECT COUNT(*) total,' +
          ' ROUND(MAX(grounding_score+consistency_score),3) best,' +
          ' ROUND(MAX(grounding_score),3) best_grd,' +
          ' ROUND(AVG(grounding_score+consistency_score),3) avg,' +
          ' COUNT(CASE WHEN proposed_at >= current_timestamp() - INTERVAL 1 HOUR THEN 1 END) last_hour' +
          ' FROM serverless_stable_qh44kx_catalog.voynich.theories'
        );
        if (summary[0]) {
          const s = summary[0];
          document.getElementById('sb-total').textContent = s.total;
          document.getElementById('summary').innerHTML =
            '<div class="card"><div class="label">Total Theories</div><div class="value">'+s.total+'</div><div class="sub">'+s.last_hour+'/hr</div></div>' +
            '<div class="card"><div class="label">Best Combined</div><div class="value green">'+s.best+'</div><div class="sub">all time</div></div>' +
            '<div class="card"><div class="label">Best Grounding</div><div class="value">'+s.best_grd+'</div></div>' +
            '<div class="card"><div class="label">Avg Combined</div><div class="value amber">'+s.avg+'</div></div>';
        } else {
          document.getElementById('db-error').innerHTML =
            '<div style="background:#2a0a0a;border:1px solid #5a1a1a;border-radius:6px;padding:8px 12px;color:#ef5350;font-size:12px;margin-bottom:14px">SQL returned no rows — check warehouse access</div>';
        }

        const theories = await query(
          'SELECT target_folio, target_plant, source_language,' +
          ' ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons,' +
          ' ROUND(grounding_score+consistency_score,3) combined,' +
          ' SUBSTRING(decoded_text,1,90) decoded' +
          ' FROM serverless_stable_qh44kx_catalog.voynich.theories' +
          ' ORDER BY (grounding_score+consistency_score) DESC LIMIT 12'
        );
        document.getElementById('theories').innerHTML = theories.length
          ? theories.map(t => '<tr>' +
              '<td class="mono" style="color:#aaa">'+t.target_folio+'</td>' +
              '<td style="color:#888;font-size:11px">'+t.target_plant+'</td>' +
              '<td><span class="tag '+t.source_language+'">'+t.source_language+'</span></td>' +
              '<td>'+barHtml(t.grd)+'</td><td>'+barHtml(t.cons)+'</td>' +
              '<td><b style="color:'+(parseFloat(t.combined)>0.35?'#4caf50':parseFloat(t.combined)>0.25?'#ffb74d':'#4dd0e1')+'">'+t.combined+'</b></td>' +
              '<td class="mono" style="font-size:11px;color:#888">'+t.decoded+'</td>' +
            '</tr>').join('')
          : '<tr><td colspan="7" style="color:#444;text-align:center;padding:12px">No theories yet</td></tr>';

        const recent = await query(
          'SELECT target_folio, target_plant, source_language,' +
          ' ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons,' +
          ' ROUND(grounding_score+consistency_score,3) combined,' +
          ' SUBSTRING(decoded_text,1,90) decoded' +
          ' FROM serverless_stable_qh44kx_catalog.voynich.theories' +
          ' ORDER BY proposed_at DESC LIMIT 12'
        );
        document.getElementById('recent').innerHTML = recent.length
          ? recent.map(t => '<tr>' +
              '<td class="mono" style="color:#aaa">'+t.target_folio+'</td>' +
              '<td style="color:#888;font-size:11px">'+t.target_plant+'</td>' +
              '<td><span class="tag '+t.source_language+'">'+t.source_language+'</span></td>' +
              '<td>'+barHtml(t.grd)+'</td><td>'+barHtml(t.cons)+'</td>' +
              '<td><b style="color:'+(parseFloat(t.combined)>0.35?'#4caf50':parseFloat(t.combined)>0.25?'#ffb74d':'#4dd0e1')+'">'+t.combined+'</b></td>' +
              '<td class="mono" style="font-size:11px;color:#888">'+t.decoded+'</td>' +
            '</tr>').join('')
          : '<tr><td colspan="7" style="color:#444;text-align:center;padding:12px">No theories yet</td></tr>';
      } catch(err) {
        document.getElementById('db-error').innerHTML =
          '<div style="background:#2a0a0a;border:1px solid #5a1a1a;border-radius:6px;padding:8px 12px;color:#ef5350;font-size:12px;margin-bottom:14px"><b>SQL Error:</b> '+String(err?.message||err)+'</div>';
      }
    }

    // ── Log stream ──────────────────────────────────────────────────────────
    let logLastTs = 0;
    function logColor(line) {
      if (/\bsanat\b/i.test(line))              return '#4caf50';
      if (/restarts →/.test(line))              return '#bb86fc';
      if (/SA step \d+\/\d+/.test(line))        return '#4dd0e1';
      if (/homophonic SA:/.test(line))          return '#64b5f6';
      if (/homophonic seed:/.test(line))        return '#4a90c4';
      if (/Burst \d|Round \d/i.test(line))      return '#9575cd';
      if (/rejected|FAIL/i.test(line))          return '#ef5350';
      if (/warn/i.test(line))                   return '#ffb74d';
      return '#555';
    }
    async function loadLogs() {
      try {
        const res = await fetch('/api/logs?since=' + logLastTs);
        const data = await res.json();
        if (!data.lines?.length) return;
        logLastTs = data.lastTs;
        const el = document.getElementById('live-log');
        const atBottom = el.scrollHeight - el.scrollTop <= el.clientHeight + 40;
        data.lines.forEach(l => {
          parseLogLine(l.line);
          const d = document.createElement('div');
          d.style.color = logColor(l.line);
          d.textContent = new Date(l.ts).toLocaleTimeString() + '  ' + l.line;
          el.appendChild(d);
        });
        while (el.childNodes.length > 500) el.removeChild(el.firstChild);
        if (atBottom) el.scrollTop = el.scrollHeight;
      } catch(e) {}
    }

    // ── Champion Decipherment ────────────────────────────────────────────────
    // Small Italian dictionary for secondary highlighting (not botanical but real words)
    const ITALIAN_WORDS = new Set(['il','la','lo','le','gli','un','una','di','del','della','dei','delle','degli','in','per','con','non','che','ha','si','al','nel','sul','tra','fra','da','a','e','o','ma','se','ne','ci','vi','lui','lei','noi','voi','io','tu','ho','ai','agli','alle','sui','fra','era','sono','è','ed','od','né']);

    async function loadChampionDecipherment() {
      try {
        const data = await fetch('/api/champion-decipherment').then(r => r.json());
        if (data.error) {
          document.getElementById('champ-error').innerHTML =
            '<div style="background:#2a0a0a;border:1px solid #5a1a1a;border-radius:6px;padding:8px 12px;color:#ef5350;font-size:12px;margin-bottom:14px">'+data.error+'</div>';
          return;
        }

        // Info panel
        const combined = (data.grounding + data.consistency).toFixed(3);
        document.getElementById('champ-info').innerHTML =
          '<div style="margin-bottom:10px">' +
          '<div style="color:#ccc;font-size:14px;font-weight:600">' + data.folio + ' — ' + (data.plant || 'unknown') + '</div>' +
          '<div style="font-size:11px;color:#666;margin-top:4px;font-family:monospace">' +
          'grounding=' + data.grounding.toFixed(3) + '  consistency=' + data.consistency.toFixed(3) + '  combined=<b style="color:#4caf50">' + combined + '</b>' +
          '</div><div style="font-size:10px;color:#444;margin-top:4px">id: ' + data.id + '</div>' +
          '</div>' +
          '<div style="font-size:11px;color:#555;margin-top:8px">Expected botanical terms:</div>' +
          '<div style="margin-top:4px;line-height:1.8">' +
          (data.expectedTerms || []).map(t =>
            '<span style="display:inline-block;background:#0d2a0d;border:1px solid #1a4a1a;border-radius:3px;color:#81c784;font-size:11px;padding:1px 6px;margin:2px;font-family:monospace">'+t+'</span>'
          ).join('') +
          '</div>';

        // Translation key — sort by EVA symbol length then alphabetically
        const entries = Object.entries(data.symbolMap || {})
          .sort((a, b) => a[0].length - b[0].length || a[0].localeCompare(b[0]));
        document.getElementById('champ-key').innerHTML =
          '<div style="display:flex;flex-wrap:wrap;gap:5px">' +
          entries.map(([eva, letter]) =>
            '<div style="background:#0d0d20;border:1px solid #1e1e38;border-radius:4px;padding:3px 8px;font-family:monospace;font-size:12px;white-space:nowrap">' +
            '<span style="color:#9575cd">' + eva + '</span>' +
            '<span style="color:#333"> → </span>' +
            '<span style="color:#4dd0e1;font-weight:700">' + letter + '</span>' +
            '</div>'
          ).join('') +
          '</div>';

        // Word-by-word decoded text
        const evaWords = (data.evaText || '').trim().split(/\s+/).filter(Boolean);
        const decWords = (data.decodedText || '').trim().split(/\s+/).filter(Boolean);
        const termSet = new Set((data.expectedTerms || []).map(t => t.toLowerCase()));

        document.getElementById('champ-decoded').innerHTML =
          evaWords.map((eva, i) => {
            const dec = (decWords[i] || '').toLowerCase();
            const raw = decWords[i] || '?';
            const isTerm = termSet.has(dec);
            const isItalian = !isTerm && ITALIAN_WORDS.has(dec);
            const bg = isTerm ? '#1a3a1a' : isItalian ? '#172030' : 'transparent';
            const border = isTerm ? '1px solid #2a5a2a' : isItalian ? '1px solid #1e3048' : '1px solid #1a1a2e';
            const color = isTerm ? '#66bb6a' : isItalian ? '#64b5f6' : '#555';
            const weight = isTerm ? '700' : '400';
            return '<span title="EVA: ' + eva + '" style="display:inline-block;margin:2px 3px;padding:3px 7px;border-radius:4px;background:'+bg+';border:'+border+';cursor:default;vertical-align:middle">' +
              '<span style="font-size:9px;color:#333;display:block;line-height:1.2;text-align:center">'+eva+'</span>' +
              '<span style="color:'+color+';font-weight:'+weight+';font-size:12px">'+raw+'</span>' +
              '</span>';
          }).join('');

      } catch(e) {
        document.getElementById('champ-error').innerHTML =
          '<div style="color:#ef5350;font-size:12px">Failed to load: ' + e.message + '</div>';
      }
    }

    loadLogs(); loadActivity(); loadDb(); loadChampionDecipherment();
    setInterval(loadLogs, 1000);
    setInterval(loadActivity, 3000);
    setInterval(loadDb, 30000);
    setInterval(loadChampionDecipherment, 120000);
  </script>
</body>
</html>`);
});

// Champion decipherment — best theory's symbol map, EVA text, and expected terms
app.get('/api/champion-decipherment', async (_req, res) => {
  try {
    const { resolveToken: rt, resolveHost: rh } = await import('./appkit-agent/index.mjs');
    const tk = await rt();
    const h = rh();
    const wid = process.env.DATABRICKS_WAREHOUSE_ID;

    async function sql(statement: string) {
      const r = await fetch(h + '/api/2.0/sql/statements', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tk },
        body: JSON.stringify({ warehouse_id: wid, statement, wait_timeout: '30s' }),
      });
      const d = await r.json() as { manifest?: { schema?: { columns?: { name: string }[] } }; result?: { data_array?: string[][] } };
      const cols = (d.manifest?.schema?.columns ?? []).map((c) => c.name);
      return (d.result?.data_array ?? []).map((row) => {
        const o: Record<string, string> = {};
        cols.forEach((c, i) => { o[c] = row[i]; });
        return o;
      });
    }

    // Best theory
    const theories = await sql(
      `SELECT id, target_folio, target_plant, symbol_map, decoded_text,
              grounding_score, consistency_score
       FROM serverless_stable_qh44kx_catalog.voynich.theories
       WHERE source_language = 'italian' AND cipher_type = 'substitution'
         AND symbol_map IS NOT NULL AND symbol_map != '{}'
       ORDER BY grounding_score + consistency_score DESC LIMIT 1`
    );
    if (!theories.length) return res.json({ error: 'no theories yet' });
    const t = theories[0];

    // EVA text for that folio
    const evaRows = await sql(
      `SELECT eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
       WHERE folio_id = '${t.target_folio}' AND section = 'herbal' LIMIT 1`
    );
    const evaText = evaRows[0]?.eva_text ?? '';

    // Expected terms for that folio
    const folioRows = await sql(
      `SELECT expected_terms FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
       WHERE folio_id = '${t.target_folio}' LIMIT 1`
    );
    let expectedTerms: string[] = [];
    try {
      const parsed = JSON.parse(folioRows[0]?.expected_terms ?? '{}') as Record<string, string[]>;
      expectedTerms = (parsed.italian ?? []).map((s) => s.toLowerCase());
    } catch { /* leave empty */ }

    res.json({
      id: t.id,
      folio: t.target_folio,
      plant: t.target_plant,
      symbolMap: JSON.parse(t.symbol_map) as Record<string, string>,
      decodedText: t.decoded_text,
      evaText,
      expectedTerms,
      grounding: parseFloat(t.grounding_score),
      consistency: parseFloat(t.consistency_score),
    });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
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
      console.log(`[orchestrator] === BATCH ${batch} starting (22 bursts × 20 rounds) ===`);
      try {
        const theories = await m.runTheoryLoop(22, batch);
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

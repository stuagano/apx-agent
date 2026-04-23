/**
 * Agent Trace Dashboard
 *
 * Serves an HTML page that visualizes the multi-agent orchestration loop:
 * - Which agents are called, in what order
 * - What tools each agent invokes
 * - Input/output data flowing between agents
 * - Timing per step
 *
 * Mounted at /_apx/traces on the orchestrator app.
 */

import type { Express } from 'express';

export function mountTraceDashboard(app: Express): void {

  // Trace event buffer — the theory loop pushes events here
  const traceEvents: TraceEvent[] = [];
  const MAX_EVENTS = 500;

  app.get('/_apx/traces', (_req, res) => {
    res.setHeader('Content-Type', 'text/html');
    res.send(DASHBOARD_HTML);
  });

  // SSE stream for real-time updates
  app.get('/_apx/traces/stream', (req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');

    // Send existing events
    for (const evt of traceEvents) {
      res.write('data: ' + JSON.stringify(evt) + '\n\n');
    }

    // Keep connection open for new events
    const interval = setInterval(() => {
      res.write(': keepalive\n\n');
    }, 15000);

    req.on('close', () => clearInterval(interval));
  });

  // API to push trace events (called by theory loop)
  app.post('/_apx/traces/event', (req, res) => {
    const event = req.body as TraceEvent;
    event.timestamp = Date.now();
    traceEvents.push(event);
    if (traceEvents.length > MAX_EVENTS) traceEvents.shift();
    res.json({ ok: true });
  });

  // API to get all events
  app.get('/_apx/traces/events', (_req, res) => {
    res.json(traceEvents);
  });

  // SQL proxy for the dashboard
  app.post('/_apx/traces/sql', async (req, res) => {
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
}

interface TraceEvent {
  type: 'round_start' | 'agent_call' | 'tool_call' | 'agent_response' | 'verdict' | 'round_end';
  round?: number;
  agent?: string;
  tool?: string;
  folio?: string;
  plant?: string;
  language?: string;
  cipher?: string;
  input?: string;
  output?: string;
  duration_ms?: number;
  grounding?: number;
  consistency?: number;
  verdict?: string;
  timestamp?: number;
}

// Expose for the theory loop to push events
export function createTraceEmitter(baseUrl: string) {
  return async function emit(event: Omit<TraceEvent, 'timestamp'>): Promise<void> {
    try {
      await fetch(baseUrl + '/_apx/traces/event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(event),
      });
    } catch {
      // Non-critical — don't crash the loop
    }
  };
}

const DASHBOARD_HTML = `<!DOCTYPE html>
<html>
<head>
  <title>Voynich Agent Traces</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; }

    .header { padding: 20px 24px; border-bottom: 1px solid #1a1a2e; display: flex; align-items: center; gap: 16px; }
    .header h1 { font-size: 20px; color: #fff; }
    .header .badge { background: #1a3e1a; color: #4caf50; padding: 4px 10px; border-radius: 12px; font-size: 12px; }

    .layout { display: grid; grid-template-columns: 300px 1fr; height: calc(100vh - 61px); }

    /* Left panel: Agent constellation */
    .agents-panel { border-right: 1px solid #1a1a2e; padding: 16px; overflow-y: auto; }
    .agents-panel h2 { font-size: 13px; text-transform: uppercase; color: #666; margin-bottom: 12px; }

    .agent-card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 12px; margin-bottom: 8px; transition: border-color 0.3s; }
    .agent-card.active { border-color: #4dd0e1; box-shadow: 0 0 12px rgba(77,208,225,0.15); }
    .agent-card .name { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
    .agent-card .role { font-size: 11px; color: #888; }
    .agent-card .status { font-size: 11px; margin-top: 6px; }
    .agent-card .status .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; }
    .agent-card .status .dot.green { background: #4caf50; }
    .agent-card .status .dot.gray { background: #555; }

    .stats { margin-top: 16px; }
    .stat { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #1a1a2e; font-size: 13px; }
    .stat .val { color: #4dd0e1; font-weight: 600; }

    /* Right panel: Trace timeline */
    .trace-panel { padding: 16px; overflow-y: auto; }
    .trace-panel h2 { font-size: 13px; text-transform: uppercase; color: #666; margin-bottom: 12px; }

    .trace-event { border-left: 2px solid #333; padding: 8px 0 8px 16px; margin-bottom: 2px; font-size: 13px; position: relative; }
    .trace-event::before { content: ''; position: absolute; left: -5px; top: 12px; width: 8px; height: 8px; border-radius: 50%; background: #333; }

    .trace-event.round_start { border-left-color: #7c5cbf; }
    .trace-event.round_start::before { background: #7c5cbf; }
    .trace-event.round_start .label { color: #b39ddb; font-weight: 600; }

    .trace-event.agent_call { border-left-color: #4dd0e1; }
    .trace-event.agent_call::before { background: #4dd0e1; }

    .trace-event.tool_call { border-left-color: #ffb74d; }
    .trace-event.tool_call::before { background: #ffb74d; }
    .trace-event.tool_call .label { color: #ffb74d; }

    .trace-event.agent_response { border-left-color: #81c784; }
    .trace-event.agent_response::before { background: #81c784; }

    .trace-event.verdict { border-left-color: #ef5350; }
    .trace-event.verdict::before { background: #ef5350; }
    .trace-event.verdict.plausible { border-left-color: #4caf50; }
    .trace-event.verdict.plausible::before { background: #4caf50; }

    .trace-event.round_end { border-left-color: #555; }
    .trace-event.round_end::before { background: #555; }

    .trace-event .label { font-size: 11px; text-transform: uppercase; color: #888; }
    .trace-event .content { margin-top: 4px; }
    .trace-event .data { font-family: monospace; font-size: 12px; color: #aaa; margin-top: 4px; background: #0f0f1f; padding: 6px 8px; border-radius: 4px; max-height: 60px; overflow: hidden; }
    .trace-event .time { font-size: 10px; color: #555; float: right; }
    .trace-event .score { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }
    .trace-event .score.high { background: #1a3e1a; color: #4caf50; }
    .trace-event .score.mid { background: #3e3a1a; color: #ffb74d; }
    .trace-event .score.low { background: #3e1a1a; color: #ef5350; }
  </style>
</head>
<body>
  <div class="header">
    <h1>Agent Orchestration Traces</h1>
    <span class="badge" id="status">Loading...</span>
  </div>

  <div class="layout">
    <div class="agents-panel">
      <h2>Agent Constellation</h2>
      <div class="agent-card" id="agent-orchestrator">
        <div class="name">Orchestrator</div>
        <div class="role">Theory loop coordinator</div>
        <div class="status"><span class="dot green"></span>Running</div>
      </div>
      <div class="agent-card" id="agent-proposer">
        <div class="name">Proposer (FMAPI)</div>
        <div class="role">Generates decoding theories per folio</div>
        <div class="status"><span class="dot gray"></span>Idle</div>
      </div>
      <div class="agent-card" id="agent-grounder">
        <div class="name">Grounder</div>
        <div class="role">Scores against folio images</div>
        <div class="status"><span class="dot gray"></span>Idle</div>
      </div>
      <div class="agent-card" id="agent-skeptic">
        <div class="name">Skeptic (FMAPI)</div>
        <div class="role">Cross-folio consistency check</div>
        <div class="status"><span class="dot gray"></span>Idle</div>
      </div>

      <div class="stats">
        <h2 style="margin-top:16px">Results</h2>
        <div class="stat"><span>Theories</span><span class="val" id="stat-total">0</span></div>
        <div class="stat"><span>Grounded</span><span class="val" id="stat-grounded">0</span></div>
        <div class="stat"><span>Best Grounding</span><span class="val" id="stat-best-grd">0</span></div>
        <div class="stat"><span>Best Consistency</span><span class="val" id="stat-best-cons">0</span></div>
        <div class="stat"><span>Plausible</span><span class="val" id="stat-plausible">0</span></div>
        <div class="stat"><span>Rejected</span><span class="val" id="stat-rejected">0</span></div>
      </div>
    </div>

    <div class="trace-panel" id="traces">
      <h2>Live Trace</h2>
    </div>
  </div>

  <script>
    async function sql(stmt) {
      const res = await fetch('/_apx/traces/sql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ statement: stmt })
      });
      const d = await res.json();
      const cols = (d.manifest?.schema?.columns || []).map(c => c.name);
      return (d.result?.data_array || []).map(row => {
        const obj = {};
        cols.forEach((c, i) => obj[c] = row[i]);
        return obj;
      });
    }

    async function loadStats() {
      try {
        const r = await sql(\`SELECT COUNT(*) total,
          COUNT(CASE WHEN grounding_score > 0 THEN 1 END) grounded,
          ROUND(MAX(grounding_score),3) best_grd,
          ROUND(MAX(consistency_score),3) best_cons,
          COUNT(CASE WHEN verdict = 'plausible' THEN 1 END) plausible,
          COUNT(CASE WHEN verdict = 'rejected' THEN 1 END) rejected
          FROM serverless_stable_qh44kx_catalog.voynich.theories\`);
        if (r[0]) {
          document.getElementById('stat-total').textContent = r[0].total;
          document.getElementById('stat-grounded').textContent = r[0].grounded;
          document.getElementById('stat-best-grd').textContent = r[0].best_grd;
          document.getElementById('stat-best-cons').textContent = r[0].best_cons;
          document.getElementById('stat-plausible').textContent = r[0].plausible;
          document.getElementById('stat-rejected').textContent = r[0].rejected;
        }
      } catch(e) { console.error(e); }
    }

    async function loadTraces() {
      try {
        const r = await sql(\`SELECT target_folio, target_plant, source_language,
          ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons,
          verdict, SUBSTRING(decoded_text, 1, 80) decoded, proposed_at
          FROM serverless_stable_qh44kx_catalog.voynich.theories
          ORDER BY proposed_at DESC LIMIT 30\`);

        const panel = document.getElementById('traces');
        let html = '<h2>Theory Timeline (latest first)</h2>';

        for (const t of r) {
          const grdClass = parseFloat(t.grd) > 0.1 ? 'high' : parseFloat(t.grd) > 0 ? 'mid' : 'low';
          html += \`
            <div class="trace-event round_start">
              <span class="label">Theory</span>
              <span class="time">\${t.proposed_at || ''}</span>
              <div class="content">
                <strong>\${t.target_folio}</strong> — \${t.target_plant} [\${t.source_language}]
              </div>
            </div>
            <div class="trace-event agent_call">
              <span class="label">Proposer → FMAPI</span>
              <div class="content">Generate decoding for \${t.target_plant}</div>
            </div>
            <div class="trace-event agent_response">
              <span class="label">Decoded Text</span>
              <div class="data">\${t.decoded}</div>
            </div>
            <div class="trace-event tool_call">
              <span class="label">Grounder → score_image_grounding</span>
              <div class="content">Grounding: <span class="score \${grdClass}">\${t.grd}</span></div>
            </div>
            <div class="trace-event tool_call">
              <span class="label">Cross-Folio Test</span>
              <div class="content">Consistency: <span class="score \${parseFloat(t.cons) > 0 ? 'high' : 'low'}">\${t.cons}</span></div>
            </div>
            <div class="trace-event verdict \${t.verdict}">
              <span class="label">Skeptic Verdict</span>
              <div class="content"><span class="score \${t.verdict === 'plausible' ? 'high' : 'low'}">\${t.verdict}</span></div>
            </div>
            <div class="trace-event round_end">
              <span class="label">Round Complete</span>
            </div>
          \`;
        }

        panel.innerHTML = html;
        document.getElementById('status').textContent = 'Live — ' + r.length + ' theories';
      } catch(e) { console.error(e); }
    }

    loadStats();
    loadTraces();
    setInterval(() => { loadStats(); loadTraces(); }, 15000);
  </script>
</body>
</html>`;

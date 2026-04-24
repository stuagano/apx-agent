/**
 * Agent Trace Dashboard
 *
 * Two views:
 * - /_apx/traces — list of all theories with links to detail view
 * - /_apx/traces/:id — single trace showing agent-to-agent conversation
 */

import type { Express } from 'express';

const THEORIES_TABLE = 'serverless_stable_qh44kx_catalog.voynich.theories';

async function querySql(statement: string): Promise<Array<Record<string, string>>> {
  const { resolveToken: rt, resolveHost: rh } = await import('./appkit-agent/index.mjs');
  const tk = await rt();
  const h = rh();
  const res = await fetch(h + '/api/2.0/sql/statements', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tk },
    body: JSON.stringify({
      warehouse_id: process.env.DATABRICKS_WAREHOUSE_ID,
      statement,
      wait_timeout: '30s',
    }),
  });
  const data = (await res.json()) as {
    result?: { data_array?: string[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
  };
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string> = {};
    cols.forEach((col, i) => { obj[col] = row[i]; });
    return obj;
  });
}

export function mountTraceDashboard(app: Express): void {

  // ---------------------------------------------------------------------------
  // List view: all theories
  // ---------------------------------------------------------------------------
  app.get('/_apx/traces', async (_req, res) => {
    try {
      const theories = await querySql(
        `SELECT id, target_folio, target_plant, source_language,
                ROUND(grounding_score,3) grd, ROUND(consistency_score,3) cons,
                verdict, SUBSTRING(decoded_text, 1, 50) decoded, proposed_at
         FROM ${THEORIES_TABLE} ORDER BY proposed_at DESC LIMIT 50`
      );

      const stats = await querySql(
        `SELECT COUNT(*) total,
                COUNT(CASE WHEN grounding_score > 0 THEN 1 END) grounded,
                ROUND(MAX(grounding_score),3) best_grd,
                ROUND(MAX(consistency_score),3) best_cons
         FROM ${THEORIES_TABLE}`
      );
      const s = stats[0] || { total: '0', grounded: '0', best_grd: '0', best_cons: '0' };

      const rows = theories.map((t) => {
        const grdPct = Math.round(parseFloat(t.grd || '0') * 100);
        const verdictClass = t.verdict === 'plausible' ? 'plausible' : t.verdict === 'weak' ? 'weak' : 'rejected';
        return `<a href="/_apx/traces/${t.id}" class="trace-row">
          <div class="folio">${t.target_folio}</div>
          <div class="plant">${(t.target_plant || '').slice(0, 25)}</div>
          <div class="lang">${t.source_language}</div>
          <div class="bar-cell"><div class="bar"><div class="bar-fill" style="width:${grdPct}%"></div></div><span>${t.grd}</span></div>
          <div class="verdict"><span class="tag ${verdictClass}">${t.verdict}</span></div>
          <div class="decoded">${(t.decoded || '').slice(0, 40)}</div>
        </a>`;
      }).join('');

      res.setHeader('Content-Type', 'text/html');
      res.send(LIST_HTML(s, rows, theories.length));
    } catch (err) {
      res.status(500).send('Error: ' + String(err));
    }
  });

  // ---------------------------------------------------------------------------
  // Detail view: single trace conversation
  // ---------------------------------------------------------------------------
  app.get('/_apx/traces/:id', async (req, res) => {
    try {
      const id = req.params.id.replace(/[^a-zA-Z0-9_-]/g, '');
      const results = await querySql(
        `SELECT * FROM ${THEORIES_TABLE} WHERE id = '${id}' LIMIT 1`
      );

      if (results.length === 0) {
        res.status(404).send('Theory not found');
        return;
      }

      const t = results[0];
      const crossFolio = JSON.parse(t.cross_folio_results || '[]');
      const symbolMap = JSON.parse(t.symbol_map || '{}');

      res.setHeader('Content-Type', 'text/html');
      res.send(DETAIL_HTML(t, crossFolio, symbolMap));
    } catch (err) {
      res.status(500).send('Error: ' + String(err));
    }
  });
}

// ---------------------------------------------------------------------------
// HTML Templates
// ---------------------------------------------------------------------------

function LIST_HTML(stats: Record<string, string>, rows: string, count: number): string {
  return `<!DOCTYPE html>
<html>
<head>
  <title>Agent Traces</title>
  <meta http-equiv="refresh" content="20">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 22px; color: #fff; margin-bottom: 4px; }
    .sub { color: #666; font-size: 13px; margin-bottom: 20px; }
    .cards { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
    .card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 14px 18px; min-width: 140px; }
    .card .label { font-size: 10px; text-transform: uppercase; color: #666; }
    .card .val { font-size: 24px; font-weight: 600; color: #4dd0e1; margin-top: 2px; }
    .trace-row { display: grid; grid-template-columns: 50px 180px 60px 120px 80px 1fr; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid #1a1a2e; text-decoration: none; color: #e0e0e0; transition: background 0.15s; }
    .trace-row:hover { background: #1a1a2e; }
    .folio { font-weight: 600; color: #4dd0e1; }
    .plant { font-size: 13px; }
    .lang { font-size: 12px; color: #888; }
    .bar-cell { display: flex; align-items: center; gap: 6px; font-size: 12px; }
    .bar { width: 60px; height: 5px; background: #252540; border-radius: 3px; }
    .bar-fill { height: 100%; background: #4dd0e1; border-radius: 3px; }
    .tag { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
    .tag.rejected { background: #2a1515; color: #ef5350; }
    .tag.plausible { background: #152a15; color: #4caf50; }
    .tag.weak { background: #2a2815; color: #ffb74d; }
    .decoded { font-family: monospace; font-size: 12px; color: #888; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .header-row { display: grid; grid-template-columns: 50px 180px 60px 120px 80px 1fr; gap: 8px; padding: 8px 12px; font-size: 10px; text-transform: uppercase; color: #555; border-bottom: 1px solid #252540; }
  </style>
</head>
<body>
  <h1>Agent Orchestration Traces</h1>
  <p class="sub">${count} theories tested across 38 herbal folios. Click a row to see the full agent conversation.</p>

  <div class="cards">
    <div class="card"><div class="label">Theories</div><div class="val">${stats.total}</div></div>
    <div class="card"><div class="label">Grounded</div><div class="val">${stats.grounded}</div></div>
    <div class="card"><div class="label">Best Grounding</div><div class="val">${stats.best_grd}</div></div>
    <div class="card"><div class="label">Best Consistency</div><div class="val">${stats.best_cons}</div></div>
  </div>

  <div class="header-row">
    <span>Folio</span><span>Plant</span><span>Lang</span><span>Grounding</span><span>Verdict</span><span>Decoded</span>
  </div>
  ${rows}
</body>
</html>`;
}

function DETAIL_HTML(
  t: Record<string, string>,
  crossFolio: Array<{ folio_id: string; plant_expected: string; decoded_text: string; grounding_score: number }>,
  symbolMap: Record<string, string>,
): string {
  const mapEntries = Object.entries(symbolMap).slice(0, 20)
    .map(([k, v]) => `<span class="map-entry"><span class="eva">${k}</span> → <span class="plain">${v}</span></span>`)
    .join('');

  const crossRows = crossFolio.map((cf) => {
    const score = (cf.grounding_score || 0).toFixed(3);
    const cls = parseFloat(score) > 0 ? 'match' : 'miss';
    return `<div class="cross-row ${cls}">
      <span class="cf-folio">${cf.folio_id}</span>
      <span class="cf-plant">${cf.plant_expected}</span>
      <span class="cf-score">${score}</span>
      <span class="cf-decoded">${(cf.decoded_text || '').slice(0, 40)}</span>
    </div>`;
  }).join('');

  const verdictClass = t.verdict === 'plausible' ? 'plausible' : t.verdict === 'weak' ? 'weak' : 'rejected';
  const grd = parseFloat(t.grounding_score || '0');
  const cons = parseFloat(t.consistency_score || '0');

  return `<!DOCTYPE html>
<html>
<head>
  <title>Trace: ${t.target_folio} — ${(t.target_plant || '').slice(0, 20)}</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a1a; color: #e0e0e0; padding: 24px; max-width: 800px; margin: 0 auto; }
    a { color: #4dd0e1; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .back { font-size: 13px; margin-bottom: 16px; display: block; }
    h1 { font-size: 20px; color: #fff; margin-bottom: 4px; }
    .sub { color: #666; font-size: 13px; margin-bottom: 24px; }

    .msg { margin-bottom: 16px; padding: 16px; border-radius: 10px; }
    .msg .sender { font-size: 11px; font-weight: 600; text-transform: uppercase; margin-bottom: 6px; letter-spacing: 0.5px; }
    .msg .body { font-size: 14px; line-height: 1.6; }
    .msg .body code { background: #0f0f1f; padding: 2px 6px; border-radius: 3px; font-size: 13px; }

    .msg.orchestrator { background: #1a1a30; border: 1px solid #2a2a4e; }
    .msg.orchestrator .sender { color: #7c5cbf; }

    .msg.proposer { background: #1a2a2a; border: 1px solid #2a4a4a; }
    .msg.proposer .sender { color: #4dd0e1; }

    .msg.grounder { background: #1a2a1a; border: 1px solid #2a4a2a; }
    .msg.grounder .sender { color: #81c784; }

    .msg.skeptic { background: #2a1a1a; border: 1px solid #4a2a2a; }
    .msg.skeptic .sender { color: #ef5350; }

    .msg.result { background: #1a1a2e; border: 1px solid #333; }
    .msg.result .sender { color: #ffb74d; }

    .decoded-block { font-family: 'Georgia', serif; font-size: 15px; line-height: 1.8; color: #c8b89a; background: #12100e; border: 1px solid #2a2520; padding: 16px; border-radius: 6px; margin: 8px 0; font-style: italic; }

    .symbol-map { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
    .map-entry { background: #0f0f1f; padding: 3px 8px; border-radius: 4px; font-family: monospace; font-size: 12px; }
    .eva { color: #4dd0e1; }
    .plain { color: #ffb74d; }

    .cross-results { margin: 8px 0; }
    .cross-row { display: grid; grid-template-columns: 50px 160px 60px 1fr; gap: 8px; padding: 6px 8px; font-size: 13px; border-bottom: 1px solid #1a1a2e; }
    .cross-row.miss { opacity: 0.6; }
    .cross-row.match { background: #0f1f0f; }
    .cf-folio { font-weight: 600; color: #4dd0e1; }
    .cf-plant { color: #aaa; }
    .cf-score { font-family: monospace; }
    .cf-decoded { font-family: monospace; font-size: 11px; color: #666; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .score-big { font-size: 32px; font-weight: 700; }
    .score-big.high { color: #4caf50; }
    .score-big.mid { color: #ffb74d; }
    .score-big.low { color: #ef5350; }
    .scores { display: flex; gap: 32px; margin: 8px 0; }
    .score-item .label { font-size: 10px; text-transform: uppercase; color: #666; }

    .tag { padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight: 600; }
    .tag.rejected { background: #2a1515; color: #ef5350; }
    .tag.plausible { background: #152a15; color: #4caf50; }
    .tag.weak { background: #2a2815; color: #ffb74d; }
  </style>
</head>
<body>
  <a href="/_apx/traces" class="back">← All traces</a>
  <h1>${t.target_folio} — ${t.target_plant}</h1>
  <p class="sub">${t.source_language} · ${t.proposed_at || ''}</p>

  <!-- Step 1: Orchestrator assigns the task -->
  <div class="msg orchestrator">
    <div class="sender">Orchestrator</div>
    <div class="body">
      Analyze folio <code>${t.target_folio}</code>. The image depicts <strong>${t.target_plant}</strong>.
      Propose a ${t.source_language} decoding using a ${t.cipher_type || 'substitution'} cipher.
    </div>
  </div>

  <!-- Step 2: Proposer generates theory -->
  <div class="msg proposer">
    <div class="sender">Proposer → FMAPI (Claude Sonnet 4.6)</div>
    <div class="body">
      Here is my proposed symbol map:
      <div class="symbol-map">${mapEntries}</div>
      Decoded text:
      <div class="decoded-block">${t.decoded_text || ''}</div>
    </div>
  </div>

  <!-- Step 3: Grounder scores against folio images -->
  <div class="msg grounder">
    <div class="sender">Grounder → score_image_grounding</div>
    <div class="body">
      Scored decoded text against 38 herbal folio vision analyses.
      <div class="scores">
        <div class="score-item">
          <div class="label">Grounding</div>
          <div class="score-big ${grd > 0.1 ? 'high' : grd > 0 ? 'mid' : 'low'}">${grd.toFixed(3)}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Step 4: Cross-folio consistency test -->
  <div class="msg orchestrator">
    <div class="sender">Orchestrator → Cross-Folio Test</div>
    <div class="body">
      Applied the same symbol map to ${crossFolio.length} other folios:
      <div class="cross-results">
        <div class="cross-row" style="font-size:10px;text-transform:uppercase;color:#555;">
          <span>Folio</span><span>Expected Plant</span><span>Score</span><span>Decoded</span>
        </div>
        ${crossRows || '<div style="color:#555;padding:8px;">No cross-folio data</div>'}
      </div>
      <div class="scores" style="margin-top:12px">
        <div class="score-item">
          <div class="label">Consistency</div>
          <div class="score-big ${cons > 0 ? 'high' : 'low'}">${cons.toFixed(3)}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Step 5: Skeptic challenges -->
  <div class="msg skeptic">
    <div class="sender">Skeptic → FMAPI (Claude Sonnet 4.6)</div>
    <div class="body">
      <span class="tag ${verdictClass}">${t.verdict || 'unknown'}</span>
      <p style="margin-top:8px">The cross-folio test results ${cons > 0 ? 'show partial consistency' : 'constitute a fatal falsification'} of this theory.
      ${cons === 0 ? 'The same symbol map produces gibberish on every other folio tested.' : ''}</p>
    </div>
  </div>

  <!-- Final result -->
  <div class="msg result">
    <div class="sender">Result</div>
    <div class="body">
      <div class="scores">
        <div class="score-item">
          <div class="label">Grounding</div>
          <div class="score-big ${grd > 0.1 ? 'high' : grd > 0 ? 'mid' : 'low'}">${grd.toFixed(3)}</div>
        </div>
        <div class="score-item">
          <div class="label">Consistency</div>
          <div class="score-big ${cons > 0 ? 'high' : 'low'}">${cons.toFixed(3)}</div>
        </div>
        <div class="score-item">
          <div class="label">Verdict</div>
          <div class="score-big ${verdictClass === 'plausible' ? 'high' : verdictClass === 'weak' ? 'mid' : 'low'}">${t.verdict || '?'}</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>`;
}

/**
 * One-off backtest: run every historical theory in voynich.theories through
 * the new critic checks (find_contradictions + score_latin_likelihood +
 * null_baseline_test, with llm_judge gated on the heuristic threshold), and
 * write the structured verdict back to the same row.
 *
 * Answers: "given the new critic has teeth, is there ANY existing theory
 * sitting in the table that survives?"
 *
 * Run from the orchestrator deploy directory:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   npx tsx backtest-critic.ts
 *
 * Resumable: skips rows where critic_likelihood IS NOT NULL. Re-run anytime.
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const TABLE = 'serverless_stable_qh44kx_catalog.voynich.theories';
const CRITIC_URL = process.env.CRITIC_AGENT_URL;
const CONCURRENCY = 3;             // rows in flight at once
const HEURISTIC_LIKELIHOOD_GATE = 0.3;
const PROGRESS_EVERY = 50;

if (!CRITIC_URL) {
  console.error('CRITIC_AGENT_URL env required');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// SQL
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');

  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Critic tool calls — same shape as theory-loop.ts:callCriticTool but no
// trace propagation (this is a backfill, no parent trace to link to).
// ---------------------------------------------------------------------------

async function callCriticTool(toolName: string, params: Record<string, unknown>): Promise<unknown | null> {
  const url = `${CRITIC_URL!.replace(/\/$/, '')}/api/agent/tools/${toolName}`;
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    try { headers.Authorization = `Bearer ${await resolveToken()}`; } catch { /* may be open */ }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60_000);
    let res: globalThis.Response;
    try {
      res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(params), signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      console.warn(`[critic] ${toolName} http ${res.status}: ${(await res.text()).slice(0, 120)}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.warn(`[critic] ${toolName} threw: ${(err as Error).message}`);
    return null;
  }
}

interface CriticVerdict {
  adversarial?: number;
  likelihood?: number;
  null_distinguishable?: boolean;
  judge_verdict?: 'PASS' | 'FAIL' | 'SKIPPED';
  composite_verdict: 'plausible' | 'weak' | 'rejected' | 'unknown';
}

async function critique(decoded_text: string, source_language: string): Promise<CriticVerdict> {
  const [c, l, n] = await Promise.all([
    callCriticTool('find_contradictions', { decoded_text, section: 'herbal' }),
    callCriticTool('score_latin_likelihood', { decoded_text, source_language }),
    callCriticTool('null_baseline_test', { decoded_text, source_language }),
  ]);
  const contradictions = c as { adversarial?: number } | null;
  const likelihood = l as { likelihood?: number } | null;
  const nullTest = n as { distinguishable?: boolean } | null;

  const v: CriticVerdict = {
    adversarial: contradictions?.adversarial,
    likelihood: likelihood?.likelihood,
    null_distinguishable: nullTest?.distinguishable,
    composite_verdict: 'unknown',
  };

  const promising =
    (v.likelihood ?? 0) >= HEURISTIC_LIKELIHOOD_GATE &&
    v.null_distinguishable === true;

  if (promising) {
    const j = (await callCriticTool('llm_judge', { decoded_text, source_language })) as
      | { verdict?: 'PASS' | 'FAIL' }
      | null;
    v.judge_verdict = j?.verdict ?? 'FAIL';
  } else {
    v.judge_verdict = 'SKIPPED';
  }

  if (
    (v.adversarial !== undefined && v.adversarial < 0.5) ||
    v.null_distinguishable === false ||
    v.judge_verdict === 'FAIL'
  ) v.composite_verdict = 'rejected';
  else if (v.judge_verdict === 'PASS') v.composite_verdict = 'plausible';
  else if (promising) v.composite_verdict = 'weak';
  else v.composite_verdict = 'rejected';

  return v;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

interface Row {
  id: string;
  decoded_text: string;
  source_language: string;
}

async function processOne(row: Row): Promise<{ id: string; v: CriticVerdict; error?: string }> {
  try {
    const v = await critique(row.decoded_text, row.source_language);
    if (v.composite_verdict === 'unknown') {
      return { id: row.id, v, error: 'all critic signals null' };
    }
    const escId = row.id.replace(/'/g, "''");
    const lik = v.likelihood !== undefined ? String(v.likelihood) : 'NULL';
    const adv = v.adversarial !== undefined ? String(v.adversarial) : 'NULL';
    const nd = v.null_distinguishable !== undefined ? String(v.null_distinguishable) : 'NULL';
    const jv = v.judge_verdict ? `'${v.judge_verdict}'` : 'NULL';
    const cv = `'${v.composite_verdict}'`;
    await executeSql(`
      UPDATE ${TABLE}
      SET critic_likelihood = ${lik},
          critic_adversarial = ${adv},
          critic_null_distinguishable = ${nd},
          critic_judge_verdict = ${jv},
          verdict = ${cv}
      WHERE id = '${escId}'
    `);
    return { id: row.id, v };
  } catch (err) {
    return { id: row.id, v: { composite_verdict: 'unknown' }, error: (err as Error).message };
  }
}

async function processBatch(rows: Row[]): Promise<Array<{ id: string; v: CriticVerdict; error?: string }>> {
  return Promise.all(rows.map(processOne));
}

async function main(): Promise<void> {
  console.log(`[backtest] loading rows from ${TABLE} where critic_likelihood IS NULL...`);
  const rows = (await executeSql(`
    SELECT id, decoded_text, source_language
    FROM ${TABLE}
    WHERE critic_likelihood IS NULL
      AND decoded_text IS NOT NULL
      AND LENGTH(decoded_text) >= 20
    ORDER BY proposed_at
  `)) as unknown as Row[];

  console.log(`[backtest] ${rows.length} rows to process at concurrency=${CONCURRENCY}`);

  // Stats accumulators
  const stats = {
    processed: 0,
    rejected: 0,
    weak: 0,
    plausible: 0,
    unknown: 0,
    judgePass: 0,
    judgeFail: 0,
    judgeSkipped: 0,
    errors: 0,
    survivors: [] as Array<{ id: string; v: CriticVerdict }>,
  };

  const start = Date.now();
  for (let i = 0; i < rows.length; i += CONCURRENCY) {
    const batch = rows.slice(i, i + CONCURRENCY);
    const results = await processBatch(batch);
    for (const r of results) {
      stats.processed++;
      if (r.error) stats.errors++;
      const cv = r.v.composite_verdict;
      if (cv === 'rejected') stats.rejected++;
      else if (cv === 'weak') stats.weak++;
      else if (cv === 'plausible') stats.plausible++;
      else stats.unknown++;

      if (r.v.judge_verdict === 'PASS') stats.judgePass++;
      else if (r.v.judge_verdict === 'FAIL') stats.judgeFail++;
      else if (r.v.judge_verdict === 'SKIPPED') stats.judgeSkipped++;

      if (cv === 'plausible' || cv === 'weak') {
        stats.survivors.push({ id: r.id, v: r.v });
      }
    }

    if (stats.processed % PROGRESS_EVERY === 0 || stats.processed === rows.length) {
      const elapsed = (Date.now() - start) / 1000;
      const rate = stats.processed / elapsed;
      const eta = (rows.length - stats.processed) / Math.max(rate, 0.01);
      console.log(`[backtest] ${stats.processed}/${rows.length} rate=${rate.toFixed(1)}/s eta=${eta.toFixed(0)}s rej=${stats.rejected} weak=${stats.weak} plaus=${stats.plausible} judge[P/F/S]=${stats.judgePass}/${stats.judgeFail}/${stats.judgeSkipped} err=${stats.errors}`);
    }
  }

  // Final report
  console.log('');
  console.log('=== Backtest summary ===');
  console.log(`processed: ${stats.processed}`);
  console.log(`composite verdict:`);
  console.log(`  rejected:  ${stats.rejected}`);
  console.log(`  weak:      ${stats.weak}`);
  console.log(`  plausible: ${stats.plausible}`);
  console.log(`  unknown:   ${stats.unknown}`);
  console.log(`judge:`);
  console.log(`  PASS:    ${stats.judgePass}`);
  console.log(`  FAIL:    ${stats.judgeFail}`);
  console.log(`  SKIPPED: ${stats.judgeSkipped}`);
  console.log(`errors: ${stats.errors}`);
  console.log('');

  if (stats.survivors.length === 0) {
    console.log('No survivors. The pipeline has not produced anything credible yet.');
    console.log('Focus next iteration on the decoder side (mutation strategies, seed diversity).');
  } else {
    console.log(`SURVIVORS (composite_verdict in {plausible, weak}): ${stats.survivors.length}`);
    const sorted = stats.survivors.sort((a, b) => (b.v.likelihood ?? 0) - (a.v.likelihood ?? 0));
    for (const s of sorted.slice(0, 20)) {
      console.log(`  ${s.id}  lik=${(s.v.likelihood ?? 0).toFixed(3)} adv=${(s.v.adversarial ?? 0).toFixed(2)} null=${s.v.null_distinguishable} judge=${s.v.judge_verdict} verdict=${s.v.composite_verdict}`);
    }
    if (sorted.length > 20) console.log(`  ... (${sorted.length - 20} more)`);
  }
}

main().catch((err) => {
  console.error('[backtest] fatal:', err);
  process.exit(1);
});

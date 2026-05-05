/**
 * One-off: rerun phase 2 of the transposition comparison with a fresh token.
 *
 * The original cmp-trans-1777573872157 run had Phase 2 corrupted by token
 * expiration mid-run (Round 6 onward got http 401 from the critic and
 * SQL "Invalid Token" on persist). Phase 1 (N=1) is clean. This script
 * runs a fresh Phase 2 (N=5) under a new batch_label, then summarizes
 * against the original Phase 1 + this fresh Phase 2.
 *
 * Run from this dir:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   npx tsx rerun-transposition-phase2.ts
 *
 * Cost: 20 rounds at N=5, ~13 min wallclock. Within typical token lifetime
 * if the token is fresh at start.
 */

import { runTheoryLoop, type Strategy } from './theory-loop.js';
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const TABLE = 'serverless_stable_qh44kx_catalog.voynich.theories';

const STRATEGY: Strategy = {
  language: 'latin',
  cipherType: 'transposition',
  seedMode: 'cold',
};

const PHASE1_LABEL_ORIGINAL = 'cmp-trans-1777573872157/N1';
const PHASE2_LABEL_NEW = `cmp-trans-rerun-${Date.now()}/N5`;

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

async function main(): Promise<void> {
  console.log(`rerun-transposition-phase2`);
  console.log(`new phase 2 label: ${PHASE2_LABEL_NEW}`);
  console.log(`will summarize against original phase 1: ${PHASE1_LABEL_ORIGINAL}`);
  console.log('');

  process.env.N_RESTARTS = '5';

  const start = Date.now();
  console.log(`=== Phase 2 (rerun): ${PHASE2_LABEL_NEW} ===`);
  console.log(`    N_RESTARTS=5 strategy=${STRATEGY.language}|${STRATEGY.cipherType}|${STRATEGY.seedMode}`);
  await runTheoryLoop(1, 1, PHASE2_LABEL_NEW, STRATEGY);
  const elapsed = ((Date.now() - start) / 1000).toFixed(1);
  console.log(`=== Phase 2 done in ${elapsed}s ===`);

  // Summary across original phase 1 + this fresh phase 2.
  const labels = [PHASE1_LABEL_ORIGINAL, PHASE2_LABEL_NEW]
    .map((l) => `'${l.replace(/'/g, "''")}'`)
    .join(', ');
  const rows = await executeSql(`
    SELECT batch_label,
           COUNT(*) AS theories,
           SUM(CASE WHEN critic_judge_verdict = 'PASS' THEN 1 ELSE 0 END) AS judge_pass,
           SUM(CASE WHEN critic_judge_verdict = 'FAIL' THEN 1 ELSE 0 END) AS judge_fail,
           SUM(CASE WHEN critic_judge_verdict = 'SKIPPED' THEN 1 ELSE 0 END) AS judge_skipped,
           SUM(CASE WHEN verdict = 'plausible' THEN 1 ELSE 0 END) AS plausible,
           SUM(CASE WHEN verdict = 'weak' THEN 1 ELSE 0 END) AS weak,
           ROUND(AVG(critic_likelihood), 3) AS avg_lik,
           ROUND(AVG(grounding_score + consistency_score), 3) AS avg_combined
    FROM ${TABLE}
    WHERE batch_label IN (${labels})
    GROUP BY batch_label
    ORDER BY batch_label
  `);

  console.log('');
  console.log('=== Comparison summary (transposition, clean) ===');
  console.log(
    'phase'.padEnd(48) + ' ' +
    ['n','PASS','FAIL','SKIP','plaus','weak'].map((h) => h.padStart(5)).join(' ') + ' ' +
    ['avg_lik','avg_comb'].map((h) => h.padStart(8)).join(' ')
  );
  console.log('-'.repeat(48) + ' ' + '-'.repeat(35) + ' ' + '-'.repeat(17));

  const byLabel = new Map<string, Record<string, string | null>>();
  for (const r of rows) byLabel.set(r.batch_label ?? '', r);

  for (const label of [PHASE1_LABEL_ORIGINAL, PHASE2_LABEL_NEW]) {
    const r = byLabel.get(label);
    if (!r) {
      console.log(label.padEnd(48) + '  (no rows persisted)');
      continue;
    }
    const cells = [
      (r.theories ?? '0').toString().padStart(5),
      (r.judge_pass ?? '0').toString().padStart(5),
      (r.judge_fail ?? '0').toString().padStart(5),
      (r.judge_skipped ?? '0').toString().padStart(5),
      (r.plausible ?? '0').toString().padStart(5),
      (r.weak ?? '0').toString().padStart(5),
      (r.avg_lik ?? 'NULL').toString().padStart(8),
      (r.avg_combined ?? 'NULL').toString().padStart(8),
    ];
    console.log(label.padEnd(48) + ' ' + cells.join(' '));
  }

  console.log('');
  console.log('Compare against substitution baseline (cmp-1777568282885): 0 PASS across all 60 rounds.');
}

main().catch((err) => {
  console.error('[rerun] fatal:', err);
  process.exit(1);
});

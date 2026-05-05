/**
 * Compare two decoder configurations for the columnar-transposition cipher
 * type, against the latin|substitution baseline (cmp-1777568282885 — 0 PASS):
 *   1. transposition-N1   : N_RESTARTS=1
 *   2. transposition-N5   : N_RESTARTS=5
 *
 * Tokenization toggle (multi-glyph vs single-char) has no effect on
 * transposition — the decoder operates on individual letters, ignoring EVA's
 * multi-character glyph candidates. So no phase 3.
 *
 * Strategy is PINNED to latin|transposition|cold via runTheoryLoop's
 * strategyOverride argument. cold seedMode is the only meaningful one for
 * transposition (no substitution elite pool to seed from; proposeTransposition
 * always starts each K-sweep restart from a random permutation).
 *
 * Each phase tags its rows in voynich.theories with a unique batch_label so
 * the post-run summary is robust to overlapping production runs. Discriminator
 * is the judge PASS rate per phase.
 *
 * Decision rule:
 *   - Either phase shows >= 1 PASS → transposition family is interesting,
 *     dig in (more K values, larger SA budget, or per-folio K).
 *   - Both phases 0 PASS → the cipher *family* axis (substitution AND
 *     transposition) is exhausted within the existing harness. Strong
 *     evidence for option (C): pivot project to falsification-framework
 *     as deliverable.
 *
 * Run from this dir:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   npx tsx compare-transposition.ts
 *
 * Tunables:
 *   COMPARE_BURSTS=1   default; each burst = ROUNDS_PER_BURST (20) rounds.
 *
 * Cost (numBursts=1):
 *   40 rounds total across 2 phases. ~52 LLM calls (skeptic + gated judge).
 *   Per-round SA: 5 K values × 1500 SA steps = 7500 hill-climb iterations,
 *   ~5x cheaper than substitution's 8000-step positional SA. Phase 1 ~150k
 *   scorer evals, phase 2 ~750k.
 */

import { runTheoryLoop, type Strategy } from './theory-loop.js';
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const TABLE = 'serverless_stable_qh44kx_catalog.voynich.theories';
const NUM_BURSTS = parseInt(process.env.COMPARE_BURSTS ?? '1');
const RUN_ID = `cmp-trans-${Date.now()}`;

const PINNED_STRATEGY: Strategy = {
  language: 'latin',
  cipherType: 'transposition',
  seedMode: 'cold',
};

interface Phase {
  label: string;
  N_RESTARTS: string;
}

const PHASES: Phase[] = [
  { label: `${RUN_ID}/N1`, N_RESTARTS: '1' },
  { label: `${RUN_ID}/N5`, N_RESTARTS: '5' },
];

// ---------------------------------------------------------------------------
// SQL — same shape as backtest-critic.ts and compare-restart-tokenization.ts
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
// Phase runner
// ---------------------------------------------------------------------------

async function runPhase(phase: Phase, phaseIdx: number): Promise<void> {
  process.env.N_RESTARTS = phase.N_RESTARTS;
  // EVA_TOKENIZATION not relevant for transposition — leave whatever the
  // surrounding env had set.
  const start = Date.now();
  console.log('');
  console.log(`=== Phase ${phaseIdx + 1}/${PHASES.length}: ${phase.label} ===`);
  console.log(`    N_RESTARTS=${phase.N_RESTARTS} bursts=${NUM_BURSTS}`);
  console.log(`    pinned strategy: ${PINNED_STRATEGY.language}|${PINNED_STRATEGY.cipherType}|${PINNED_STRATEGY.seedMode}`);
  await runTheoryLoop(NUM_BURSTS, phaseIdx, phase.label, PINNED_STRATEGY);
  const elapsed = ((Date.now() - start) / 1000).toFixed(1);
  console.log(`=== Phase ${phaseIdx + 1} done in ${elapsed}s ===`);
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

async function summarize(): Promise<void> {
  const labels = PHASES.map((p) => `'${p.label.replace(/'/g, "''")}'`).join(', ');
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
  console.log('=== Comparison summary (transposition) ===');
  console.log(
    'phase'.padEnd(42) + ' ' +
    ['n','PASS','FAIL','SKIP','plaus','weak'].map((h) => h.padStart(5)).join(' ') + ' ' +
    ['avg_lik','avg_comb'].map((h) => h.padStart(8)).join(' ')
  );
  console.log('-'.repeat(42) + ' ' + '-'.repeat(35) + ' ' + '-'.repeat(17));

  const byLabel = new Map<string, Record<string, string | null>>();
  for (const r of rows) byLabel.set(r.batch_label ?? '', r);

  for (const phase of PHASES) {
    const r = byLabel.get(phase.label);
    if (!r) {
      console.log(phase.label.padEnd(42) + '  (no rows persisted — phase may have failed)');
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
    console.log(phase.label.padEnd(42) + ' ' + cells.join(' '));
  }

  console.log('');
  console.log('Discriminator: judge PASS count (and plausible verdicts).');
  console.log('Compare against substitution baseline (cmp-1777568282885): 0 PASS across N=1, N=5, single-char.');
  console.log('Either phase >= 1 PASS = transposition is interesting. Both 0 PASS = pivot to (C).');
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log(`compare-transposition`);
  console.log(`run id  : ${RUN_ID}`);
  console.log(`bursts  : ${NUM_BURSTS} per phase (each burst = 20 rounds)`);
  console.log(`rounds  : ${NUM_BURSTS * 20 * PHASES.length} total across ${PHASES.length} phases`);
  console.log(`labels  : ${PHASES.map((p) => p.label).join(', ')}`);

  for (let i = 0; i < PHASES.length; i++) {
    try {
      await runPhase(PHASES[i], i);
    } catch (err) {
      console.error(`Phase ${i + 1} (${PHASES[i].label}) failed:`, err);
    }
  }

  await summarize();
}

main().catch((err) => {
  console.error('[compare] fatal:', err);
  process.exit(1);
});

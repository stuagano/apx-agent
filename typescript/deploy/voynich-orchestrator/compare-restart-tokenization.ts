/**
 * Compare three decoder configurations on the same orchestrator/folios/critic:
 *   1. baseline-N1-multi   : N_RESTARTS=1, EVA_TOKENIZATION=multi-glyph (pre-bd75037 behavior)
 *   2. N5-multi            : N_RESTARTS=5, EVA_TOKENIZATION=multi-glyph (4ddd599 default)
 *   3. N5-single           : N_RESTARTS=5, EVA_TOKENIZATION=single-char (alternative hypothesis class)
 *
 * Each phase tags its rows in voynich.theories with a unique batch_label so
 * the post-run summary is robust to overlapping production runs. Discriminator
 * is the judge PASS rate per phase — avg likelihood and avg combined score
 * are secondary signals (a "polished local optimum" can score well on both
 * without being real Latin).
 *
 * Run from this dir (same env as backtest-critic.ts plus MUTATION/CRITIC URLs
 * the orchestrator's module-init expects):
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   export MUTATION_AGENT_URL=...   # theory-loop's module init may import other agent URLs
 *   npx tsx compare-restart-tokenization.ts
 *
 * Tunables:
 *   COMPARE_BURSTS=1   default; each burst = ROUNDS_PER_BURST (20) rounds.
 *                      With 1 burst per phase, each phase uses ONE strategy
 *                      for 20 rounds; phases may pick different strategies
 *                      from pickNextStrategy(). Bump to 2-3 if you want
 *                      strategy diversity within each phase.
 *
 * Cost (numBursts=1):
 *   60 rounds total. ~78 LLM calls (skeptic + gated judge).
 *   Phase-1 ~40k scorer evals, phases 2 and 3 ~200k each.
 *
 * Notes / caveats:
 *   - Tokenization toggle only affects substitution-family ciphers; the other
 *     5 families (verbose/positional/homophonic/...) have their own code paths
 *     and won't differ between phase 2 and phase 3.
 *   - Multi-restart for non-substitution ciphers reruns the corresponding
 *     proposeXxxTheory function N times, which is the same independence
 *     assumption (each starts from random seeds), but the bimodal calibration
 *     was measured on substitution. Generalization is plausible but not
 *     measured.
 */

import { runTheoryLoop } from './theory-loop.js';
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const TABLE = 'serverless_stable_qh44kx_catalog.voynich.theories';
const NUM_BURSTS = parseInt(process.env.COMPARE_BURSTS ?? '1');
const RUN_ID = `cmp-${Date.now()}`;

interface Phase {
  label: string;
  N_RESTARTS: string;
  EVA_TOKENIZATION: 'multi-glyph' | 'single-char';
}

const PHASES: Phase[] = [
  { label: `${RUN_ID}/baseline-N1-multi`, N_RESTARTS: '1', EVA_TOKENIZATION: 'multi-glyph' },
  { label: `${RUN_ID}/N5-multi`,          N_RESTARTS: '5', EVA_TOKENIZATION: 'multi-glyph' },
  { label: `${RUN_ID}/N5-single`,         N_RESTARTS: '5', EVA_TOKENIZATION: 'single-char' },
];

// ---------------------------------------------------------------------------
// SQL — same shape as backtest-critic.ts (no shared helper module yet)
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
  process.env.EVA_TOKENIZATION = phase.EVA_TOKENIZATION;
  const start = Date.now();
  console.log('');
  console.log(`=== Phase ${phaseIdx + 1}/${PHASES.length}: ${phase.label} ===`);
  console.log(`    N_RESTARTS=${phase.N_RESTARTS} EVA_TOKENIZATION=${phase.EVA_TOKENIZATION} bursts=${NUM_BURSTS}`);
  await runTheoryLoop(NUM_BURSTS, phaseIdx, phase.label);
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
  console.log('=== Comparison summary ===');
  const header = ['phase', 'n', 'PASS', 'FAIL', 'SKIP', 'plaus', 'weak', 'avg_lik', 'avg_comb'];
  console.log(
    header[0].padEnd(42) + ' ' +
    ['n','PASS','FAIL','SKIP','plaus','weak'].map((h) => h.padStart(5)).join(' ') + ' ' +
    ['avg_lik','avg_comb'].map((h) => h.padStart(8)).join(' ')
  );
  console.log('-'.repeat(42) + ' ' + '-----'.repeat(6).split('').join('').slice(0, 35) + ' ' + '-'.repeat(17));

  // Index returned rows by label so we can render in PHASES order even if SQL
  // sort doesn't match (lexicographic ≠ phase order).
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
  console.log('avg_lik / avg_comb are secondary — high values do NOT imply real Latin if PASS=0.');
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log(`compare-restart-tokenization`);
  console.log(`run id  : ${RUN_ID}`);
  console.log(`bursts  : ${NUM_BURSTS} per phase (each burst = 20 rounds)`);
  console.log(`rounds  : ${NUM_BURSTS * 20 * PHASES.length} total across ${PHASES.length} phases`);
  console.log(`labels  : ${PHASES.map((p) => p.label).join(', ')}`);

  for (let i = 0; i < PHASES.length; i++) {
    try {
      await runPhase(PHASES[i], i);
    } catch (err) {
      console.error(`Phase ${i + 1} (${PHASES[i].label}) failed:`, err);
      // Keep going — partial results are still useful for the surviving phases.
    }
  }

  await summarize();
}

main().catch((err) => {
  console.error('[compare] fatal:', err);
  process.exit(1);
});

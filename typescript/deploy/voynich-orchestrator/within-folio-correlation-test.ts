/**
 * Within-folio feature correlation test.
 *
 * Tests whether pairs of EVA morphological features are anti-correlated
 * WITHIN individual folios, independent of family assignment.
 *
 * The cross-family evidence shows e.g. solanaceae HIGH qo-prefix and LOW -chy.
 * A complementary question: within a single folio's text, do high-qo-prefix
 * passages co-occur with low-chy passages? This would suggest the features
 * compete for the same "slot" in EVA morphology.
 *
 * Test design:
 *   1. Within each folio, split text into 2 halves by position (first/second half)
 *   2. Compute feature A and feature B for each half
 *   3. Compute Pearson r(feature_A_half, feature_B_half) across all folios
 *   4. Permutation null: shuffle which half gets which text (within each folio)
 *      — this preserves within-folio word count and global word distribution
 *
 * Expected findings from cross-family anti-correlations:
 *   qo-prefix vs -chy: expected negative r (sol HIGH qo LOW chy)
 *   qo-prefix vs -dy:  expected positive r (sol HIGH qo HIGH dy) — co-occur
 *   -dy vs -chy:       expected negative r (sol HIGH dy LOW chy)
 *   ch-init vs qo-prefix: expected negative r (sol LOW ch HIGH qo)
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx within-folio-correlation-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, computeFeature } from './stat-types.ts';

const N_PERMS = parseInt(process.env.N_PERMS ?? '2000');

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

function pearsonR(xs: number[], ys: number[]): number {
  const n = xs.length;
  if (n < 3) return 0;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, dx2 = 0, dy2 = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx, dy = ys[i] - my;
    num += dx * dy; dx2 += dx * dx; dy2 += dy * dy;
  }
  const denom = Math.sqrt(dx2 * dy2);
  return denom === 0 ? 0 : num / denom;
}

function permutationP(
  folioHalves: Array<{ half1: string[]; half2: string[] }>,
  featureA: string,
  featureB: string,
  realR: number,
): number {
  let nAbove = 0;
  for (let i = 0; i < N_PERMS; i++) {
    // Shuffle which half is which for each folio independently
    const xs: number[] = [], ys: number[] = [];
    for (const { half1, half2 } of folioHalves) {
      const [h1, h2] = Math.random() < 0.5 ? [half1, half2] : [half2, half1];
      const vA1 = computeFeature(h1, featureA);
      const vA2 = computeFeature(h2, featureA);
      const vB1 = computeFeature(h1, featureB);
      const vB2 = computeFeature(h2, featureB);
      if (vA1 !== null && vA2 !== null && vB1 !== null && vB2 !== null) {
        // Within-folio contrast: diff for feature A vs diff for feature B
        xs.push(vA1 - vA2);
        ys.push(vB1 - vB2);
      }
    }
    const permR = pearsonR(xs, ys);
    if (Math.abs(permR) >= Math.abs(realR)) nAbove++;
  }
  return nAbove / N_PERMS;
}

const PAIRS: Array<{
  featureA: string;
  featureB: string;
  predictedDir: '+' | '-' | '?';
  rationale: string;
}> = [
  { featureA: 'qo-prefix rate', featureB: '-chy suffix rate', predictedDir: '-', rationale: 'sol HIGH qo, LOW -chy → expected anti-correlation' },
  { featureA: 'qo-prefix rate', featureB: '-dy suffix rate',  predictedDir: '+', rationale: 'sol HIGH qo, HIGH -dy → expected co-occurrence' },
  { featureA: '-dy suffix rate', featureB: '-chy suffix rate', predictedDir: '-', rationale: 'sol HIGH -dy, LOW -chy; plantago reversed → anti-correlation expected' },
  { featureA: 'ch-init rate',   featureB: 'qo-prefix rate',  predictedDir: '-', rationale: 'sol LOW ch, HIGH qo → expected anti-correlation' },
  { featureA: '-aiin suffix rate', featureB: 'qo-prefix rate', predictedDir: '-', rationale: 'poppy HIGH -aiin vs sol HIGH qo → possible anti-correlation' },
  { featureA: '-ain suffix rate', featureB: '-aiin suffix rate', predictedDir: '+', rationale: 'lily HIGH -ain, poppy HIGH -aiin — suffix family test (null expected if orthogonal)' },
  { featureA: 'short word rate', featureB: 'qo-prefix rate', predictedDir: '-', rationale: 'thistle HIGH short LOW qo; sol LOW short HIGH qo → anti-correlation' },
  { featureA: 'sh-init rate',   featureB: 'ch-init rate',    predictedDir: '?', rationale: 'null control — sh and ch initials may be structurally independent' },
];

async function main() {
  console.log(`Within-folio feature correlation test — ${N_PERMS} permutations\n`);

  console.log('Loading herbal EVA corpus...');
  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal' ORDER BY folio_id
  `);

  // Build half-split folios (first half / second half of word sequence)
  interface FolioHalves { folio_id: string; half1: string[]; half2: string[]; }
  const folioHalves: FolioHalves[] = [];
  for (const r of evaRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue; // need enough words per half for stable rates
    const mid = Math.floor(words.length / 2);
    folioHalves.push({ folio_id: r.folio_id, half1: words.slice(0, mid), half2: words.slice(mid) });
  }
  console.log(`Corpus: ${folioHalves.length} herbal folios with ≥20 words\n`);

  console.log('─'.repeat(80));
  console.log(`${'Feature A'.padEnd(22)} vs ${'Feature B'.padEnd(22)} | ${'r'.padStart(6)} | ${'p'.padStart(6)} | Pred | Verdict`);
  console.log('─'.repeat(80));

  for (const { featureA, featureB, predictedDir, rationale } of PAIRS) {
    // Compute within-folio contrast: (half1 - half2) for each feature
    const xs: number[] = [], ys: number[] = [];
    const validHalves: Array<{ half1: string[]; half2: string[] }> = [];

    for (const { half1, half2 } of folioHalves) {
      const vA1 = computeFeature(half1, featureA);
      const vA2 = computeFeature(half2, featureA);
      const vB1 = computeFeature(half1, featureB);
      const vB2 = computeFeature(half2, featureB);
      if (vA1 !== null && vA2 !== null && vB1 !== null && vB2 !== null) {
        xs.push(vA1 - vA2);
        ys.push(vB1 - vB2);
        validHalves.push({ half1, half2 });
      }
    }

    if (xs.length < 10) {
      console.log(`  SKIP: ${featureA} vs ${featureB} — too few valid folios (${xs.length})`);
      continue;
    }

    const r = pearsonR(xs, ys);
    const p = permutationP(validHalves, featureA, featureB, r);

    const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : 'ns';
    const dirMatch = predictedDir === '?' ? '~' : (predictedDir === '+' ? r > 0 : r < 0) ? (p < 0.05 ? '✓' : '~') : '✗';

    const rStr = (r >= 0 ? '+' : '') + r.toFixed(3);
    const pStr = p.toFixed(3);

    console.log(
      `${featureA.padEnd(22)} vs ${featureB.padEnd(22)} | ${rStr.padStart(6)} | ${pStr.padStart(6)} ${sig.padStart(3)} | ${predictedDir.padStart(4)} | dir:${dirMatch}  n=${xs.length}`
    );
    console.log(`  ↳ ${rationale}`);
  }

  console.log('─'.repeat(80));
  console.log('\nDone.');
}

main().catch(console.error);

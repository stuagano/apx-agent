/**
 * Folio-pair feature-distance test: within-family morphological distance vs. lexical distance.
 *
 * Question: within a single botanical family, do folios that are morphologically
 * similar (close in 15-feature EVA space) also share more vocabulary (higher Jaccard)?
 * If yes, the two independent signals — feature rates and word-type distributions —
 * co-vary at the folio level, not just at the family level. That rules out a
 * scenario where family effects are driven entirely by a shared quire/scribal hand
 * and the morphological + lexical signals accidentally correlate only at the macro level.
 *
 * Design (within-family only):
 *   For each botanical family with ≥ 5 folios, enumerate all within-family folio pairs.
 *   For each pair compute:
 *     - morphological distance: Euclidean distance in 15-dimensional feature space
 *     - lexical distance: 1 - Jaccard(vocab_a, vocab_b)
 *   Then test Spearman rank correlation between the two distances across all within-family pairs.
 *   Permutation test: shuffle feature vectors within the family 2000 times.
 *
 * Why within-family only:
 *   All-pairs design is confounded by family membership — same-family pairs are
 *   necessarily close on both axes, so any cross-family correlation is trivially
 *   explained by the family label itself. Within-family tests whether the signal
 *   persists at the folio level after removing the family-level effect.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx folio-pair-distance-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { computeFeature, extractEvaWords, classifyPlantFamily, shuffle, rankBiserial } from './stat-types.js';

// ---------------------------------------------------------------------------
// SQL helper (same pattern as family-distance-test.ts)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Jaccard
// ---------------------------------------------------------------------------

function jaccard(setA: Set<string>, setB: Set<string>): number {
  if (setA.size === 0 && setB.size === 0) return 1;
  let inter = 0;
  for (const w of setA) if (setB.has(w)) inter++;
  const union = setA.size + setB.size - inter;
  return union === 0 ? 0 : inter / union;
}

// ---------------------------------------------------------------------------
// Feature vector (15 features from stat-types.ts computeFeature)
// ---------------------------------------------------------------------------

const FEATURES = [
  'qo-prefix rate',
  '-chy suffix rate',
  '-dy suffix rate',
  'ch-init rate',
  'short word rate',
  'unique word ratio',
  'word entropy',
  'oq-prefix rate',
  '-ol suffix rate',
  '-ain suffix rate',
  '-aiin suffix rate',
  'sh-init rate',
  'ok-prefix rate',
  'mean word length',
  'long word rate',
];

function featureVector(words: string[]): number[] {
  return FEATURES.map((f) => computeFeature(words, f) ?? 0);
}

function euclideanDistance(a: number[], b: number[]): number {
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += (a[i] - b[i]) ** 2;
  return Math.sqrt(sum);
}

// ---------------------------------------------------------------------------
// Spearman rank correlation
// ---------------------------------------------------------------------------

function spearmanR(x: number[], y: number[]): number {
  const n = x.length;
  if (n < 3) return 0;
  const rank = (arr: number[]) => {
    const order = arr.map((v, i) => ({ v, i })).sort((a, b) => a.v - b.v);
    const ranks = new Array(n);
    let i = 0;
    while (i < n) {
      let j = i;
      while (j < n && order[j].v === order[i].v) j++;
      const r = (i + j - 1) / 2 + 1;
      for (let k = i; k < j; k++) ranks[order[k].i] = r;
      i = j;
    }
    return ranks;
  };
  const rx = rank(x), ry = rank(y);
  const mx = rx.reduce((s, v) => s + v, 0) / n;
  const my = ry.reduce((s, v) => s + v, 0) / n;
  let num = 0, sx = 0, sy = 0;
  for (let i = 0; i < n; i++) {
    num += (rx[i] - mx) * (ry[i] - my);
    sx += (rx[i] - mx) ** 2;
    sy += (ry[i] - my) ** 2;
  }
  const denom = Math.sqrt(sx * sy);
  return denom === 0 ? 0 : num / denom;
}

// Two-tailed p-value from Spearman r via t-distribution approximation (n≥10)
function spearmanPValue(r: number, n: number): number {
  if (n < 4) return 1;
  const t = Math.abs(r) * Math.sqrt((n - 2) / (1 - r * r + 1e-12));
  const df = n - 2;
  // Beta function approximation for t-distribution CDF tail
  // Use normal approximation for df > 30, else use simple iterative
  if (df >= 30) {
    const z = t / Math.sqrt(1 + t * t / df);
    const poly = (v: number) => {
      const c = 1 / (1 + 0.2316419 * v);
      return c * (0.319381530 + c * (-0.356563782 + c * (1.781477937 + c * (-1.821255978 + c * 1.330274429))));
    };
    const pdf = Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI);
    return Math.min(1, 2 * pdf * poly(z));
  }
  // For small df, use regularized incomplete beta approximation
  const x = df / (df + t * t);
  // Simple approximation via continued fraction for I_x(df/2, 1/2)
  // Good enough for p-value ordering; not publication-grade
  let betaApprox = x ** (df / 2) * Math.pow(1 - x, 0.5) / ((df / 2) * Math.exp(0));
  betaApprox = Math.min(1, betaApprox * 2);
  return Math.min(1, betaApprox);
}

// ---------------------------------------------------------------------------
// Permutation test: shuffle feature vectors within family, recompute r
// ---------------------------------------------------------------------------

function permutationTest(
  morphDists: number[],
  lexDists: number[],
  featureVecs: number[][],
  pairIndices: [number, number][],
  nPerms: number,
): { pValue: number; permMean: number; permStd: number } {
  const observed = spearmanR(morphDists, lexDists);
  const n = featureVecs.length;
  let countGeq = 0;
  const permRs: number[] = [];

  for (let p = 0; p < nPerms; p++) {
    const shuffled = shuffle(featureVecs);
    const permMorphDists = pairIndices.map(([i, j]) => euclideanDistance(shuffled[i], shuffled[j]));
    const r = spearmanR(permMorphDists, lexDists);
    permRs.push(r);
    if (Math.abs(r) >= Math.abs(observed)) countGeq++;
    if ((p + 1) % 500 === 0) process.stdout.write(`  ${p + 1}/${nPerms}  `);
  }
  process.stdout.write('\n');

  const mean = permRs.reduce((s, v) => s + v, 0) / nPerms;
  const variance = permRs.reduce((s, v) => s + (v - mean) ** 2, 0) / nPerms;
  return { pValue: countGeq / nPerms, permMean: mean, permStd: Math.sqrt(variance) };
}

// ---------------------------------------------------------------------------
// Significance star helper
// ---------------------------------------------------------------------------

function star(p: number): string {
  if (p < 0.001) return '***';
  if (p < 0.01)  return '**';
  if (p < 0.05)  return '*';
  return 'ns';
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const N_PERMS = 2000;
const MIN_FAMILY_SIZE = 5;

async function main(): Promise<void> {
  console.log('folio-pair-distance-test — within-family morphological vs. lexical distance');
  console.log(`N_PERMS=${N_PERMS}  MIN_FAMILY_SIZE=${MIN_FAMILY_SIZE}`);
  console.log('');

  console.log('Loading folio_vision_analysis...');
  const visionRows = await executeSql(`
    SELECT folio_id, subject_candidates
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  console.log('Loading EVA corpus...');
  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  // Build family map
  const familyMap = new Map<string, string>();
  for (const row of visionRows) {
    if (!row.folio_id || !row.subject_candidates) continue;
    const family = classifyPlantFamily(row.subject_candidates);
    familyMap.set(row.folio_id, family);
  }

  // Build EVA map
  const evaMap = new Map<string, string[]>();
  for (const row of evaRows) {
    if (!row.folio_id || !row.eva_text) continue;
    const words = extractEvaWords(row.eva_text);
    if (words.length >= 20) evaMap.set(row.folio_id, words);
  }

  // Group folios by family
  const familyFolios = new Map<string, string[]>();
  for (const [folioId, family] of familyMap) {
    if (!evaMap.has(folioId)) continue;
    if (!familyFolios.has(family)) familyFolios.set(family, []);
    familyFolios.get(family)!.push(folioId);
  }

  console.log('\nFamily sizes (folios with EVA text ≥20 words):');
  const eligible: Array<[string, string[]]> = [];
  for (const [family, folios] of [...familyFolios].sort((a, b) => b[1].length - a[1].length)) {
    const mark = folios.length >= MIN_FAMILY_SIZE ? '✓' : '✗';
    console.log(`  ${mark} ${family.padEnd(18)} n=${folios.length}`);
    if (folios.length >= MIN_FAMILY_SIZE) eligible.push([family, folios]);
  }

  console.log(`\n${eligible.length} families eligible for within-family pair analysis\n`);

  const results: Array<{
    family: string;
    nFolios: number;
    nPairs: number;
    spearmanR: number;
    approxP: number;
    permP: number;
    direction: string;
  }> = [];

  // ─── Pooled analysis across all eligible families ─────────────────────────
  const allMorphDists: number[] = [];
  const allLexDists: number[] = [];

  for (const [family, folios] of eligible) {
    console.log(`\n=== ${family.toUpperCase()} (n=${folios.length}) ===`);

    const featureVecs = folios.map((id) => featureVector(evaMap.get(id)!));
    const vocabSets  = folios.map((id) => new Set(evaMap.get(id)!));

    const morphDists: number[] = [];
    const lexDists: number[] = [];
    const pairIndices: [number, number][] = [];

    for (let i = 0; i < folios.length; i++) {
      for (let j = i + 1; j < folios.length; j++) {
        const md = euclideanDistance(featureVecs[i], featureVecs[j]);
        const ld = 1 - jaccard(vocabSets[i], vocabSets[j]);
        morphDists.push(md);
        lexDists.push(ld);
        pairIndices.push([i, j]);
        allMorphDists.push(md);
        allLexDists.push(ld);
      }
    }

    console.log(`  ${folios.length} folios → ${morphDists.length} within-family pairs`);

    const r = spearmanR(morphDists, lexDists);
    const approxP = spearmanPValue(r, morphDists.length);

    console.log(`  Spearman r = ${r.toFixed(4)}  approx-p = ${approxP.toFixed(4)}`);
    console.log(`  Running permutation test (N=${N_PERMS})...`);

    const { pValue, permMean, permStd } = permutationTest(
      morphDists, lexDists, featureVecs, pairIndices, N_PERMS,
    );

    const direction = r > 0 ? 'morphologically similar → more shared vocab' : 'morphologically similar → less shared vocab';
    console.log(`  Permutation p = ${pValue.toFixed(4)} ${star(pValue)}`);
    console.log(`  Perm null: mean=${permMean.toFixed(4)} std=${permStd.toFixed(4)}`);
    console.log(`  Direction: ${direction}`);

    results.push({ family, nFolios: folios.length, nPairs: morphDists.length, spearmanR: r, approxP, permP: pValue, direction });
  }

  // ─── Pooled result ─────────────────────────────────────────────────────────
  if (allMorphDists.length > 0) {
    const rPooled = spearmanR(allMorphDists, allLexDists);
    const pPooled = spearmanPValue(rPooled, allMorphDists.length);
    console.log(`\n=== POOLED (all families, ${allMorphDists.length} pairs) ===`);
    console.log(`  Spearman r = ${rPooled.toFixed(4)}  approx-p = ${pPooled.toFixed(4)} ${star(pPooled)}`);
  }

  // ─── Summary table ─────────────────────────────────────────────────────────
  console.log('\n' + '─'.repeat(90));
  console.log('Family             n_folios  n_pairs  Spearman-r  approx-p  perm-p  sig  Interpretation');
  console.log('─'.repeat(90));
  for (const r of results) {
    const interp = r.permP < 0.05
      ? (r.spearmanR > 0 ? 'similar folios share more vocab ✓' : 'similar folios share LESS vocab (!)')
      : 'no within-family distance effect';
    console.log(
      `${r.family.padEnd(18)} ${String(r.nFolios).padStart(8)}  ${String(r.nPairs).padStart(7)}  `
      + `${r.spearmanR.toFixed(4).padStart(10)}  ${r.approxP.toFixed(4).padStart(8)}  `
      + `${r.permP.toFixed(4).padStart(6)}  ${star(r.permP).padEnd(4)} ${interp}`,
    );
  }
  console.log('─'.repeat(90));

  console.log('\nInterpretation guide:');
  console.log('  r > 0, p < 0.05 → morphological and lexical channels co-vary at folio level');
  console.log('                    (independent signals are not independent — shared underlying structure)');
  console.log('  r ≈ 0, p ≫ 0.05 → channels are orthogonal at folio level');
  console.log('                    (family effect is real but the two signals are independently assigned)');
  console.log('  r < 0, p < 0.05 → surprising anti-correlation; possible tradeoff or two-scribe hypothesis');

  console.log('\nDone.');
}

main().catch((e) => { console.error(e); process.exit(1); });

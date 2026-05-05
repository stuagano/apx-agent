/**
 * Permutation test for the morphological axis result.
 *
 * prefix-suffix-axis.ts found that solanaceae has a large rank-biserial
 * correlation for qo-prefix rate vs. other botanical families (r=0.538).
 * This script tests whether that effect size could arise from random family
 * assignment.
 *
 * Null model: shuffle family labels among botanical herbal folios, keeping
 * family sizes fixed, recompute rank-biserial r for each morphological
 * feature. Empirical p-value = fraction of shuffles where permuted r ≥ real r.
 *
 * This is independent of the expected-term database (no NPMI involved), so
 * it does not share the flaw found by permutation-test.ts.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx morpho-axis-permutation.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const N_PERMS = parseInt(process.env.N_PERMS ?? '1000');

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

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

function classifyPlantFamily(subjectCandidates: string): string {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return 'unknown'; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return 'uncertain';
  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();
  if (/papaver|poppy/.test(latin + name)) return 'poppy';
  if (/rosa|rose/.test(latin + name)) return 'rose';
  if (/mentha|mint|melissa|salvia|sage|thymus|thyme|origanum|oregano|rosmarinus|rosemary|lavandula/.test(latin + name)) return 'mint-family';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name)) return 'thistle';
  if (/helleborus|aconitum|ranunculus|clematis|anemone/.test(latin + name)) return 'ranunculaceae';
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name)) return 'solanaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';
  return 'other-botanical';
}

const BOTANICAL = new Set(['solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
  'mint-family', 'ranunculaceae', 'apiaceae', 'verbena', 'artemisia', 'brassicaceae', 'lily-family']);

interface MorphoRates {
  qo_rate: number;
  chy_rate: number;
  dy_rate: number;
  suffix_rate: number;
  ch_init: number;
  short_rate: number;
}

function computeRates(words: string[]): MorphoRates {
  const n = words.length;
  if (n === 0) return { qo_rate: 0, chy_rate: 0, dy_rate: 0, suffix_rate: 0, ch_init: 0, short_rate: 0 };
  let qo = 0, chy = 0, dy = 0, ch = 0, short = 0;
  for (const w of words) {
    if (w.startsWith('qo')) qo++;
    if (w.endsWith('chy')) chy++;
    if (w.endsWith('dy')) dy++;
    if (w.startsWith('ch')) ch++;
    if (w.length <= 3) short++;
  }
  return {
    qo_rate: qo / n, chy_rate: chy / n, dy_rate: dy / n,
    suffix_rate: (chy + dy) / n, ch_init: ch / n, short_rate: short / n,
  };
}

/** Rank-biserial correlation (non-parametric effect size for two-group comparison). */
function rankBiserial(groupA: number[], groupB: number[]): number {
  const nA = groupA.length, nB = groupB.length;
  if (nA === 0 || nB === 0) return 0;
  let u = 0;
  for (const a of groupA) {
    for (const b of groupB) {
      if (a > b) u++;
      else if (a === b) u += 0.5;
    }
  }
  return (2 * u) / (nA * nB) - 1;
}

function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

async function main(): Promise<void> {
  console.log('morpho-axis-permutation — permutation test for morphological axis effect sizes');
  console.log(`N_PERMS=${N_PERMS}`);
  console.log('');

  console.log('Loading data...');
  const [visionRows, evaRows] = await Promise.all([
    executeSql(`
      SELECT folio_id, subject_candidates
      FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
      WHERE section = 'herbal'
      ORDER BY folio_id
    `),
    executeSql(`
      SELECT folio_id, eva_text
      FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
      WHERE section = 'herbal'
      ORDER BY folio_id
    `),
  ]);

  const evaMap = new Map<string, string[]>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
  }

  interface FolioRecord { family: string; rates: MorphoRates; }
  const folios: FolioRecord[] = [];

  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL.has(family)) continue;
    folios.push({ family, rates: computeRates(words) });
  }

  const N = folios.length;
  console.log(`  ${N} botanical herbal folios\n`);

  // Family counts
  const famCounts = new Map<string, number>();
  for (const f of folios) famCounts.set(f.family, (famCounts.get(f.family) ?? 0) + 1);
  console.log('Family sizes:', [...famCounts.entries()].sort((a,b)=>b[1]-a[1]).map(([f,n])=>`${f}=${n}`).join(', '));
  console.log('');

  // Features to test
  const features: Array<{ name: string; label: string; get: (r: MorphoRates) => number }> = [
    { name: 'qo_rate',    label: 'qo-prefix',  get: (r) => r.qo_rate },
    { name: 'chy_rate',   label: '-chy suffix', get: (r) => r.chy_rate },
    { name: 'dy_rate',    label: '-dy suffix',  get: (r) => r.dy_rate },
    { name: 'suffix_rate',label: 'chy+dy sfx',  get: (r) => r.suffix_rate },
    { name: 'ch_init',    label: 'ch-init',     get: (r) => r.ch_init },
    { name: 'short_rate', label: 'short(≤3)',   get: (r) => r.short_rate },
  ];

  // Test: solanaceae vs. all other botanical
  const solanIdx  = folios.map((f, i) => f.family === 'solanaceae' ? i : -1).filter((i) => i >= 0);
  const otherIdx  = folios.map((f, i) => f.family !== 'solanaceae' ? i : -1).filter((i) => i >= 0);
  const allLabels = folios.map((f) => f.family);

  // Real rank-biserial r per feature
  const realR: Record<string, number> = {};
  for (const feat of features) {
    const solanVals = solanIdx.map((i) => feat.get(folios[i].rates));
    const otherVals = otherIdx.map((i) => feat.get(folios[i].rates));
    realR[feat.name] = rankBiserial(solanVals, otherVals);
  }

  console.log('=== REAL RANK-BISERIAL CORRELATIONS (solanaceae vs. other botanical) ===');
  for (const feat of features) {
    const r = realR[feat.name];
    const dir = r > 0 ? 'solan HIGHER' : 'solan LOWER';
    console.log(`  ${feat.label.padEnd(14)} r=${r.toFixed(3)}  [${dir}]`);
  }
  console.log('');

  // Permutation: shuffle family labels preserving sizes
  console.log(`Running ${N_PERMS} permutations...`);
  const permR: Record<string, number[]> = {};
  for (const feat of features) permR[feat.name] = [];

  for (let p = 0; p < N_PERMS; p++) {
    const shuffledLabels = shuffle(allLabels);
    const pSolanIdx = shuffledLabels.map((l, i) => l === 'solanaceae' ? i : -1).filter((i) => i >= 0);
    const pOtherIdx = shuffledLabels.map((l, i) => l !== 'solanaceae' ? i : -1).filter((i) => i >= 0);
    for (const feat of features) {
      const sv = pSolanIdx.map((i) => feat.get(folios[i].rates));
      const ov = pOtherIdx.map((i) => feat.get(folios[i].rates));
      permR[feat.name].push(rankBiserial(sv, ov));
    }
    if ((p + 1) % 250 === 0) process.stdout.write(`  ${p + 1}/${N_PERMS}\r`);
  }
  process.stdout.write('\n\n');

  // Results table
  console.log('=== PERMUTATION TEST RESULTS ===');
  console.log('');
  console.log(
    'Feature'.padEnd(14) + 'Real r'.padEnd(10) +
    'Perm mean'.padEnd(12) + 'Perm max'.padEnd(12) +
    'p-value'.padEnd(10) + 'Sig?',
  );
  console.log('-'.repeat(72));

  for (const feat of features) {
    const r = realR[feat.name];
    const perms = permR[feat.name];
    const permMean = perms.reduce((s, x) => s + x, 0) / N_PERMS;
    const permMax  = Math.max(...perms);
    // One-tailed: how often |perm_r| >= |real_r| in the same direction
    const atLeast  = perms.filter((x) => (r >= 0 ? x >= r : x <= r)).length;
    const p = atLeast / N_PERMS;
    const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : p < 0.10 ? '.' : '';
    console.log(
      feat.label.padEnd(14) +
      r.toFixed(3).padEnd(10) +
      permMean.toFixed(3).padEnd(12) +
      permMax.toFixed(3).padEnd(12) +
      p.toFixed(4).padEnd(10) +
      sig,
    );
  }
  console.log('');
  console.log('Significance: *** p<0.001  ** p<0.01  * p<0.05  . p<0.10');
  console.log('');

  // Quick histogram for qo_rate (the primary feature)
  const qoPerms = permR['qo_rate'];
  const qoReal  = realR['qo_rate'];
  const bins = 12;
  const lo = Math.min(...qoPerms, qoReal);
  const hi = Math.max(...qoPerms, qoReal);
  const bw = (hi - lo) / bins;
  const hist = new Array(bins).fill(0);
  for (const x of qoPerms) hist[Math.min(bins-1, Math.floor((x - lo) / bw))]++;

  console.log('qo-prefix r permutation distribution:');
  const hmax = Math.max(...hist);
  for (let b = 0; b < bins; b++) {
    const blo = lo + b * bw, bhi = blo + bw;
    const bar = '█'.repeat(Math.round(hist[b] / hmax * 30));
    const marker = qoReal >= blo && qoReal < bhi + 1e-9 ? ' ← real' : '';
    console.log(`  [${blo.toFixed(3)}, ${bhi.toFixed(3)}]  ${String(hist[b]).padEnd(5)} ${bar}${marker}`);
  }
}

main().catch((err) => {
  console.error('[morpho-axis-permutation] fatal:', err);
  process.exit(1);
});

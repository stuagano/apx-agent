/**
 * Family-distance × morphology interaction test.
 *
 * The morpho-axis-permutation.ts result showed solanaceae has a large,
 * statistically significant qo-prefix elevation (r=+0.532, p<0.001). But is
 * this a content signal or a quire/scribal-hand artifact?
 *
 * A scribal-hand artifact would appear as: the elevated qo-prefix rate is
 * concentrated in one quire (one scribe wrote all solanaceae folios in that
 * quire with a higher qo-prefix habit), not spread across the manuscript.
 *
 * Two tests:
 *
 * TEST A — Pairwise distance × signed morphological diff (analogous to
 *   family-distance-test.ts Jaccard test):
 *   For each (solanaceae folio i, other-botanical folio j) pair, compute
 *   qo_rate[i] - qo_rate[j] and |folio_rank[i] - folio_rank[j]|.
 *   Bin by distance (5 quantile bins). If mean signed diff is stable across
 *   bins → content encoding. If it decays toward 0 → quire artifact.
 *
 * TEST B — Per-quire breakdown (direct scribal-hand falsification):
 *   Assign each folio to an approximate quire (floor((n-1)/8)+1).
 *   For each quire with ≥1 solanaceae AND ≥1 other botanical folio, compute
 *   mean qo_rate per group. If solanaceae is elevated within multiple quires
 *   independently, a single-hand explanation fails.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx morpho-distance-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

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

function folioToSortKey(folioId: string): number {
  const m = folioId.match(/^f(\d+)(r|v)(\d?)$/i);
  if (!m) return 999999;
  const n = parseInt(m[1], 10);
  const side = m[2].toLowerCase() === 'v' ? 1 : 0;
  const sub = m[3] ? parseInt(m[3], 10) : 0;
  return n * 100 + side * 10 + sub;
}

function folioNumber(folioId: string): number {
  const m = folioId.match(/^f(\d+)/i);
  return m ? parseInt(m[1], 10) : 9999;
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

interface MorphoRates { qo_rate: number; chy_rate: number; dy_rate: number; }

function computeRates(words: string[]): MorphoRates {
  const n = words.length;
  if (n === 0) return { qo_rate: 0, chy_rate: 0, dy_rate: 0 };
  let qo = 0, chy = 0, dy = 0;
  for (const w of words) {
    if (w.startsWith('qo')) qo++;
    if (w.endsWith('chy')) chy++;
    if (w.endsWith('dy')) dy++;
  }
  return { qo_rate: qo / n, chy_rate: chy / n, dy_rate: dy / n };
}

function quantileBreakpoints(values: number[], nBuckets: number): number[] {
  const sorted = [...values].sort((a, b) => a - b);
  const breaks: number[] = [];
  for (let i = 1; i < nBuckets; i++) {
    breaks.push(sorted[Math.floor((i / nBuckets) * sorted.length)]);
  }
  return breaks;
}

function bucketIndex(value: number, breaks: number[]): number {
  for (let i = 0; i < breaks.length; i++) if (value <= breaks[i]) return i;
  return breaks.length;
}

async function main(): Promise<void> {
  console.log('morpho-distance-test — family-distance × morphology interaction');
  console.log('Tests whether solanaceae qo-prefix elevation is stable or decays with folio distance');
  console.log('');

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

  interface FolioRecord {
    folioId: string;
    sortKey: number;
    quire: number;
    family: string;
    rates: MorphoRates;
  }

  const folios: FolioRecord[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL.has(family)) continue;
    const sk = folioToSortKey(r.folio_id);
    const fn = folioNumber(r.folio_id);
    const quire = Math.floor((fn - 1) / 8) + 1;
    folios.push({ folioId: r.folio_id, sortKey: sk, quire, family, rates: computeRates(words) });
  }

  folios.sort((a, b) => a.sortKey - b.sortKey);
  const N = folios.length;
  console.log(`  ${N} botanical herbal folios\n`);

  const solanFolios = folios.filter((f) => f.family === 'solanaceae');
  const otherFolios = folios.filter((f) => f.family !== 'solanaceae');
  console.log(`  solanaceae: ${solanFolios.length}  other botanical: ${otherFolios.length}\n`);

  // -------------------------------------------------------------------------
  // TEST A: pairwise signed diff binned by folio rank distance
  // -------------------------------------------------------------------------
  console.log('=== TEST A: Pairwise distance × morphological elevation ===');
  console.log('Each pair: (solanaceae folio i, other-botanical folio j)');
  console.log('Signed diff = qo_rate[solan] - qo_rate[other]  (positive = solan higher)');
  console.log('Binned by |folio_rank_i - folio_rank_j|');
  console.log('');

  // Assign ordinal ranks (position in sorted array)
  const rankMap = new Map<string, number>();
  folios.forEach((f, i) => rankMap.set(f.folioId, i));

  // Build all (solan, other) pairs with their rank distance and signed diffs
  interface Pair { rankDist: number; qoDiff: number; chyDiff: number; dyDiff: number; }
  const pairs: Pair[] = [];
  for (const s of solanFolios) {
    for (const o of otherFolios) {
      const rankDist = Math.abs((rankMap.get(s.folioId) ?? 0) - (rankMap.get(o.folioId) ?? 0));
      pairs.push({
        rankDist,
        qoDiff:  s.rates.qo_rate  - o.rates.qo_rate,
        chyDiff: s.rates.chy_rate - o.rates.chy_rate,
        dyDiff:  s.rates.dy_rate  - o.rates.dy_rate,
      });
    }
  }

  const N_BINS = 5;
  const breaks = quantileBreakpoints(pairs.map((p) => p.rankDist), N_BINS);
  const bins: Pair[][] = Array.from({ length: N_BINS }, () => []);
  for (const p of pairs) bins[bucketIndex(p.rankDist, breaks)].push(p);

  const binLabels = breaks.map((b, i) => {
    const lo = i === 0 ? 0 : breaks[i - 1] + 1;
    return `[${lo}–${b}]`;
  }).concat([`[${breaks[breaks.length - 1] + 1}–]`]);

  function mean(arr: number[]): number { return arr.length ? arr.reduce((s, x) => s + x, 0) / arr.length : NaN; }

  console.log('Bin'.padEnd(14) + 'Pairs'.padEnd(8) + 'qo diff'.padEnd(12) + 'chy diff'.padEnd(12) + 'dy diff');
  console.log('-'.repeat(58));
  for (let b = 0; b < N_BINS; b++) {
    const bp = bins[b];
    const qoM  = mean(bp.map((p) => p.qoDiff));
    const chyM = mean(bp.map((p) => p.chyDiff));
    const dyM  = mean(bp.map((p) => p.dyDiff));
    const qoBar = qoM > 0 ? '+' + qoM.toFixed(3) : qoM.toFixed(3);
    const chyBar = chyM > 0 ? '+' + chyM.toFixed(3) : chyM.toFixed(3);
    const dyBar = dyM > 0 ? '+' + dyM.toFixed(3) : dyM.toFixed(3);
    console.log(
      binLabels[b].padEnd(14) +
      String(bp.length).padEnd(8) +
      qoBar.padEnd(12) +
      chyBar.padEnd(12) +
      dyBar,
    );
  }
  console.log('');

  // Correlation between rank distance and absolute qo diff (does elevation decay?)
  const qoDiffs = pairs.map((p) => p.qoDiff);
  const dists   = pairs.map((p) => p.rankDist);
  const meanQo  = mean(qoDiffs);
  const meanD   = mean(dists);
  let cov = 0, varQo = 0, varD = 0;
  for (let i = 0; i < pairs.length; i++) {
    cov   += (qoDiffs[i] - meanQo) * (dists[i] - meanD);
    varQo += (qoDiffs[i] - meanQo) ** 2;
    varD  += (dists[i]   - meanD)  ** 2;
  }
  const pearsonR = cov / Math.sqrt(varQo * varD);
  console.log(`Pearson r(qo_diff, rank_distance) = ${pearsonR.toFixed(4)}`);
  console.log(`  Near 0 → no distance decay (stable across manuscript)`);
  console.log(`  Negative → solanaceae elevation shrinks at longer distances (quire artifact)`);
  console.log('');

  // -------------------------------------------------------------------------
  // TEST B: Per-quire breakdown
  // -------------------------------------------------------------------------
  console.log('=== TEST B: Per-quire breakdown ===');
  console.log('Each quire with ≥1 solanaceae AND ≥1 other-botanical folio');
  console.log('');

  const quireMap = new Map<number, { solan: FolioRecord[]; other: FolioRecord[] }>();
  for (const f of folios) {
    if (!quireMap.has(f.quire)) quireMap.set(f.quire, { solan: [], other: [] });
    const q = quireMap.get(f.quire)!;
    if (f.family === 'solanaceae') q.solan.push(f);
    else q.other.push(f);
  }

  // Quires with enough data to compare
  const mixed = [...quireMap.entries()]
    .filter(([, q]) => q.solan.length >= 1 && q.other.length >= 1)
    .sort(([a], [b]) => a - b);

  console.log(
    'Quire'.padEnd(8) + 'Solan n'.padEnd(10) + 'Other n'.padEnd(10) +
    'Solan qo'.padEnd(12) + 'Other qo'.padEnd(12) + 'Diff'.padEnd(10) + 'Folios (solan)',
  );
  console.log('-'.repeat(80));

  let elevatedQuires = 0;
  for (const [quireNum, q] of mixed) {
    const solanQo = mean(q.solan.map((f) => f.rates.qo_rate));
    const otherQo = mean(q.other.map((f) => f.rates.qo_rate));
    const diff = solanQo - otherQo;
    if (diff > 0) elevatedQuires++;
    const arrow = diff > 0.01 ? '↑' : diff < -0.01 ? '↓' : '≈';
    const solanIds = q.solan.map((f) => f.folioId).join(',');
    console.log(
      String(quireNum).padEnd(8) +
      String(q.solan.length).padEnd(10) +
      String(q.other.length).padEnd(10) +
      solanQo.toFixed(4).padEnd(12) +
      otherQo.toFixed(4).padEnd(12) +
      `${diff >= 0 ? '+' : ''}${diff.toFixed(4)} ${arrow}`.padEnd(10) +
      solanIds,
    );
  }
  console.log('');
  console.log(`Quires with solanaceae qo_rate > other: ${elevatedQuires}/${mixed.length}`);
  console.log('');

  // Also: distribution of solanaceae folios across quires
  console.log('Solanaceae folio distribution across quires:');
  const solanByQuire = new Map<number, string[]>();
  for (const f of solanFolios) {
    const q = solanByQuire.get(f.quire) ?? [];
    q.push(f.folioId);
    solanByQuire.set(f.quire, q);
  }
  for (const [q, ids] of [...solanByQuire.entries()].sort(([a], [b]) => a - b)) {
    console.log(`  Quire ${String(q).padStart(2)}: n=${ids.length}  ${ids.join(', ')}`);
  }
  console.log('');

  // -------------------------------------------------------------------------
  // Summary
  // -------------------------------------------------------------------------
  console.log('=== INTERPRETATION ===');
  console.log('');
  const nQ = mixed.length;
  if (Math.abs(pearsonR) < 0.05 && elevatedQuires >= Math.ceil(nQ * 0.75)) {
    console.log(`VERDICT: Content encoding supported.`);
    console.log(`  qo-diff shows no distance decay (r=${pearsonR.toFixed(3)}),`);
    console.log(`  and solanaceae is elevated in ${elevatedQuires}/${nQ} mixed quires.`);
    console.log(`  A scribal-hand artifact would require the same elevated hand`);
    console.log(`  in ${elevatedQuires} separate quires — implausible.`);
  } else if (Math.abs(pearsonR) < 0.10 && elevatedQuires >= Math.ceil(nQ * 0.6)) {
    console.log(`VERDICT: Weakly consistent with content encoding.`);
    console.log(`  qo-diff distance correlation is small (r=${pearsonR.toFixed(3)}),`);
    console.log(`  solanaceae elevated in ${elevatedQuires}/${nQ} mixed quires.`);
  } else if (pearsonR < -0.10) {
    console.log(`VERDICT: Possible quire artifact.`);
    console.log(`  qo-diff decays with distance (r=${pearsonR.toFixed(3)}) — the elevation`);
    console.log(`  is concentrated in nearby folios, consistent with a localized scribal hand.`);
  } else {
    console.log(`VERDICT: Ambiguous. pearsonR=${pearsonR.toFixed(3)}, elevated quires=${elevatedQuires}/${nQ}.`);
    console.log(`  Manual review of per-quire table recommended.`);
  }
}

main().catch((err) => {
  console.error('[morpho-distance-test] fatal:', err);
  process.exit(1);
});

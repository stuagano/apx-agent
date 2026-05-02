/**
 * Prefix/suffix morphological axis test.
 *
 * TEST 2 (vision-correlation.ts) found on 5 individual folios that:
 *   - solanaceae (f13r, f33r): dominated by -chy/-dy suffixes
 *   - thistle/lily-family/mint-family (f18v, f79r, f21r): dominated by qo- prefix
 *
 * This script tests whether that pattern holds statistically across all
 * 94 herbal folios with recognized botanical family classification. For each
 * folio we compute per-word morphological rates (qo_rate, chy_suffix, dy_suffix,
 * ch_init, sh_init, etc.), then compare family-level means and compute effect
 * sizes for solanaceae vs. other botanical families.
 *
 * Also identifies the top EVA word types over-represented in each major family
 * by log-odds ratio vs. all other botanical families.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx prefix-suffix-axis.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// ---------------------------------------------------------------------------
// SQL helper
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
// EVA word extraction
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Plant family classification (same allowlist as other scripts)
// ---------------------------------------------------------------------------

const BOTANICAL_FAMILIES = new Set([
  'solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
  'mint-family', 'ranunculaceae', 'apiaceae', 'verbena',
  'artemisia', 'brassicaceae', 'lily-family',
]);

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
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/nasturtium|tropaeolum|brassica|cress/.test(latin + name)) return 'brassicaceae';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';

  const genus = latin.split(/[\s/,]/)[0];
  return genus || 'unknown';
}

// ---------------------------------------------------------------------------
// Morphological feature extraction
// ---------------------------------------------------------------------------

interface MorphoProfile {
  folioId: string;
  family: string;
  n: number;            // total word tokens
  qo_rate: number;      // words starting with 'qo'
  chy_rate: number;     // words ending with 'chy'
  dy_rate: number;      // words ending with 'dy'
  suffix_rate: number;  // chy_rate + dy_rate
  ch_init: number;      // words starting with 'ch'
  sh_init: number;      // words starting with 'sh'
  ol_init: number;      // words starting with 'ol'
  aiin_rate: number;    // words containing 'aiin'
  short_rate: number;   // words of length 2–3
  long_rate: number;    // words of length ≥ 7
  freq: Map<string, number>;
}

function buildProfile(folioId: string, family: string, words: string[]): MorphoProfile {
  const n = words.length;
  if (n === 0) return {
    folioId, family, n: 0,
    qo_rate: 0, chy_rate: 0, dy_rate: 0, suffix_rate: 0,
    ch_init: 0, sh_init: 0, ol_init: 0, aiin_rate: 0,
    short_rate: 0, long_rate: 0,
    freq: new Map(),
  };

  let qo = 0, chy = 0, dy = 0, ch = 0, sh = 0, ol = 0, aiin = 0, short = 0, long = 0;
  const freq = new Map<string, number>();

  for (const w of words) {
    freq.set(w, (freq.get(w) ?? 0) + 1);
    if (w.startsWith('qo')) qo++;
    if (w.endsWith('chy')) chy++;
    if (w.endsWith('dy')) dy++;
    if (w.startsWith('ch')) ch++;
    if (w.startsWith('sh')) sh++;
    if (w.startsWith('ol')) ol++;
    if (w.includes('aiin')) aiin++;
    if (w.length <= 3) short++;
    if (w.length >= 7) long++;
  }

  return {
    folioId, family, n,
    qo_rate: qo / n,
    chy_rate: chy / n,
    dy_rate: dy / n,
    suffix_rate: (chy + dy) / n,
    ch_init: ch / n,
    sh_init: sh / n,
    ol_init: ol / n,
    aiin_rate: aiin / n,
    short_rate: short / n,
    long_rate: long / n,
    freq,
  };
}

// ---------------------------------------------------------------------------
// Effect size: rank-biserial correlation (non-parametric, no external deps)
// r = (2 * mean_rank_A / N_total) - 1  [simplified Mann-Whitney effect size]
// ---------------------------------------------------------------------------

function mannWhitneyEffectSize(groupA: number[], groupB: number[]): number {
  const nA = groupA.length, nB = groupB.length;
  if (nA === 0 || nB === 0) return 0;

  // Count U statistic: for each a in A, count b in B where a > b (+ 0.5 for ties)
  let u = 0;
  for (const a of groupA) {
    for (const b of groupB) {
      if (a > b) u++;
      else if (a === b) u += 0.5;
    }
  }
  // Rank-biserial correlation: r = 2U / (nA * nB) - 1
  return (2 * u) / (nA * nB) - 1;
}

function mean(xs: number[]): number {
  if (xs.length === 0) return 0;
  return xs.reduce((s, x) => s + x, 0) / xs.length;
}

function std(xs: number[]): number {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / (xs.length - 1));
}

// ---------------------------------------------------------------------------
// Log-odds ratio for word over-representation
// ---------------------------------------------------------------------------

function logOdds(
  wordFreqIn: Map<string, number>, totalIn: number,
  wordFreqOut: Map<string, number>, totalOut: number,
  minCount: number,
  topN: number,
): Array<{ word: string; lor: number; countIn: number; countOut: number }> {
  const results: Array<{ word: string; lor: number; countIn: number; countOut: number }> = [];
  for (const [w, cIn] of wordFreqIn) {
    if (cIn < minCount) continue;
    const cOut = wordFreqOut.get(w) ?? 0;
    const pIn = (cIn + 0.5) / (totalIn + 1);
    const pOut = (cOut + 0.5) / (totalOut + 1);
    const lor = Math.log(pIn / (1 - pIn)) - Math.log(pOut / (1 - pOut));
    results.push({ word: w, lor, countIn: cIn, countOut: cOut });
  }
  return results.sort((a, b) => b.lor - a.lor).slice(0, topN);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('prefix-suffix-axis — qo-prefix vs. -chy/-dy suffix morphological axis test');
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

  const evaMap = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, r.eva_text);
  }

  const profiles: MorphoProfile[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const evaText = evaMap.get(r.folio_id) ?? '';
    if (!evaText) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;
    const words = extractEvaWords(evaText);
    if (words.length < 10) continue;
    profiles.push(buildProfile(r.folio_id, family, words));
  }

  console.log(`  ${profiles.length} botanical herbal folios\n`);

  // Group profiles by family
  const byFamily = new Map<string, MorphoProfile[]>();
  for (const p of profiles) {
    const arr = byFamily.get(p.family) ?? [];
    arr.push(p);
    byFamily.set(p.family, arr);
  }

  // Morphological feature names and accessors
  const features: Array<{ name: string; label: string; get: (p: MorphoProfile) => number }> = [
    { name: 'qo_rate',    label: 'qo-prefix',  get: (p) => p.qo_rate },
    { name: 'chy_rate',   label: '-chy suffix', get: (p) => p.chy_rate },
    { name: 'dy_rate',    label: '-dy suffix',  get: (p) => p.dy_rate },
    { name: 'suffix_rate',label: 'chy+dy sfx',  get: (p) => p.suffix_rate },
    { name: 'ch_init',    label: 'ch-init',     get: (p) => p.ch_init },
    { name: 'sh_init',    label: 'sh-init',     get: (p) => p.sh_init },
    { name: 'aiin_rate',  label: 'aiin-word',   get: (p) => p.aiin_rate },
    { name: 'short_rate', label: 'short(≤3)',   get: (p) => p.short_rate },
    { name: 'long_rate',  label: 'long(≥7)',    get: (p) => p.long_rate },
  ];

  // Print per-family morphological rates table
  const families = [...byFamily.keys()].sort((a, b) => (byFamily.get(b)!.length) - (byFamily.get(a)!.length));

  console.log('=== PER-FAMILY MORPHOLOGICAL RATES ===');
  console.log('');
  const hdr = 'Family'.padEnd(14) + 'n'.padEnd(5) +
    features.map((f) => f.label.padEnd(11)).join('');
  console.log(hdr);
  console.log('-'.repeat(hdr.length));
  for (const fam of families) {
    const ps = byFamily.get(fam)!;
    const row = fam.padEnd(14) + String(ps.length).padEnd(5) +
      features.map((f) => {
        const vals = ps.map((p) => f.get(p));
        return mean(vals).toFixed(4).padEnd(11);
      }).join('');
    console.log(row);
  }
  console.log('');

  // Solanaceae vs. all other botanical: effect sizes per feature
  const solan = byFamily.get('solanaceae') ?? [];
  const nonSolan = profiles.filter((p) => p.family !== 'solanaceae');

  console.log('=== SOLANACEAE vs. OTHER BOTANICAL: EFFECT SIZES (rank-biserial r) ===');
  console.log('');
  console.log(
    'Feature'.padEnd(14) +
    'Solan mean'.padEnd(13) + 'Solan std'.padEnd(12) +
    'Other mean'.padEnd(13) + 'Other std'.padEnd(12) +
    'r (pos=solan higher)',
  );
  console.log('-'.repeat(80));
  for (const f of features) {
    const solanVals = solan.map((p) => f.get(p));
    const otherVals = nonSolan.map((p) => f.get(p));
    const r = mannWhitneyEffectSize(solanVals, otherVals);
    console.log(
      f.label.padEnd(14) +
      mean(solanVals).toFixed(4).padEnd(13) +
      std(solanVals).toFixed(4).padEnd(12) +
      mean(otherVals).toFixed(4).padEnd(13) +
      std(otherVals).toFixed(4).padEnd(12) +
      r.toFixed(3) + (Math.abs(r) >= 0.2 ? (r > 0 ? ' ← SOLAN HIGHER' : ' ← SOLAN LOWER') : ''),
    );
  }
  console.log('');
  console.log('Effect size guide: |r| < 0.1 negligible, 0.1–0.2 small, 0.2–0.4 medium, ≥ 0.4 large');
  console.log('');

  // Thistle vs. solanaceae direct comparison
  const thistle = byFamily.get('thistle') ?? [];
  console.log('=== SOLANACEAE vs. THISTLE: DIRECT COMPARISON ===');
  console.log('');
  console.log(
    'Feature'.padEnd(14) +
    'Solan mean'.padEnd(13) +
    'Thistle mean'.padEnd(14) +
    'r (pos=solan higher)',
  );
  console.log('-'.repeat(65));
  for (const f of features) {
    const sv = solan.map((p) => f.get(p));
    const tv = thistle.map((p) => f.get(p));
    const r = mannWhitneyEffectSize(sv, tv);
    console.log(
      f.label.padEnd(14) +
      mean(sv).toFixed(4).padEnd(13) +
      mean(tv).toFixed(4).padEnd(14) +
      r.toFixed(3) + (Math.abs(r) >= 0.2 ? (r > 0 ? ' ← SOLAN HIGHER' : ' ← THISTLE HIGHER') : ''),
    );
  }
  console.log('');

  // Top over-represented words per family by log-odds ratio
  // Build aggregate freq maps per family
  const familyFreq = new Map<string, { freq: Map<string, number>; total: number }>();
  for (const [fam, ps] of byFamily) {
    const freq = new Map<string, number>();
    let total = 0;
    for (const p of ps) {
      for (const [w, c] of p.freq) {
        freq.set(w, (freq.get(w) ?? 0) + c);
        total += c;
      }
    }
    familyFreq.set(fam, { freq, total });
  }

  const bigFamilies = families.filter((f) => (byFamily.get(f)?.length ?? 0) >= 10);

  for (const targetFam of bigFamilies) {
    const inData = familyFreq.get(targetFam)!;
    // Pool all OTHER botanical families
    const outFreq = new Map<string, number>();
    let outTotal = 0;
    for (const [fam, data] of familyFreq) {
      if (fam === targetFam) continue;
      for (const [w, c] of data.freq) {
        outFreq.set(w, (outFreq.get(w) ?? 0) + c);
        outTotal += c;
      }
    }

    const topWords = logOdds(inData.freq, inData.total, outFreq, outTotal, 3, 15);

    console.log(`=== TOP EVA WORDS — ${targetFam.toUpperCase()} (vs. other botanical families) ===`);
    console.log('');
    console.log('Word'.padEnd(14) + 'LOR'.padEnd(8) + 'Count-in'.padEnd(12) + 'Count-out'.padEnd(12) + 'qo?  -chy?  -dy?');
    console.log('-'.repeat(65));
    for (const { word, lor, countIn, countOut } of topWords) {
      const qo  = word.startsWith('qo') ? 'qo ' : '   ';
      const chy = word.endsWith('chy') ? '-chy ' : '     ';
      const dy  = word.endsWith('dy') ? '-dy' : '';
      console.log(
        word.padEnd(14) +
        lor.toFixed(3).padEnd(8) +
        String(countIn).padEnd(12) +
        String(countOut).padEnd(12) +
        qo + chy + dy,
      );
    }
    console.log('');
  }

  // Summary axis interpretation
  console.log('=== AXIS INTERPRETATION ===');
  console.log('');
  const solanQo     = mean(solan.map((p) => p.qo_rate));
  const thistleQo   = mean(thistle.map((p) => p.qo_rate));
  const solanSfx    = mean(solan.map((p) => p.suffix_rate));
  const thistleSfx  = mean(thistle.map((p) => p.suffix_rate));

  console.log(`qo-prefix rate:  solanaceae=${solanQo.toFixed(4)}  thistle=${thistleQo.toFixed(4)}  diff=${(solanQo-thistleQo>0?'+':'')}${(solanQo-thistleQo).toFixed(4)}`);
  console.log(`-chy/-dy rate:   solanaceae=${solanSfx.toFixed(4)}  thistle=${thistleSfx.toFixed(4)}  diff=${(solanSfx-thistleSfx>0?'+':'')}${(solanSfx-thistleSfx).toFixed(4)}`);
  console.log('');

  if (solanSfx > thistleSfx && thistleQo > solanQo) {
    console.log('CONFIRMED: The qo-prefix / -chy-suffix axis from TEST 2 holds at scale.');
    console.log('  solanaceae → suffix-dominant (-chy/-dy higher)');
    console.log('  thistle → qo-prefix-dominant (qo_rate higher)');
  } else if (solanSfx > thistleSfx) {
    console.log('PARTIAL: solanaceae has higher suffix rate, but qo-prefix separation is weak.');
  } else if (thistleQo > solanQo) {
    console.log('PARTIAL: thistle has higher qo-prefix rate, but suffix separation is weak.');
  } else {
    console.log('NOT CONFIRMED: Axis does not hold at scale — TEST 2 observation was noise on 5 folios.');
  }
}

main().catch((err) => {
  console.error('[prefix-suffix-axis] fatal:', err);
  process.exit(1);
});

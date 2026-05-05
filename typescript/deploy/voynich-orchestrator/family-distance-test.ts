/**
 * Distance-controlled Jaccard test: content-driven vs. quire-positional clustering.
 *
 * If EVA vocabulary clusters by plant family because the manuscript has
 * botanical semantic content, the within-family Jaccard lift should be roughly
 * flat across folio-rank distance. If it's a quire/positional artifact (nearby
 * folios share scribal habits), within-family lift should decay sharply as
 * folio distance increases.
 *
 * Method:
 *   1. Load all herbal folios with botanical family classification + EVA text.
 *   2. Assign ordinal ranks by manuscript position (sorted parsed folio number).
 *   3. Compute pairwise Jaccard for all pairs where both folios have a
 *      recognized botanical family (solanaceae, thistle, plantago, etc.).
 *   4. Classify each pair as within-family or between-family.
 *   5. Bin by |rank_a − rank_b| using quantile breakpoints of ALL pair distances.
 *   6. Report mean within-family and between-family Jaccard by distance bin.
 *
 * Interpretation:
 *   - flat lift → content signal is real (falsifies pure quire-position theory)
 *   - decaying lift → quire artifact; family clustering is proximity confound
 *   - no lift at any distance → family signal is noise
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx family-distance-test.ts
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
// Folio parsing and ranking
// ---------------------------------------------------------------------------

/**
 * Convert a folio ID to a sortable integer.
 * f13r → 1300, f13v → 1310, f102r1 → 10201, f102v2 → 10222
 * Guarantees: recto < verso, earlier folio < later folio.
 */
function folioToSortKey(folioId: string): number {
  const m = folioId.match(/^f(\d+)(r|v)(\d?)$/i);
  if (!m) return 999999;
  const n = parseInt(m[1], 10);
  const side = m[2].toLowerCase() === 'v' ? 1 : 0;
  const sub = m[3] ? parseInt(m[3], 10) : 0;
  return n * 100 + side * 10 + sub;
}

// ---------------------------------------------------------------------------
// EVA extraction and Jaccard
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): Set<string> {
  const words = evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
  return new Set(words);
}

function jaccard(setA: Set<string>, setB: Set<string>): number {
  if (setA.size === 0 && setB.size === 0) return 1;
  let inter = 0;
  for (const w of setA) if (setB.has(w)) inter++;
  const union = setA.size + setB.size - inter;
  return union === 0 ? 0 : inter / union;
}

// ---------------------------------------------------------------------------
// Plant family classification
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
// Quantile bucketing
// ---------------------------------------------------------------------------

function quantileBreakpoints(values: number[], nBuckets: number): number[] {
  const sorted = [...values].sort((a, b) => a - b);
  const breaks: number[] = [];
  for (let i = 1; i < nBuckets; i++) {
    const idx = Math.floor((i / nBuckets) * sorted.length);
    breaks.push(sorted[idx]);
  }
  return breaks;
}

function bucketIndex(value: number, breaks: number[]): number {
  for (let i = 0; i < breaks.length; i++) {
    if (value <= breaks[i]) return i;
  }
  return breaks.length;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('family-distance-test — distance-controlled Jaccard to separate content vs. quire confound');
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

  // Build EVA map
  const evaMap = new Map<string, Set<string>>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) {
      evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
    }
  }

  // Build folio list: family + words + raw sort key
  interface FolioData {
    folioId: string;
    sortKey: number;
    rank: number;
    family: string;
    words: Set<string>;
  }

  const rawFolios: Omit<FolioData, 'rank'>[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id);
    if (!words || words.size === 0) continue;

    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;

    rawFolios.push({
      folioId: r.folio_id,
      sortKey: folioToSortKey(r.folio_id),
      family,
      words,
    });
  }

  // Sort by manuscript order and assign ordinal ranks
  rawFolios.sort((a, b) => a.sortKey - b.sortKey);
  const folios: FolioData[] = rawFolios.map((f, i) => ({ ...f, rank: i }));

  console.log(`  ${folios.length} herbal folios with recognized botanical family + EVA text`);
  console.log('');

  // Family sizes
  const familyCounts = new Map<string, number>();
  for (const f of folios) familyCounts.set(f.family, (familyCounts.get(f.family) ?? 0) + 1);
  console.log('Family sizes (folios with EVA text):');
  for (const [fam, n] of [...familyCounts.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${fam.padEnd(16)} n=${n}`);
  }
  console.log('');

  // Compute all pairwise Jaccard among botanical folios
  console.log('Computing pairwise Jaccard...');
  interface Pair {
    withinFamily: boolean;
    familyA: string;
    familyB: string;
    rankDist: number;
    jac: number;
  }
  const pairs: Pair[] = [];
  for (let i = 0; i < folios.length; i++) {
    for (let j = i + 1; j < folios.length; j++) {
      const a = folios[i], b = folios[j];
      pairs.push({
        withinFamily: a.family === b.family,
        familyA: a.family,
        familyB: b.family,
        rankDist: b.rank - a.rank,  // always positive since j > i
        jac: jaccard(a.words, b.words),
      });
    }
  }
  console.log(`  ${pairs.length} pairs computed`);
  console.log('');

  // Compute quantile breakpoints from all pair distances
  const N_BUCKETS = 5;
  const allDists = pairs.map((p) => p.rankDist);
  const breaks = quantileBreakpoints(allDists, N_BUCKETS);
  const bucketLabels = [
    `[1–${breaks[0]}]`,
    `[${breaks[0]+1}–${breaks[1]}]`,
    `[${breaks[1]+1}–${breaks[2]}]`,
    `[${breaks[2]+1}–${breaks[3]}]`,
    `[${breaks[3]+1}+]`,
  ];
  console.log(`Distance quantile breakpoints (rank units): ${breaks.join(', ')}`);
  console.log('');

  // Aggregate Jaccard by bucket × within/between
  interface BucketStats {
    withinSum: number; withinN: number;
    betweenSum: number; betweenN: number;
  }
  const buckets: BucketStats[] = Array.from({ length: N_BUCKETS }, () => ({
    withinSum: 0, withinN: 0, betweenSum: 0, betweenN: 0,
  }));
  for (const p of pairs) {
    const b = bucketIndex(p.rankDist, breaks);
    if (p.withinFamily) {
      buckets[b].withinSum += p.jac;
      buckets[b].withinN++;
    } else {
      buckets[b].betweenSum += p.jac;
      buckets[b].betweenN++;
    }
  }

  console.log('=== DISTANCE-CONTROLLED JACCARD ===');
  console.log('');
  console.log(
    'Dist bin'.padEnd(16) +
    'Within-fam Jac'.padEnd(18) +
    '(n pairs)'.padEnd(12) +
    'Between-fam Jac'.padEnd(18) +
    '(n pairs)'.padEnd(12) +
    'Lift',
  );
  console.log('-'.repeat(80));
  for (let i = 0; i < N_BUCKETS; i++) {
    const s = buckets[i];
    const within = s.withinN > 0 ? s.withinSum / s.withinN : 0;
    const between = s.betweenN > 0 ? s.betweenSum / s.betweenN : 0;
    const lift = between > 0 ? within / between : 0;
    console.log(
      bucketLabels[i].padEnd(16) +
      within.toFixed(4).padEnd(18) +
      `(${s.withinN})`.padEnd(12) +
      between.toFixed(4).padEnd(18) +
      `(${s.betweenN})`.padEnd(12) +
      lift.toFixed(2) + 'x',
    );
  }
  console.log('');

  // Per-family breakdown for the two largest families
  const BIG_FAMILIES = [...familyCounts.entries()]
    .filter(([, n]) => n >= 10)
    .map(([fam]) => fam);

  if (BIG_FAMILIES.length > 0) {
    console.log('=== PER-FAMILY BREAKDOWN (families n ≥ 10) ===');
    console.log('');

    for (const targetFamily of BIG_FAMILIES) {
      const famPairs = pairs.filter(
        (p) => (p.familyA === targetFamily && p.familyB === targetFamily) ||
               (p.familyA === targetFamily || p.familyB === targetFamily),
      );
      // within = both targetFamily; between = one is targetFamily, other is different botanical
      const withinPairs = famPairs.filter((p) => p.familyA === targetFamily && p.familyB === targetFamily);
      const crossPairs  = famPairs.filter((p) =>
        (p.familyA === targetFamily || p.familyB === targetFamily) && !p.withinFamily,
      );

      const wBuckets: BucketStats[] = Array.from({ length: N_BUCKETS }, () => ({
        withinSum: 0, withinN: 0, betweenSum: 0, betweenN: 0,
      }));
      for (const p of withinPairs) {
        const b = bucketIndex(p.rankDist, breaks);
        wBuckets[b].withinSum += p.jac;
        wBuckets[b].withinN++;
      }
      for (const p of crossPairs) {
        const b = bucketIndex(p.rankDist, breaks);
        wBuckets[b].betweenSum += p.jac;
        wBuckets[b].betweenN++;
      }

      console.log(`  ${targetFamily} (n=${familyCounts.get(targetFamily)}, ${withinPairs.length} within-pairs):`);
      console.log(
        '  ' + 'Dist bin'.padEnd(16) +
        'Within Jac'.padEnd(14) +
        '(n)'.padEnd(8) +
        'Cross Jac'.padEnd(14) +
        '(n)'.padEnd(8) +
        'Lift',
      );
      for (let i = 0; i < N_BUCKETS; i++) {
        const s = wBuckets[i];
        const within = s.withinN > 0 ? s.withinSum / s.withinN : NaN;
        const between = s.betweenN > 0 ? s.betweenSum / s.betweenN : NaN;
        const lift = !isNaN(within) && !isNaN(between) && between > 0 ? within / between : NaN;
        console.log(
          '  ' + bucketLabels[i].padEnd(16) +
          (isNaN(within) ? '  —  ' : within.toFixed(4)).padEnd(14) +
          `(${s.withinN})`.padEnd(8) +
          (isNaN(between) ? '  —  ' : between.toFixed(4)).padEnd(14) +
          `(${s.betweenN})`.padEnd(8) +
          (isNaN(lift) ? '—' : lift.toFixed(2) + 'x'),
        );
      }
      console.log('');
    }
  }

  // Verdict
  console.log('=== VERDICT ===');
  const overallWithin = buckets.reduce((s, b) => s + b.withinSum, 0) /
                        Math.max(1, buckets.reduce((s, b) => s + b.withinN, 0));
  const overallBetween = buckets.reduce((s, b) => s + b.betweenSum, 0) /
                         Math.max(1, buckets.reduce((s, b) => s + b.betweenN, 0));
  const overallLift = overallBetween > 0 ? overallWithin / overallBetween : 0;

  // Check if lift decays: compare Q1 lift vs Q4 lift
  const q1Stats = buckets[0], q4Stats = buckets[N_BUCKETS - 1];
  const q1Lift = q1Stats.betweenN > 0 && q1Stats.withinN > 0
    ? (q1Stats.withinSum / q1Stats.withinN) / (q1Stats.betweenSum / q1Stats.betweenN)
    : 0;
  const q4Lift = q4Stats.betweenN > 0 && q4Stats.withinN > 0
    ? (q4Stats.withinSum / q4Stats.withinN) / (q4Stats.betweenSum / q4Stats.betweenN)
    : 0;
  const liftDecay = q1Lift > 0 ? (q1Lift - q4Lift) / q1Lift : 0;

  console.log(`Overall: within-family Jaccard=${overallWithin.toFixed(4)}, between=${overallBetween.toFixed(4)}, lift=${overallLift.toFixed(2)}x`);
  console.log(`Q1 lift (nearby): ${q1Lift.toFixed(2)}x  |  Q5 lift (distant): ${q4Lift.toFixed(2)}x  |  decay=${(liftDecay * 100).toFixed(0)}%`);
  console.log('');

  if (overallLift < 1.05) {
    console.log('VERDICT: NO WITHIN-FAMILY SIGNAL — family labels do not predict EVA similarity at any distance.');
  } else if (liftDecay > 0.50) {
    console.log('VERDICT: QUIRE-POSITIONAL ARTIFACT — lift decays >50% from Q1→Q5; family clustering is explained by proximity.');
  } else if (liftDecay > 0.25) {
    console.log('VERDICT: MIXED — some quire-positional confound (25–50% decay), some persistent family signal.');
  } else {
    console.log('VERDICT: CONTENT-DRIVEN SIGNAL — within-family lift is roughly flat across manuscript distance.');
    console.log('         This is evidence of semantic/botanical content in EVA vocabulary, not a scribal proximity artifact.');
  }
}

main().catch((err) => {
  console.error('[family-distance-test] fatal:', err);
  process.exit(1);
});

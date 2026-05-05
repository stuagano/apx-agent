/**
 * Species-level vocabulary clustering within solanaceae.
 *
 * We've shown that solanaceae folios share more EVA vocabulary than
 * cross-family pairs (distance-controlled Jaccard, family-distance-test.ts).
 * This test asks whether the clustering goes one level deeper: within
 * solanaceae, do folios depicting the SAME species (mandrake, nightshade,
 * henbane) share more vocabulary than folios depicting DIFFERENT species?
 *
 * If yes → EVA vocabulary encodes botanical identity at species resolution,
 *           not just family level.
 * If no  → the solanaceae signal is family-level only; species labels add
 *           no additional vocabulary structure.
 *
 * Method:
 *   1. Load all solanaceae folios; assign species from subject_candidates
 *      top name (mandrake / mandragora, nightshade / solanum, henbane /
 *      hyoscyamus, etc.)
 *   2. Compute pairwise Jaccard within solanaceae.
 *   3. Compare within-species vs. between-species Jaccard.
 *   4. Permutation test: shuffle species labels among solanaceae folios
 *      N=1000 times; measure how often the within-species lift ≥ observed.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx species-level-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const N_PERMS = parseInt(process.env.N_PERMS ?? '1000');

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
// Helpers
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let inter = 0;
  for (const w of a) if (b.has(w)) inter++;
  const union = a.size + b.size - inter;
  return union === 0 ? 0 : inter / union;
}

function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** Assign species label from subject_candidates top name. */
function assignSpecies(subjectCandidates: string): { species: string; confidence: number } {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return { species: 'unknown', confidence: 0 }; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return { species: 'uncertain', confidence: top?.confidence ?? 0 };

  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();

  // Solanaceae species groups
  if (/mandragora|mandrake/.test(latin + name)) return { species: 'mandrake', confidence: top.confidence };
  if (/hyoscyamus|henbane/.test(latin + name))  return { species: 'henbane',  confidence: top.confidence };
  if (/atropa|belladonna/.test(latin + name))   return { species: 'belladonna', confidence: top.confidence };
  if (/datura|stramonium/.test(latin + name))   return { species: 'datura',   confidence: top.confidence };
  if (/solanum nigrum|nightshade|black night/.test(latin + name)) return { species: 'nightshade', confidence: top.confidence };
  if (/solanum|bittersweet/.test(latin + name)) return { species: 'nightshade', confidence: top.confidence };

  // Fallback: use first word of Latin name as genus
  const genus = latin.split(/[\s/,]/)[0];
  return { species: genus || 'other-solan', confidence: top.confidence };
}

/** Compute mean within-species and between-species Jaccard for a given species label array. */
function computeLift(
  words: Set<string>[],
  species: string[],
): { within: number; between: number; withinN: number; betweenN: number } {
  let withinSum = 0, withinN = 0, betweenSum = 0, betweenN = 0;
  for (let i = 0; i < words.length; i++) {
    for (let j = i + 1; j < words.length; j++) {
      const jac = jaccard(words[i], words[j]);
      if (species[i] === species[j]) {
        withinSum += jac; withinN++;
      } else {
        betweenSum += jac; betweenN++;
      }
    }
  }
  return {
    within:  withinN  > 0 ? withinSum  / withinN  : 0,
    between: betweenN > 0 ? betweenSum / betweenN : 0,
    withinN,
    betweenN,
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('species-level-test — within-solanaceae species vocabulary clustering');
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

  const evaMap = new Map<string, Set<string>>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, new Set(extractEvaWords(r.eva_text)));
  }

  // Filter to solanaceae folios with species assignment and EVA text
  interface SolanFolio {
    folioId: string;
    species: string;
    confidence: number;
    words: Set<string>;
  }

  const folios: SolanFolio[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id);
    if (!words || words.size < 10) continue;

    // Check if solanaceae
    let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
    try { candidates = JSON.parse(r.subject_candidates ?? '[]'); } catch { continue; }
    const top = candidates[0];
    if (!top || top.confidence < 0.35) continue;

    const latin = (top.latin ?? '').toLowerCase();
    const name  = (top.name ?? '').toLowerCase();
    const isSolan = /mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name);
    if (!isSolan) continue;

    const { species, confidence } = assignSpecies(r.subject_candidates ?? '[]');
    folios.push({ folioId: r.folio_id, species, confidence, words });
  }

  console.log(`  ${folios.length} solanaceae folios with EVA text\n`);

  // Species distribution
  const speciesCounts = new Map<string, number>();
  for (const f of folios) speciesCounts.set(f.species, (speciesCounts.get(f.species) ?? 0) + 1);
  console.log('Species distribution:');
  for (const [sp, n] of [...speciesCounts.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${sp.padEnd(16)} n=${n}`);
  }
  console.log('');

  // Filter to species with n ≥ 2
  const validSpecies = new Set([...speciesCounts.entries()].filter(([, n]) => n >= 2).map(([sp]) => sp));
  const filtered = folios.filter((f) => validSpecies.has(f.species));
  console.log(`  ${filtered.length} folios in species with n ≥ 2 (${filtered.length - folios.length} dropped)`);
  console.log('');

  if (filtered.length < 4) {
    console.log('Insufficient folios for analysis.');
    return;
  }

  const wordSets   = filtered.map((f) => f.words);
  const speciesArr = filtered.map((f) => f.species);

  // Real within/between Jaccard
  const real = computeLift(wordSets, speciesArr);
  const realLift = real.between > 0 ? real.within / real.between : 0;

  console.log('=== OBSERVED ===');
  console.log(`Within-species Jaccard:  ${real.within.toFixed(4)}  (${real.withinN} pairs)`);
  console.log(`Between-species Jaccard: ${real.between.toFixed(4)}  (${real.betweenN} pairs)`);
  console.log(`Lift:                    ${realLift.toFixed(3)}x`);
  console.log('');

  // Per-species within-Jaccard
  console.log('Per-species within-Jaccard:');
  for (const sp of validSpecies) {
    const spFolios = filtered.filter((f) => f.species === sp);
    if (spFolios.length < 2) continue;
    let sum = 0, n = 0;
    for (let i = 0; i < spFolios.length; i++) {
      for (let j = i + 1; j < spFolios.length; j++) {
        sum += jaccard(spFolios[i].words, spFolios[j].words);
        n++;
      }
    }
    console.log(`  ${sp.padEnd(16)} n=${spFolios.length}  mean_Jaccard=${n > 0 ? (sum/n).toFixed(4) : 'n/a'}  (${n} pairs)`);
  }
  console.log('');

  // Permutation test: shuffle species labels
  console.log(`=== PERMUTATION TEST (N=${N_PERMS}) ===`);
  console.log('Shuffling species labels among solanaceae folios...');

  const permLifts: number[] = [];
  let atLeastReal = 0;

  for (let p = 0; p < N_PERMS; p++) {
    const permSpecies = shuffle(speciesArr);
    const perm = computeLift(wordSets, permSpecies);
    const permLift = perm.between > 0 ? perm.within / perm.between : 0;
    permLifts.push(permLift);
    if (permLift >= realLift) atLeastReal++;
    if ((p + 1) % 250 === 0) process.stdout.write(`  ${p + 1}/${N_PERMS}\r`);
  }
  process.stdout.write('\n');

  const permMean = permLifts.reduce((s, x) => s + x, 0) / N_PERMS;
  const permMax  = Math.max(...permLifts);
  const permMin  = Math.min(...permLifts);
  const pValue   = atLeastReal / N_PERMS;

  // Distribution histogram (lift values)
  const bins = 10;
  const binMin = Math.min(permMin, realLift);
  const binMax = Math.max(permMax, realLift);
  const binW = (binMax - binMin) / bins;
  const hist = new Array(bins).fill(0);
  for (const l of permLifts) {
    const b = Math.min(bins - 1, Math.floor((l - binMin) / binW));
    hist[b]++;
  }

  console.log('');
  console.log('Permutation lift distribution:');
  const histMax = Math.max(...hist);
  for (let b = 0; b < bins; b++) {
    const lo = binMin + b * binW;
    const hi = lo + binW;
    const bar = '█'.repeat(Math.round((hist[b] / histMax) * 25));
    const realMarker = realLift >= lo && realLift < hi + 0.0001 ? ' ← real' : '';
    console.log(`  [${lo.toFixed(3)}–${hi.toFixed(3)}]  ${String(hist[b]).padEnd(5)} ${bar}${realMarker}`);
  }
  console.log('');

  console.log(`Permutation mean lift: ${permMean.toFixed(3)}x`);
  console.log(`Permutation max lift:  ${permMax.toFixed(3)}x`);
  console.log(`Real lift:             ${realLift.toFixed(3)}x`);
  console.log(`Permutations ≥ real:   ${atLeastReal}/${N_PERMS}`);
  console.log(`Empirical p-value:     ${pValue.toFixed(4)}${pValue === 0 ? ` (p < ${(1/N_PERMS).toFixed(4)})` : ''}`);
  console.log('');

  if (pValue < 0.01) {
    console.log('VERDICT: p < 0.01 — species-level clustering within solanaceae is statistically significant.');
    console.log('         EVA vocabulary encodes botanical identity at sub-family (species) resolution.');
  } else if (pValue < 0.05) {
    console.log('VERDICT: p < 0.05 — significant. Species-level vocabulary structure exists within solanaceae.');
  } else if (pValue < 0.15) {
    console.log(`VERDICT: p = ${pValue.toFixed(3)} — trend but not significant. Borderline species-level signal.`);
    console.log('         Insufficient folio count for definitive result (need more per-species folios).');
  } else {
    console.log(`VERDICT: p = ${pValue.toFixed(3)} — no species-level vocabulary structure within solanaceae.`);
    console.log('         The solanaceae signal is family-level only.');
  }
}

main().catch((err) => {
  console.error('[species-level-test] fatal:', err);
  process.exit(1);
});

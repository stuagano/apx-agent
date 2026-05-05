/**
 * Comprehensive morphological grid scan — all features × all family pairs.
 *
 * Tests every feature in stat-types.ts against every viable botanical family
 * (vs all-botanical AND pairwise vs each other family). Produces a ranked table
 * of effect sizes and permutation p-values.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx morpho-grid-scan.ts
 *
 * Optional:
 *   N_PERMS=500 npx tsx morpho-grid-scan.ts           # faster, less precise p-values
 *   MIN_R=0.2 npx tsx morpho-grid-scan.ts             # only show |r| >= threshold
 *   PERSIST=1 npx tsx morpho-grid-scan.ts             # save significant results to stat_findings table
 *   BATCH_LABEL=grid-scan-v1 npx tsx morpho-grid-scan.ts  # custom batch label
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import {
  extractEvaWords, classifyPlantFamily, rankBiserial, shuffle,
  computeFeature, BOTANICAL_FAMILIES,
} from './stat-types.ts';

const N_PERMS     = parseInt(process.env.N_PERMS ?? '1000');
const MIN_R       = parseFloat(process.env.MIN_R ?? '0.0');
const PERSIST     = process.env.PERSIST === '1';
const BATCH_LABEL = process.env.BATCH_LABEL ?? `grid-scan-${new Date().toISOString().slice(0, 10)}`;
const STAT_TABLE  = process.env.STAT_POPULATION_TABLE ?? 'serverless_stable_qh44kx_catalog.voynich.stat_findings';

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
// Persist significant result to stat_findings table
// ---------------------------------------------------------------------------

async function persistResult(row: {
  feature: string; family_a: string; family_b: string;
  nA: number; nB: number; r: number; p: number;
}, generation: number): Promise<void> {
  const sqlEsc = (s: string) => s.replace(/\\/g, '\\\\').replace(/'/g, "''");
  const id = crypto.randomUUID();
  const spec = {
    feature: row.feature, method: 'permutation-test',
    family_a: row.family_a, family_b: row.family_b,
    rationale: `Grid scan systematic test — ${row.feature} ${row.family_a} vs ${row.family_b}`,
  };
  const finding = {
    spec, effect_size: Math.round(row.r * 1000) / 1000,
    p_value: Math.round(row.p * 1000) / 1000,
    n_samples: row.nA + row.nB,
    interpretation: `${row.family_a} shows ${row.r > 0 ? 'higher' : 'lower'} ${row.feature} than ${row.family_b} (r=${row.r.toFixed(3)}, p=${row.p.toFixed(3)}, n_a=${row.nA}, n_b=${row.nB}). Method: permutation-test (${N_PERMS} permutations).`,
  };
  const specJson  = sqlEsc(JSON.stringify(spec));
  const findJson  = sqlEsc(JSON.stringify(finding));
  const feedback  = sqlEsc(`Grid scan: r=${row.r.toFixed(3)}, p=${row.p.toFixed(3)}, n=${row.nA}+${row.nB}`);
  const criticScore = row.p < 0.001 ? 0.65 : row.p < 0.01 ? 0.55 : 0.45;
  await executeSql(`
    CREATE TABLE IF NOT EXISTS ${STAT_TABLE} (
      id STRING, generation INT, batch_label STRING, spec STRING,
      finding STRING, critic_score DOUBLE, critic_feedback STRING, created_at TIMESTAMP
    )
  `);
  await executeSql(`
    INSERT INTO ${STAT_TABLE} (id, generation, batch_label, spec, finding, critic_score, critic_feedback, created_at)
    VALUES ('${id}', ${generation}, '${BATCH_LABEL}', '${specJson}', '${findJson}', ${criticScore}, '${feedback}', current_timestamp())
  `);
}

// ---------------------------------------------------------------------------
// Permutation p-value
// ---------------------------------------------------------------------------

function permutationP(
  pool: Array<{ family: string; value: number }>,
  familyA: string,
  familyB: string,
  realR: number,
): number {
  let nAbove = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const shuffled = shuffle(pool.map((f) => f.family));
    const gA = pool.filter((_, j) => shuffled[j] === familyA).map((f) => f.value);
    const gB = familyB === 'all-botanical'
      ? pool.filter((_, j) => shuffled[j] !== familyA).map((f) => f.value)
      : pool.filter((_, j) => shuffled[j] === familyB).map((f) => f.value);
    if (Math.abs(rankBiserial(gA, gB)) >= Math.abs(realR)) nAbove++;
  }
  return nAbove / N_PERMS;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const ALL_FEATURES = [
  'qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate',
  'short word rate', 'unique word ratio', 'word entropy',
  'oq-prefix rate', '-ol suffix rate', '-ain suffix rate',
  '-aiin suffix rate', 'sh-init rate', 'ok-prefix rate',
  'mean word length', 'long word rate',
];

// Only families with ≥3 botanical herbal folios in the corpus
const VIABLE_FAMILIES = [
  'solanaceae', 'thistle', 'plantago', 'poppy', 'lily-family', 'mint-family', 'artemisia',
];

interface GridResult {
  feature: string;
  family_a: string;
  family_b: string;
  nA: number;
  nB: number;
  r: number;
  p: number;
  sig: string;
}

async function main() {
  console.log('Loading corpus from Databricks...');
  const [visionRows, evaRows] = await Promise.all([
    executeSql(`
      SELECT folio_id, subject_candidates
      FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
      WHERE section = 'herbal' ORDER BY folio_id
    `),
    executeSql(`
      SELECT folio_id, eva_text
      FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
      WHERE section = 'herbal' ORDER BY folio_id
    `),
  ]);

  const evaMap = new Map<string, string[]>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
  }

  interface FolioEntry { family: string; words: string[]; }
  const corpus: FolioEntry[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;
    corpus.push({ family, words });
  }

  const familyCounts = new Map<string, number>();
  for (const { family } of corpus) familyCounts.set(family, (familyCounts.get(family) ?? 0) + 1);
  console.log(`\nCorpus: ${corpus.length} botanical folios`);
  for (const fam of VIABLE_FAMILIES) {
    console.log(`  ${fam}: n=${familyCounts.get(fam) ?? 0}`);
  }
  console.log(`\nRunning ${N_PERMS} permutations per test...\n`);

  const results: GridResult[] = [];
  let testCount = 0;

  for (const feature of ALL_FEATURES) {
    // Pre-compute feature values for all folios
    const folios = corpus.flatMap(({ family, words }) => {
      const value = computeFeature(words, feature);
      return value === null ? [] : [{ family, value }];
    });

    // Tests: each viable family vs all-botanical
    for (const familyA of VIABLE_FAMILIES) {
      const groupA = folios.filter(f => f.family === familyA).map(f => f.value);
      const groupB = folios.filter(f => f.family !== familyA).map(f => f.value);
      if (groupA.length < 3 || groupB.length < 3) continue;

      const r = rankBiserial(groupA, groupB);
      if (Math.abs(r) < MIN_R && MIN_R > 0) { testCount++; continue; }
      const p = permutationP(folios, familyA, 'all-botanical', r);
      const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : '';
      results.push({ feature, family_a: familyA, family_b: 'all-botanical', nA: groupA.length, nB: groupB.length, r, p, sig });
      testCount++;
    }

    // Tests: each viable family pair directly
    for (let i = 0; i < VIABLE_FAMILIES.length; i++) {
      for (let j = i + 1; j < VIABLE_FAMILIES.length; j++) {
        const familyA = VIABLE_FAMILIES[i];
        const familyB = VIABLE_FAMILIES[j];
        const groupA = folios.filter(f => f.family === familyA).map(f => f.value);
        const groupB = folios.filter(f => f.family === familyB).map(f => f.value);
        if (groupA.length < 3 || groupB.length < 3) continue;

        const pool = [...folios.filter(f => f.family === familyA), ...folios.filter(f => f.family === familyB)];
        const r = rankBiserial(groupA, groupB);
        if (Math.abs(r) < MIN_R && MIN_R > 0) { testCount++; continue; }
        const p = permutationP(pool, familyA, familyB, r);
        const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : '';
        results.push({ feature, family_a: familyA, family_b: familyB, nA: groupA.length, nB: groupB.length, r, p, sig });
        testCount++;
      }
    }

    process.stdout.write(`  ${feature}: ${testCount} tests done so far\r`);
  }

  console.log(`\n\nTotal tests: ${testCount}`);

  // Sort by |r| descending, then by p ascending
  results.sort((a, b) => Math.abs(b.r) - Math.abs(a.r) || a.p - b.p);

  // Print significant results first, then top non-sig
  const sig = results.filter(r => r.p < 0.05);
  const nonsig = results.filter(r => r.p >= 0.05).slice(0, 10);

  console.log('\n=== SIGNIFICANT RESULTS (p < 0.05, sorted by |r|) ===\n');
  const header = 'feature                  | family_a      | family_b      | nA  | nB  |      r |      p | sig';
  const sep    = '-'.repeat(header.length);
  console.log(header);
  console.log(sep);
  for (const row of sig) {
    console.log(
      `${row.feature.padEnd(25)}| ${row.family_a.padEnd(14)}| ${row.family_b.padEnd(14)}| ${String(row.nA).padStart(3)} | ${String(row.nB).padStart(3)} | ${row.r >= 0 ? '+' : ''}${row.r.toFixed(3)} | ${row.p.toFixed(3)} | ${row.sig}`,
    );
  }

  if (nonsig.length > 0) {
    console.log('\n=== TOP NON-SIGNIFICANT (by |r|) ===\n');
    console.log(header);
    console.log(sep);
    for (const row of nonsig) {
      console.log(
        `${row.feature.padEnd(25)}| ${row.family_a.padEnd(14)}| ${row.family_b.padEnd(14)}| ${String(row.nA).padStart(3)} | ${String(row.nB).padStart(3)} | ${row.r >= 0 ? '+' : ''}${row.r.toFixed(3)} | ${row.p.toFixed(3)} | ${row.sig}`,
      );
    }
  }

  // Summary by family
  console.log('\n=== SIGNIFICANT RESULTS BY FAMILY ===\n');
  for (const fam of VIABLE_FAMILIES) {
    const famResults = sig.filter(r => r.family_a === fam);
    if (famResults.length === 0) continue;
    console.log(`${fam} (${famResults.length} significant):`);
    for (const row of famResults.slice(0, 5)) {
      console.log(`  ${row.feature.padEnd(25)} vs ${row.family_b.padEnd(14)} r=${row.r >= 0 ? '+' : ''}${row.r.toFixed(3)} p=${row.p.toFixed(3)} ${row.sig}`);
    }
  }

  // Anti-correlation summary: features where two families go in opposite directions
  console.log('\n=== ANTI-CORRELATIONS (same feature, opposite directions, both p<0.05) ===\n');
  const sigByFeature = new Map<string, GridResult[]>();
  for (const r of sig) {
    if (!sigByFeature.has(r.feature)) sigByFeature.set(r.feature, []);
    sigByFeature.get(r.feature)!.push(r);
  }
  for (const [feature, rows] of sigByFeature) {
    const vsAll = rows.filter(r => r.family_b === 'all-botanical');
    // Find pairs where r goes in opposite directions
    for (let i = 0; i < vsAll.length; i++) {
      for (let j = i + 1; j < vsAll.length; j++) {
        if (vsAll[i].r * vsAll[j].r < 0) {
          console.log(`  ${feature}: ${vsAll[i].family_a} r=${vsAll[i].r >= 0 ? '+' : ''}${vsAll[i].r.toFixed(3)} ${vsAll[i].sig} | ${vsAll[j].family_a} r=${vsAll[j].r >= 0 ? '+' : ''}${vsAll[j].r.toFixed(3)} ${vsAll[j].sig}`);
        }
      }
    }
  }

  // Persist to stat_findings table if requested
  if (PERSIST) {
    const toSave = sig.filter(r => Math.abs(r.r) >= 0.2);
    console.log(`\nPersisting ${toSave.length} significant results (|r|≥0.2) to ${STAT_TABLE}...`);
    // Determine next generation number
    const genRows = await executeSql(`SELECT MAX(generation) AS maxgen FROM ${STAT_TABLE}`).catch(() => []);
    const generation = (parseInt(genRows[0]?.maxgen ?? '0') || 0) + 1;
    let saved = 0;
    for (const row of toSave) {
      try {
        await persistResult(row, generation);
        saved++;
      } catch (err) {
        console.warn(`  Failed to save ${row.feature} ${row.family_a} vs ${row.family_b}: ${(err as Error).message}`);
      }
    }
    console.log(`Saved ${saved}/${toSave.length} findings as generation ${generation}, batch "${BATCH_LABEL}".`);
  } else {
    console.log('\n(Run with PERSIST=1 to save significant results to stat_findings table.)');
  }
}

main().catch(console.error);

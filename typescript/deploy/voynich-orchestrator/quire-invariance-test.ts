/**
 * Quire-invariance test for multi-family morphological fingerprints.
 *
 * morpho-distance-test.ts established that the solanaceae qo-prefix fingerprint
 * is stable across 8/9 mixed quires (r=−0.003 between folio distance and qo-diff).
 * This rules out a scribal-hand artifact for solanaceae.
 *
 * This script applies the same per-quire breakdown test to:
 *   - Thistle: qo-prefix, short word rate
 *   - Plantago: ch-init, -dy, -chy
 *
 * If a fingerprint is concentrated in 1-2 quires (one scribe wrote all thistle
 * folios in that quire with a different morphological habit), a scribal-hand
 * confound cannot be ruled out. If elevated in 4+ quires independently, the
 * content-encoding interpretation is supported.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx quire-invariance-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, computeFeature, BOTANICAL_FAMILIES } from './stat-types.ts';

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

function mean(arr: number[]): number {
  return arr.length === 0 ? 0 : arr.reduce((a, b) => a + b, 0) / arr.length;
}

// Approximate quire from folio rank (8 folios per quire is conventional approximation)
function quireFromRank(rank: number): number {
  return Math.floor((rank - 1) / 8) + 1;
}

async function main() {
  console.log('Loading corpus...');
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

  interface FolioData {
    folio_id: string; rank: number; quire: number; family: string;
    words: string[];
  }
  const corpus: FolioData[] = [];
  let rank = 0;
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    rank++;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;
    corpus.push({ folio_id: r.folio_id, rank, quire: quireFromRank(rank), family, words });
  }

  console.log(`Corpus: ${corpus.length} botanical folios across ${new Set(corpus.map(f => f.quire)).size} quires\n`);

  // Tests: family fingerprint × feature, per-quire breakdown
  const TESTS: Array<{ family: string; feature: string; expectedDir: '+' | '-' }> = [
    { family: 'thistle',    feature: 'qo-prefix rate',    expectedDir: '-' },
    { family: 'thistle',    feature: 'short word rate',    expectedDir: '+' },
    { family: 'thistle',    feature: 'unique word ratio',  expectedDir: '+' },
    { family: 'plantago',   feature: 'ch-init rate',       expectedDir: '+' },
    { family: 'plantago',   feature: '-dy suffix rate',    expectedDir: '-' },
    { family: 'plantago',   feature: '-chy suffix rate',   expectedDir: '+' },
    { family: 'solanaceae', feature: 'qo-prefix rate',     expectedDir: '+' }, // known — positive control
    // New family fingerprints from grid-scan-v1
    { family: 'poppy',      feature: '-aiin suffix rate',  expectedDir: '+' },
    { family: 'poppy',      feature: 'long word rate',     expectedDir: '-' },
    { family: 'lily-family', feature: '-ain suffix rate',  expectedDir: '+' },
    { family: 'lily-family', feature: 'sh-init rate',      expectedDir: '+' },
    { family: 'artemisia',  feature: 'short word rate',    expectedDir: '-' },
    { family: 'artemisia',  feature: 'ok-prefix rate',     expectedDir: '-' },
  ];

  for (const { family, feature, expectedDir } of TESTS) {
    console.log(`\n=== ${family.toUpperCase()} — ${feature} (expected: ${expectedDir}) ===`);

    // Get all quires with at least 1 target family AND 1 other botanical folio
    const targetFolios  = corpus.filter(f => f.family === family);
    const otherFolios   = corpus.filter(f => f.family !== family);
    const mixedQuires   = new Set(targetFolios.map(f => f.quire))
      .values()
      // only quires that also have at least 1 other botanical
      [Symbol.iterator] !== undefined
        ? [...new Set(targetFolios.map(f => f.quire))].filter(q =>
            otherFolios.some(f => f.quire === q))
        : [];

    if (mixedQuires.length === 0) {
      console.log(`  No mixed quires found.`);
      continue;
    }

    let quiresAboveExpected = 0;
    let quiresBelowExpected = 0;

    console.log(`  Quire | n_${family.slice(0,5)} | n_other | mean_target | mean_other | diff`);
    console.log(`  ${'─'.repeat(65)}`);

    for (const q of mixedQuires.sort((a, b) => a - b)) {
      const targetInQ = targetFolios.filter(f => f.quire === q);
      const otherInQ  = otherFolios.filter(f => f.quire === q);

      const targetVals = targetInQ
        .map(f => computeFeature(f.words, feature))
        .filter((v): v is number => v !== null);
      const otherVals  = otherInQ
        .map(f => computeFeature(f.words, feature))
        .filter((v): v is number => v !== null);

      if (targetVals.length === 0 || otherVals.length === 0) continue;

      const mT = mean(targetVals);
      const mO = mean(otherVals);
      const diff = mT - mO;
      const matchesExpected = expectedDir === '+' ? diff > 0 : diff < 0;
      if (matchesExpected) quiresAboveExpected++;
      else quiresBelowExpected++;

      const marker = matchesExpected ? '✓' : '✗';
      console.log(`  Q${String(q).padStart(2)} | ${String(targetVals.length).padStart(7)} | ${String(otherVals.length).padStart(7)} | ${mT.toFixed(4).padStart(11)} | ${mO.toFixed(4).padStart(10)} | ${diff >= 0 ? '+' : ''}${diff.toFixed(4)} ${marker}`);
    }

    const total = quiresAboveExpected + quiresBelowExpected;
    const rate = total > 0 ? quiresAboveExpected / total : 0;
    console.log(`\n  Quires in expected direction: ${quiresAboveExpected}/${total} (${(rate * 100).toFixed(0)}%)`);
    if (total >= 4) {
      const verdict = rate >= 0.75
        ? '→ CONSISTENT: fingerprint appears in ≥75% of mixed quires (scribal-hand confound unlikely)'
        : rate <= 0.25
        ? '→ ANTI-CONSISTENT: fingerprint reversed in most quires (re-examine effect)'
        : '→ MIXED: fingerprint not consistent across quires (weaker evidence for content encoding)';
      console.log(`  ${verdict}`);
    } else {
      console.log(`  (Too few mixed quires for strong verdict — ${total} quires)`);
    }
  }

  console.log('\nDone.');
}

main().catch(console.error);

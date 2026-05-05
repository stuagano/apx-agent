/**
 * Family attribution test — can EVA morphology predict plant family?
 *
 * Uses leave-one-out cross-validation with a nearest-centroid classifier
 * to test whether EVA text features alone can classify botanical folios
 * by plant family. If classification accuracy is significantly above chance,
 * this directly implies that EVA morphology encodes botanical content.
 *
 * Method:
 *   1. For each folio, compute all confirmed morphological features
 *      (qo-prefix, -chy, -dy, ch-init, short word, unique word ratio)
 *   2. For each fold: exclude target folio, compute class centroids from rest
 *   3. Classify target folio by minimum Euclidean distance to centroid
 *   4. Compare accuracy to chance baseline via permutation test
 *
 * Nearest-centroid has no hyperparameters and cannot overfit the training set —
 * making leave-one-out classification valid for small samples.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx family-attribution-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import {
  extractEvaWords, classifyPlantFamily, computeFeature,
  shuffle, BOTANICAL_FAMILIES,
} from './stat-types.ts';

const N_PERMS = parseInt(process.env.N_PERMS ?? '2000');

// Families with enough folios to be included in the classifier
const MIN_FAMILY_N = 5;

// Features to include (all confirmed as discriminative vs all-botanical)
const FEATURES = [
  'qo-prefix rate',
  '-chy suffix rate',
  '-dy suffix rate',
  'ch-init rate',
  'short word rate',
  'unique word ratio',
];

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
  return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
}

function std(arr: number[]): number {
  if (arr.length < 2) return 0;
  const m = mean(arr);
  return Math.sqrt(arr.reduce((s, x) => s + (x - m) ** 2, 0) / arr.length);
}

function euclidean(a: number[], b: number[]): number {
  return Math.sqrt(a.reduce((s, x, i) => s + (x - b[i]) ** 2, 0));
}

// ---------------------------------------------------------------------------
// Nearest-centroid classifier with leave-one-out cross-validation
// ---------------------------------------------------------------------------

interface FolioRecord {
  folio_id: string;
  family: string;
  features: number[];
}

function runLOOClassification(folios: FolioRecord[], families: string[]): {
  accuracy: number;
  correct: number;
  total: number;
  confusionMatrix: Record<string, Record<string, number>>;
} {
  let correct = 0;
  const confusionMatrix: Record<string, Record<string, number>> = {};
  for (const f of families) {
    confusionMatrix[f] = {};
    for (const g of families) confusionMatrix[f][g] = 0;
  }

  for (let i = 0; i < folios.length; i++) {
    const target = folios[i];
    const train = folios.filter((_, j) => j !== i);

    // Compute per-family centroids from training folios
    const centroids: Record<string, number[]> = {};
    for (const family of families) {
      const familyTrain = train.filter(f => f.family === family);
      if (familyTrain.length === 0) continue;
      centroids[family] = FEATURES.map((_, fi) =>
        mean(familyTrain.map(f => f.features[fi]))
      );
    }

    // Classify by nearest centroid
    let bestFamily = '';
    let bestDist = Infinity;
    for (const [family, centroid] of Object.entries(centroids)) {
      const dist = euclidean(target.features, centroid);
      if (dist < bestDist) { bestDist = dist; bestFamily = family; }
    }

    if (bestFamily === target.family) correct++;
    if (bestFamily && target.family) {
      confusionMatrix[target.family][bestFamily] = (confusionMatrix[target.family][bestFamily] ?? 0) + 1;
    }
  }

  return {
    accuracy: correct / folios.length,
    correct,
    total: folios.length,
    confusionMatrix,
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log('Family attribution test — LOO nearest-centroid classification\n');

  // Load corpus
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

  const rawFolios: FolioRecord[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;
    const features = FEATURES.map(f => computeFeature(words, f) ?? 0);
    rawFolios.push({ folio_id: r.folio_id, family, features });
  }

  // Family counts
  const familyCounts = new Map<string, number>();
  for (const { family } of rawFolios) familyCounts.set(family, (familyCounts.get(family) ?? 0) + 1);

  console.log(`Raw corpus: ${rawFolios.length} botanical folios`);
  for (const [family, n] of [...familyCounts.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${family.padEnd(16)}: n=${n}${n < MIN_FAMILY_N ? ' (excluded — too few)' : ''}`);
  }

  // Filter to families with enough folios
  const eligibleFamilies = [...familyCounts.entries()]
    .filter(([, n]) => n >= MIN_FAMILY_N)
    .map(([f]) => f)
    .sort();
  const folios = rawFolios.filter(f => eligibleFamilies.includes(f.family));

  console.log(`\nClassifier: ${eligibleFamilies.length} families, ${folios.length} folios`);
  console.log(`Families: ${eligibleFamilies.join(', ')}`);
  console.log(`Features (${FEATURES.length}): ${FEATURES.join(', ')}\n`);

  // Z-score normalization (prevent high-variance features from dominating)
  const featureStats = FEATURES.map((_, fi) => {
    const vals = folios.map(f => f.features[fi]);
    return { mean: mean(vals), std: std(vals) || 1 };
  });

  const normalizedFolios: FolioRecord[] = folios.map(f => ({
    ...f,
    features: f.features.map((v, fi) => (v - featureStats[fi].mean) / featureStats[fi].std),
  }));

  // Chance baseline
  const chanceBaseline = 1 / eligibleFamilies.length;
  const majorityCounts = [...familyCounts.entries()].filter(([f]) => eligibleFamilies.includes(f));
  const majorityBaseline = Math.max(...majorityCounts.map(([, n]) => n)) / folios.length;

  // Run LOO classification
  console.log('Running leave-one-out cross-validation...');
  const { accuracy, correct, total, confusionMatrix } = runLOOClassification(normalizedFolios, eligibleFamilies);

  console.log(`\nLOO Accuracy: ${correct}/${total} = ${(accuracy * 100).toFixed(1)}%`);
  console.log(`Chance baseline (uniform): ${(chanceBaseline * 100).toFixed(1)}%`);
  console.log(`Majority class baseline:   ${(majorityBaseline * 100).toFixed(1)}%`);

  // Per-family accuracy
  console.log('\nPer-family accuracy:');
  for (const family of eligibleFamilies) {
    const total_f = familyCounts.get(family) ?? 0;
    const correct_f = confusionMatrix[family]?.[family] ?? 0;
    console.log(`  ${family.padEnd(16)}: ${correct_f}/${total_f} = ${(100 * correct_f / (total_f || 1)).toFixed(0)}%`);
  }

  // Confusion matrix
  console.log('\nConfusion matrix (rows=true, cols=predicted):');
  const header = '              | ' + eligibleFamilies.map(f => f.slice(0, 7).padStart(8)).join(' ');
  console.log(header);
  console.log('-'.repeat(header.length));
  for (const trueF of eligibleFamilies) {
    const row = eligibleFamilies.map(predF =>
      String(confusionMatrix[trueF]?.[predF] ?? 0).padStart(8)
    ).join(' ');
    console.log(`${trueF.slice(0, 14).padEnd(14)}| ${row}`);
  }

  // Permutation test for classification accuracy
  console.log(`\nRunning ${N_PERMS} permutation test for accuracy...`);
  let nAbove = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const shuffledLabels = shuffle(normalizedFolios.map(f => f.family));
    const shuffledFolios = normalizedFolios.map((f, i) => ({ ...f, family: shuffledLabels[i] }));
    const { accuracy: permAcc } = runLOOClassification(shuffledFolios, eligibleFamilies);
    if (permAcc >= accuracy) nAbove++;
  }
  const p = nAbove / N_PERMS;
  const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : 'ns';

  console.log(`Permutation p-value: ${p.toFixed(4)} ${sig}`);

  console.log('\n' + '═'.repeat(70));
  if (p < 0.05) {
    const lift = accuracy / chanceBaseline;
    console.log(`RESULT: Classification accuracy ${(accuracy * 100).toFixed(1)}% is significantly above`);
    console.log(`        chance (${(chanceBaseline * 100).toFixed(1)}%, lift=${lift.toFixed(2)}×, p=${p.toFixed(4)} ${sig}).`);
    console.log('');
    console.log('→ EVA morphological features predict plant family above chance.');
    console.log('  This directly implies that EVA text encodes botanical content.');
  } else {
    console.log(`RESULT: Classification accuracy ${(accuracy * 100).toFixed(1)}% is NOT significantly above`);
    console.log(`        chance (${(chanceBaseline * 100).toFixed(1)}%, p=${p.toFixed(4)} ${sig}).`);
    console.log('');
    console.log('→ EVA morphology alone is insufficient to discriminate families via this classifier.');
    console.log('  Per-feature effects may be real while combined classification is below threshold.');
  }
  console.log('═'.repeat(70));

  console.log('\nDone.');
}

main().catch(console.error);

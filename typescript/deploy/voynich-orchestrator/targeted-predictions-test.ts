/**
 * Targeted predictions test — pre-specified falsifiable predictions from FINDINGS.md.
 *
 * All tests in this script were written down as predictions BEFORE execution
 * (see "Predictions from the anti-correlation theory" in FINDINGS.md).
 * This makes them confirmatory rather than exploratory, providing stronger
 * evidence than EA-generated hypotheses.
 *
 * Prediction categories:
 *   A. Direct family-vs-family comparisons (direction predicted from anti-correlation table)
 *   B. Untested features on known families (direction predicted from complementarity)
 *   C. New features on solanaceae/thistle/plantago (direction unknown, informative either way)
 *   D. Null controls (expected no significance — falsifies if they're significant)
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx targeted-predictions-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import {
  extractEvaWords, classifyPlantFamily, computeFeature,
  rankBiserial, shuffle, BOTANICAL_FAMILIES,
} from './stat-types.ts';

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

function permutationP(
  pool: Array<{ family: string; value: number }>,
  familyA: string,
  familyB: string,
  realR: number,
): number {
  // Restrict permutation to the two families being compared (correct null model).
  // For vs all-botanical, permute all labels.
  const permPool = familyB === 'all-botanical'
    ? pool
    : pool.filter(f => f.family === familyA || f.family === familyB);
  let nAbove = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const shuffled = shuffle(permPool.map(f => f.family));
    const gA = permPool.filter((_, j) => shuffled[j] === familyA).map(f => f.value);
    const gB = familyB === 'all-botanical'
      ? permPool.filter((_, j) => shuffled[j] !== familyA).map(f => f.value)
      : permPool.filter((_, j) => shuffled[j] === familyB).map(f => f.value);
    if (Math.abs(rankBiserial(gA, gB)) >= Math.abs(realR)) nAbove++;
  }
  return nAbove / N_PERMS;
}

function mean(arr: number[]): number {
  return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
}

// ---------------------------------------------------------------------------
// Pre-registered predictions
// Each test specifies the EXACT direction expected (or 'any' for new features,
// 'null' for controls expected to be non-significant).
// ---------------------------------------------------------------------------

interface Prediction {
  category: 'A' | 'B' | 'C' | 'D';
  description: string;
  family_a: string;
  family_b: string;
  feature: string;
  expectedDir: '+' | '-' | 'any' | 'null';
  threshold?: number; // minimum |r| predicted (optional)
}

const PREDICTIONS: Prediction[] = [
  // Category A: Direct family-vs-family comparisons
  {
    category: 'A',
    description: 'solanaceae vs thistle — qo-prefix (sol HIGH, thistle LOW)',
    family_a: 'solanaceae', family_b: 'thistle',
    feature: 'qo-prefix rate', expectedDir: '+', threshold: 0.5,
  },
  {
    category: 'A',
    description: 'solanaceae vs plantago — -dy suffix (sol HIGH, plantago LOW)',
    family_a: 'solanaceae', family_b: 'plantago',
    feature: '-dy suffix rate', expectedDir: '+', threshold: 0.3,
  },
  {
    category: 'A',
    description: 'solanaceae vs plantago — -chy suffix (sol LOW, plantago HIGH)',
    family_a: 'solanaceae', family_b: 'plantago',
    feature: '-chy suffix rate', expectedDir: '-', threshold: 0.3,
  },
  {
    category: 'A',
    description: 'solanaceae vs plantago — ch-init (sol LOW, plantago HIGH)',
    family_a: 'solanaceae', family_b: 'plantago',
    feature: 'ch-init rate', expectedDir: '-', threshold: 0.4,
  },
  {
    category: 'A',
    description: 'thistle vs plantago — short word (thistle HIGH, plantago direction unknown)',
    family_a: 'thistle', family_b: 'plantago',
    feature: 'short word rate', expectedDir: '+',
  },

  // Category B: Untested features on known families (direction predicted)
  {
    category: 'B',
    description: 'plantago — qo-prefix vs all-botanical (predicted negative, follows anti-correlation)',
    family_a: 'plantago', family_b: 'all-botanical',
    feature: 'qo-prefix rate', expectedDir: '-',
  },
  {
    category: 'B',
    description: 'thistle — -dy suffix vs all-botanical (predicted neutral or negative)',
    family_a: 'thistle', family_b: 'all-botanical',
    feature: '-dy suffix rate', expectedDir: '-',
  },
  {
    category: 'B',
    description: 'thistle — -chy suffix vs all-botanical (predicted neutral or positive)',
    family_a: 'thistle', family_b: 'all-botanical',
    feature: '-chy suffix rate', expectedDir: '+',
  },

  // Category C: New features — direction unknown, informative either way
  {
    category: 'C',
    description: 'solanaceae — -aiin suffix rate vs all-botanical',
    family_a: 'solanaceae', family_b: 'all-botanical',
    feature: '-aiin suffix rate', expectedDir: 'any',
  },
  {
    category: 'C',
    description: 'solanaceae — mean word length vs all-botanical',
    family_a: 'solanaceae', family_b: 'all-botanical',
    feature: 'mean word length', expectedDir: 'any',
  },
  {
    category: 'C',
    description: 'solanaceae — sh-init rate vs all-botanical',
    family_a: 'solanaceae', family_b: 'all-botanical',
    feature: 'sh-init rate', expectedDir: 'any',
  },
  {
    category: 'C',
    description: 'thistle — -aiin suffix rate vs all-botanical',
    family_a: 'thistle', family_b: 'all-botanical',
    feature: '-aiin suffix rate', expectedDir: 'any',
  },
  {
    category: 'C',
    description: 'plantago — mean word length vs all-botanical',
    family_a: 'plantago', family_b: 'all-botanical',
    feature: 'mean word length', expectedDir: 'any',
  },

  // Category D: Null controls (predicted non-significant)
  {
    category: 'D',
    description: 'solanaceae — oq-prefix vs all-botanical (expected null)',
    family_a: 'solanaceae', family_b: 'all-botanical',
    feature: 'oq-prefix rate', expectedDir: 'null',
  },
  {
    category: 'D',
    description: 'solanaceae — -ain suffix vs all-botanical (expected null)',
    family_a: 'solanaceae', family_b: 'all-botanical',
    feature: '-ain suffix rate', expectedDir: 'null',
  },
];

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log(`Targeted predictions test — ${N_PERMS} permutations per test`);
  console.log(`Pre-registered predictions from FINDINGS.md (anti-correlation theory)\n`);

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
  console.log(`Corpus: ${corpus.length} botanical folios`);
  const mainFamilies = ['solanaceae', 'thistle', 'plantago', 'poppy', 'lily-family', 'artemisia'];
  for (const fam of mainFamilies) {
    console.log(`  ${fam}: n=${familyCounts.get(fam) ?? 0}`);
  }
  console.log();

  // Run tests by category
  const categories: Array<{ label: string; key: 'A' | 'B' | 'C' | 'D'; desc: string }> = [
    { key: 'A', label: 'A', desc: 'Direct family-vs-family (direction pre-predicted)' },
    { key: 'B', label: 'B', desc: 'Untested features on known families (direction predicted)' },
    { key: 'C', label: 'C', desc: 'New features (direction unknown)' },
    { key: 'D', label: 'D', desc: 'Null controls (expected non-significant)' },
  ];

  let totalConfirmed = 0;
  let totalFalsified  = 0;
  let totalNewFindings = 0;
  let totalNullHolds  = 0;
  let totalNullFails  = 0;

  for (const { key, desc } of categories) {
    const preds = PREDICTIONS.filter(p => p.category === key);
    console.log(`\n${'═'.repeat(72)}`);
    console.log(`Category ${key}: ${desc}`);
    console.log('═'.repeat(72));

    for (const pred of preds) {
      // Compute feature values
      const folios = corpus.flatMap(({ family, words }) => {
        const value = computeFeature(words, pred.feature);
        return value === null ? [] : [{ family, value }];
      });

      const groupA = folios.filter(f => f.family === pred.family_a).map(f => f.value);
      const groupB = pred.family_b === 'all-botanical'
        ? folios.filter(f => f.family !== pred.family_a).map(f => f.value)
        : folios.filter(f => f.family === pred.family_b).map(f => f.value);

      if (groupA.length < 3 || groupB.length < 3) {
        console.log(`\n[SKIP] ${pred.description}`);
        console.log(`  Insufficient data: n(${pred.family_a})=${groupA.length}, n(${pred.family_b})=${groupB.length}`);
        continue;
      }

      const r = rankBiserial(groupA, groupB);
      const p = permutationP(folios, pred.family_a, pred.family_b, r);
      const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : 'ns';
      const mA = mean(groupA), mB = mean(groupB);

      // Verdict
      let verdict = '';
      let verdictSymbol = '';
      if (pred.expectedDir === 'null') {
        if (p >= 0.05) { verdict = 'NULL HOLDS — controls clean'; verdictSymbol = '✓'; totalNullHolds++; }
        else           { verdict = 'NULL FAILS — unexpected signal (re-examine)'; verdictSymbol = '⚠'; totalNullFails++; }
      } else if (pred.expectedDir === 'any') {
        if (p < 0.05) { verdict = `NEW FINDING: ${r > 0 ? '+' : ''}${r.toFixed(3)} (p=${p.toFixed(3)} ${sig})`; verdictSymbol = '★'; totalNewFindings++; }
        else          { verdict = `No signal detected (p=${p.toFixed(3)})`; verdictSymbol = '·'; }
      } else {
        const dirMatch = pred.expectedDir === '+' ? r > 0 : r < 0;
        const thresholdMatch = !pred.threshold || Math.abs(r) >= pred.threshold;
        if (p < 0.05 && dirMatch) {
          if (thresholdMatch) {
            verdict = `CONFIRMED: direction ✓${pred.threshold ? `, |r|=${Math.abs(r).toFixed(3)} ≥ threshold ${pred.threshold}` : ''}`;
            verdictSymbol = '✓';
            totalConfirmed++;
          } else {
            verdict = `PARTIAL: direction ✓, but |r|=${Math.abs(r).toFixed(3)} < threshold ${pred.threshold}`;
            verdictSymbol = '~';
            totalConfirmed++;
          }
        } else if (p < 0.05 && !dirMatch) {
          verdict = 'FALSIFIED: significant but wrong direction';
          verdictSymbol = '✗';
          totalFalsified++;
        } else {
          verdict = `Non-significant (direction ${dirMatch ? '✓ correct' : '✗ wrong'}, p=${p.toFixed(3)})`;
          verdictSymbol = dirMatch ? '~' : '✗';
          if (!dirMatch) totalFalsified++;
        }
      }

      console.log(`\n[${key}] ${pred.description}`);
      console.log(`  ${pred.family_a} (n=${groupA.length}) vs ${pred.family_b} (n=${groupB.length})`);
      console.log(`  mean(A)=${mA.toFixed(4)}  mean(B)=${mB.toFixed(4)}  r=${r >= 0 ? '+' : ''}${r.toFixed(4)}  p=${p.toFixed(4)} ${sig}`);
      console.log(`  ${verdictSymbol} ${verdict}`);
    }
  }

  // Summary
  console.log(`\n${'═'.repeat(72)}`);
  console.log('SUMMARY');
  console.log('═'.repeat(72));
  console.log(`\nCategory A+B (directional predictions):`);
  console.log(`  Confirmed:  ${totalConfirmed}`);
  console.log(`  Falsified:  ${totalFalsified}`);
  console.log(`Category C (new features):`);
  console.log(`  New findings: ${totalNewFindings}`);
  console.log(`Category D (null controls):`);
  console.log(`  Null holds: ${totalNullHolds}`);
  console.log(`  Null fails: ${totalNullFails}`);

  const dirPredictions = PREDICTIONS.filter(p => p.category === 'A' || p.category === 'B').length;
  console.log(`\nOverall: ${totalConfirmed}/${dirPredictions} directional predictions confirmed.`);
  if (totalFalsified === 0 && totalConfirmed >= dirPredictions * 0.6) {
    console.log('→ STRONG SUPPORT for anti-correlation theory: majority confirmed, none falsified.');
  } else if (totalFalsified > 0) {
    console.log(`→ ${totalFalsified} prediction(s) falsified — examine these carefully.`);
  } else {
    console.log('→ Inconclusive — too many non-significant results.');
  }

  console.log('\nDone.');
}

main().catch(console.error);

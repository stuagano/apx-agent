/**
 * Feature robustness test — do established findings survive definitional variants?
 *
 * Each confirmed morphological fingerprint is tested with ±variants of its
 * feature definition. If the signal is only present for one specific definition,
 * that's a red flag for post-hoc feature engineering. If it persists across
 * plausible variants, the result is more reliable.
 *
 * Tests established findings (FINDINGS.md):
 *   Solanaceae: qo-prefix (+), -chy suffix (−), -dy suffix (+), ch-init (−), short word (−)
 *   Thistle:    qo-prefix (−), short word (+), unique word ratio (+)
 *   Plantago:   ch-init (+), -dy suffix (−), -chy suffix (+)
 *
 * Each is tested with 3-4 definitional variants spanning the feature space.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx feature-robustness-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import {
  extractEvaWords, classifyPlantFamily, rankBiserial, shuffle, BOTANICAL_FAMILIES,
} from './stat-types.ts';

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

function permutationP(
  pool: Array<{ family: string; value: number }>,
  familyA: string,
  realR: number,
): number {
  // All-botanical null model (shuffles all labels)
  let nAbove = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const shuffled = shuffle(pool.map(f => f.family));
    const gA = pool.filter((_, j) => shuffled[j] === familyA).map(f => f.value);
    const gB = pool.filter((_, j) => shuffled[j] !== familyA).map(f => f.value);
    if (Math.abs(rankBiserial(gA, gB)) >= Math.abs(realR)) nAbove++;
  }
  return nAbove / N_PERMS;
}

function mean(arr: number[]): number {
  return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
}

// ---------------------------------------------------------------------------
// Feature variant definitions
// Each variant is a function: (words: string[]) => number
// ---------------------------------------------------------------------------

type FeatureVariant = {
  name: string;       // label for output
  canonical: boolean; // is this the established definition?
  fn: (words: string[]) => number;
};

interface FeatureGroup {
  featureName: string;   // canonical name
  family: string;
  expectedDir: '+' | '-';
  confirmedEffect: number; // canonical r from FINDINGS.md
  variants: FeatureVariant[];
}

const FEATURE_GROUPS: FeatureGroup[] = [
  // ── Solanaceae ───────────────────────────────────────────────────────────
  {
    featureName: 'qo-prefix (solanaceae, expected +)',
    family: 'solanaceae',
    expectedDir: '+',
    confirmedEffect: 0.532,
    variants: [
      { name: 'q- (any q-word)',         canonical: false, fn: (ws) => ws.filter(w => w.startsWith('q')).length / ws.length },
      { name: 'qo- (canonical)',          canonical: true,  fn: (ws) => ws.filter(w => w.startsWith('qo')).length / ws.length },
      { name: 'qo + ≥2 more chars',      canonical: false, fn: (ws) => ws.filter(w => w.startsWith('qo') && w.length >= 4).length / ws.length },
      { name: 'qok/qol/qod (qo + cons)', canonical: false, fn: (ws) => ws.filter(w => /^qo[kldbfghjmnprstv]/.test(w)).length / ws.length },
    ],
  },
  {
    featureName: '-chy suffix (solanaceae, expected −)',
    family: 'solanaceae',
    expectedDir: '-',
    confirmedEffect: -0.435,
    variants: [
      { name: '-y (any y-ending)',        canonical: false, fn: (ws) => ws.filter(w => w.endsWith('y')).length / ws.length },
      { name: '-hy (h before y)',         canonical: false, fn: (ws) => ws.filter(w => w.endsWith('hy')).length / ws.length },
      { name: '-chy (canonical)',          canonical: true,  fn: (ws) => ws.filter(w => w.endsWith('chy')).length / ws.length },
      { name: '-achy/-ochy (vowel+chy)',  canonical: false, fn: (ws) => ws.filter(w => /[aeiou]chy$/.test(w)).length / ws.length },
    ],
  },
  {
    featureName: '-dy suffix (solanaceae, expected +)',
    family: 'solanaceae',
    expectedDir: '+',
    confirmedEffect: 0.367,
    variants: [
      { name: '-y (any y-ending)',        canonical: false, fn: (ws) => ws.filter(w => w.endsWith('y')).length / ws.length },
      { name: '-dy (canonical)',           canonical: true,  fn: (ws) => ws.filter(w => w.endsWith('dy')).length / ws.length },
      { name: '-edy/-ody (vowel+dy)',     canonical: false, fn: (ws) => ws.filter(w => /[aeiou]dy$/.test(w)).length / ws.length },
      { name: '-dy + -ky combined',      canonical: false, fn: (ws) => ws.filter(w => w.endsWith('dy') || w.endsWith('ky')).length / ws.length },
    ],
  },
  {
    featureName: 'ch-init (solanaceae, expected −)',
    family: 'solanaceae',
    expectedDir: '-',
    confirmedEffect: -0.249,
    variants: [
      { name: 'c- (any c-word)',          canonical: false, fn: (ws) => ws.filter(w => w.startsWith('c')).length / ws.length },
      { name: 'ch- (canonical)',           canonical: true,  fn: (ws) => ws.filter(w => w.startsWith('ch')).length / ws.length },
      { name: 'ch + ≥2 more chars',       canonical: false, fn: (ws) => ws.filter(w => w.startsWith('ch') && w.length >= 4).length / ws.length },
      { name: 'chor/chol/chad (ch+cons)', canonical: false, fn: (ws) => ws.filter(w => /^ch[aolrd]/.test(w)).length / ws.length },
    ],
  },
  {
    featureName: 'short word (solanaceae, expected −)',
    family: 'solanaceae',
    expectedDir: '-',
    confirmedEffect: -0.300,
    variants: [
      { name: '≤2 chars (tight)',         canonical: false, fn: (ws) => ws.filter(w => w.length <= 2).length / ws.length },
      { name: '≤3 chars (canonical)',      canonical: true,  fn: (ws) => ws.filter(w => w.length <= 3).length / ws.length },
      { name: '≤4 chars (relaxed)',        canonical: false, fn: (ws) => ws.filter(w => w.length <= 4).length / ws.length },
    ],
  },

  // ── Thistle ──────────────────────────────────────────────────────────────
  {
    featureName: 'qo-prefix (thistle, expected −)',
    family: 'thistle',
    expectedDir: '-',
    confirmedEffect: -0.340,
    variants: [
      { name: 'q- (any q-word)',         canonical: false, fn: (ws) => ws.filter(w => w.startsWith('q')).length / ws.length },
      { name: 'qo- (canonical)',          canonical: true,  fn: (ws) => ws.filter(w => w.startsWith('qo')).length / ws.length },
      { name: 'qo + ≥2 more chars',      canonical: false, fn: (ws) => ws.filter(w => w.startsWith('qo') && w.length >= 4).length / ws.length },
    ],
  },
  {
    featureName: 'short word (thistle, expected +)',
    family: 'thistle',
    expectedDir: '+',
    confirmedEffect: 0.340,
    variants: [
      { name: '≤2 chars (tight)',         canonical: false, fn: (ws) => ws.filter(w => w.length <= 2).length / ws.length },
      { name: '≤3 chars (canonical)',      canonical: true,  fn: (ws) => ws.filter(w => w.length <= 3).length / ws.length },
      { name: '≤4 chars (relaxed)',        canonical: false, fn: (ws) => ws.filter(w => w.length <= 4).length / ws.length },
    ],
  },
  {
    featureName: 'unique word ratio (thistle, expected +)',
    family: 'thistle',
    expectedDir: '+',
    confirmedEffect: 0.297,
    variants: [
      { name: 'unique/total (canonical)',  canonical: true,  fn: (ws) => new Set(ws).size / ws.length },
      { name: 'hapax rate (once only)',    canonical: false, fn: (ws) => {
        const freq = new Map<string, number>();
        for (const w of ws) freq.set(w, (freq.get(w) ?? 0) + 1);
        return [...freq.values()].filter(c => c === 1).length / ws.length;
      }},
      { name: 'type/token log ratio',     canonical: false, fn: (ws) => {
        const types = new Set(ws).size;
        return types === 0 ? 0 : Math.log(types) / Math.log(ws.length);
      }},
    ],
  },

  // ── Plantago ─────────────────────────────────────────────────────────────
  {
    featureName: 'ch-init (plantago, expected +)',
    family: 'plantago',
    expectedDir: '+',
    confirmedEffect: 0.563,
    variants: [
      { name: 'c- (any c-word)',          canonical: false, fn: (ws) => ws.filter(w => w.startsWith('c')).length / ws.length },
      { name: 'ch- (canonical)',           canonical: true,  fn: (ws) => ws.filter(w => w.startsWith('ch')).length / ws.length },
      { name: 'ch + ≥2 more chars',       canonical: false, fn: (ws) => ws.filter(w => w.startsWith('ch') && w.length >= 4).length / ws.length },
    ],
  },
  {
    featureName: '-dy suffix (plantago, expected −)',
    family: 'plantago',
    expectedDir: '-',
    confirmedEffect: -0.449,
    variants: [
      { name: '-y (any y-ending)',        canonical: false, fn: (ws) => ws.filter(w => w.endsWith('y')).length / ws.length },
      { name: '-dy (canonical)',           canonical: true,  fn: (ws) => ws.filter(w => w.endsWith('dy')).length / ws.length },
      { name: '-edy/-ody (vowel+dy)',     canonical: false, fn: (ws) => ws.filter(w => /[aeiou]dy$/.test(w)).length / ws.length },
    ],
  },
  {
    featureName: '-chy suffix (plantago, expected +)',
    family: 'plantago',
    expectedDir: '+',
    confirmedEffect: 0.402,
    variants: [
      { name: '-y (any y-ending)',        canonical: false, fn: (ws) => ws.filter(w => w.endsWith('y')).length / ws.length },
      { name: '-chy (canonical)',          canonical: true,  fn: (ws) => ws.filter(w => w.endsWith('chy')).length / ws.length },
      { name: '-achy/-ochy (vowel+chy)',  canonical: false, fn: (ws) => ws.filter(w => /[aeiou]chy$/.test(w)).length / ws.length },
    ],
  },
];

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log(`Feature robustness test — ${N_PERMS} permutations per variant\n`);

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
  for (const fam of ['solanaceae', 'thistle', 'plantago']) {
    console.log(`  ${fam}: n=${familyCounts.get(fam) ?? 0}`);
  }
  console.log();

  let totalConsistentSig = 0;
  let totalVariants = 0;
  let totalRobust = 0; // canonical + ≥50% variants significant and same direction

  for (const group of FEATURE_GROUPS) {
    console.log(`\n${'─'.repeat(70)}`);
    console.log(`${group.featureName}`);
    console.log(`  Canonical effect: r=${group.confirmedEffect >= 0 ? '+' : ''}${group.confirmedEffect.toFixed(3)} | Expected direction: ${group.expectedDir}`);
    console.log(`${'─'.repeat(70)}`);

    const foliosWithFamily = corpus.map(({ family, words }) => ({ family, words }));
    const nFamilyA = foliosWithFamily.filter(f => f.family === group.family).length;
    const nOther   = foliosWithFamily.filter(f => f.family !== group.family).length;

    let variantsSigSameDir = 0;
    let canonicalSig = false;
    let canonicalDir = true;

    for (const variant of group.variants) {
      // Compute feature values
      const pool = corpus.flatMap(({ family, words }) => {
        const value = variant.fn(words);
        if (!isFinite(value)) return [];
        return [{ family, value }];
      });

      const groupA = pool.filter(f => f.family === group.family).map(f => f.value);
      const groupB = pool.filter(f => f.family !== group.family).map(f => f.value);

      if (groupA.length < 3 || groupB.length < 3) {
        console.log(`  [SKIP] ${variant.name} — insufficient data`);
        continue;
      }

      const r = rankBiserial(groupA, groupB);
      const p = permutationP(pool, group.family, r);
      const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : 'ns';
      const dirMatch = group.expectedDir === '+' ? r > 0 : r < 0;
      const canonicalTag = variant.canonical ? '[CANONICAL]' : '           ';
      const dirTag = dirMatch ? (p < 0.05 ? '✓' : '~') : '✗';

      console.log(`  ${canonicalTag} ${variant.name.padEnd(30)} r=${r >= 0 ? '+' : ''}${r.toFixed(3)}  p=${p.toFixed(3)} ${sig.padStart(3)}  dir:${dirTag}`);

      totalVariants++;
      if (p < 0.05 && dirMatch) { totalConsistentSig++; variantsSigSameDir++; }
      if (variant.canonical) {
        canonicalSig = p < 0.05;
        canonicalDir = dirMatch;
      }
    }

    const nTotal = group.variants.length;
    const robustness = canonicalSig && variantsSigSameDir >= nTotal * 0.5;
    const verdict = robustness
      ? `ROBUST: ${variantsSigSameDir}/${nTotal} variants sig + same direction`
      : `FRAGILE: only ${variantsSigSameDir}/${nTotal} variants sig (or canonical not sig)`;
    console.log(`\n  ${robustness ? '✓' : '!'} ${verdict}`);
    if (robustness) totalRobust++;
  }

  console.log(`\n${'═'.repeat(70)}`);
  console.log('ROBUSTNESS SUMMARY');
  console.log('═'.repeat(70));
  console.log(`\nFeature groups tested: ${FEATURE_GROUPS.length}`);
  console.log(`Robust findings (canonical sig + ≥50% variants sig + same direction): ${totalRobust}/${FEATURE_GROUPS.length}`);
  console.log(`Variants significant in expected direction: ${totalConsistentSig}/${totalVariants}`);

  if (totalRobust >= FEATURE_GROUPS.length * 0.7) {
    console.log('\n→ ROBUST OVERALL: majority of established findings survive definitional variants.');
    console.log('  The morphological fingerprints are not artifacts of specific feature definitions.');
  } else if (totalRobust >= FEATURE_GROUPS.length * 0.5) {
    console.log('\n→ MODERATELY ROBUST: most findings survive, but some are definition-sensitive.');
    console.log('  Examine fragile features for possible post-hoc selection bias.');
  } else {
    console.log('\n→ FRAGILE: many findings depend on the exact feature definition.');
    console.log('  This is a significant concern — examine whether definitions were pre-specified.');
  }

  console.log('\nDone.');
}

main().catch(console.error);

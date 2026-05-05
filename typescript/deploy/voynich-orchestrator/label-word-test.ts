/**
 * Label-word distributional analysis.
 *
 * Hypothesis: certain EVA words function as plant-name labels or headings —
 * appearing exactly once per folio, concentrated in one botanical family.
 * Examples from NPMI work: oldaiin (86% once-per-folio, solanaceae),
 * qotchor (100%, plantago), keody/dshy (100%, thistle).
 *
 * Test:
 *   For each word appearing on ≥3 folios within a botanical family, compute:
 *     - once_rate  : fraction of occupied family folios with exactly 1 token
 *     - mean_tok   : N / K  (tokens per occupied folio — should be near 1 for labels)
 *     - lor        : log-odds ratio vs. all other herbal folios
 *
 *   Permutation null: given word w appears N times across K family folios,
 *   distribute N tokens randomly (weighted by folio word-count). How often
 *   does null once_rate ≥ observed? → empirical p-value per word.
 *
 *   Label candidate: once_rate ≥ 0.70, mean_tok ≤ 1.5, lor ≥ 1.0, K ≥ 3.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx label-word-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const N_PERMS = parseInt(process.env.N_PERMS ?? '2000');
const MIN_FAMILY_FOLIOS = 3;   // min folios in family for a word to be tested
const ONCE_RATE_THRESH  = 0.70;
const MEAN_TOK_THRESH   = 1.50;
const LOR_THRESH        = 1.00;

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

function extractWordCounts(evaText: string): Map<string, number> {
  const counts = new Map<string, number>();
  const words = evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
  for (const w of words) counts.set(w, (counts.get(w) ?? 0) + 1);
  return counts;
}

/** Multinomial sample: distribute N items across weights array; return count per slot. */
function multinomialSample(n: number, weights: number[], rng: () => number): number[] {
  const total = weights.reduce((s, x) => s + x, 0);
  const cum: number[] = [];
  let acc = 0;
  for (const w of weights) { acc += w / total; cum.push(acc); }
  const counts = new Array(weights.length).fill(0);
  for (let i = 0; i < n; i++) {
    const r = rng();
    let slot = cum.findIndex((c) => r < c);
    if (slot < 0) slot = weights.length - 1;
    counts[slot]++;
  }
  return counts;
}

/** Simple LCG for fast pseudo-random numbers. */
function makeLCG(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(1664525, s) + 1013904223) >>> 0;
    return s / 0xFFFFFFFF;
  };
}

async function main(): Promise<void> {
  console.log('label-word-test — distributional signature of label/heading words in herbal EVA');
  console.log(`N_PERMS=${N_PERMS}, MIN_FAMILY_FOLIOS=${MIN_FAMILY_FOLIOS}`);
  console.log('');

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

  // Per-folio: word counts and family
  interface FolioData { folioId: string; family: string; counts: Map<string, number>; totalWords: number; }
  const folioMap = new Map<string, FolioData>();

  const evaByFolio = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaByFolio.set(r.folio_id, r.eva_text);
  }

  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const evaText = evaByFolio.get(r.folio_id);
    if (!evaText) continue;
    const counts = extractWordCounts(evaText);
    const totalWords = [...counts.values()].reduce((s, x) => s + x, 0);
    if (totalWords < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    folioMap.set(r.folio_id, { folioId: r.folio_id, family, counts, totalWords });
  }

  const allFolios = [...folioMap.values()];
  const botanicalFolios = allFolios.filter((f) => BOTANICAL.has(f.family));
  console.log(`  ${allFolios.length} herbal folios total, ${botanicalFolios.length} botanical`);

  // Corpus-level word frequency (all herbal, for LOR baseline)
  const corpusCount = new Map<string, number>(); // word → n folios it appears on (across all herbal)
  for (const f of allFolios) {
    for (const [w] of f.counts) {
      corpusCount.set(w, (corpusCount.get(w) ?? 0) + 1);
    }
  }
  const totalHerbalFolios = allFolios.length;

  const FAMILIES_TO_TEST = ['solanaceae', 'thistle', 'plantago'] as const;

  interface LabelCandidate {
    word: string;
    family: string;
    K: number;        // folios in family with this word
    N: number;        // total tokens in family
    onceCount: number;
    onceRate: number;
    meanTok: number;
    lor: number;
    pValue: number | null;
  }

  const allCandidates: LabelCandidate[] = [];

  for (const fam of FAMILIES_TO_TEST) {
    const famFolios = botanicalFolios.filter((f) => f.family === fam);
    const otherFolios = allFolios.filter((f) => f.family !== fam);

    console.log(`\n${'='.repeat(60)}`);
    console.log(`FAMILY: ${fam.toUpperCase()}  (${famFolios.length} folios)`);
    console.log('='.repeat(60));

    // For each word in any family folio: compute K, N, onceCount
    const wordStats = new Map<string, { K: number; N: number; onceCount: number; folioCounts: number[]; folioWeights: number[] }>();

    for (const f of famFolios) {
      for (const [w, cnt] of f.counts) {
        if (!wordStats.has(w)) wordStats.set(w, { K: 0, N: 0, onceCount: 0, folioCounts: [], folioWeights: [] });
        const s = wordStats.get(w)!;
        s.K++;
        s.N += cnt;
        if (cnt === 1) s.onceCount++;
        s.folioCounts.push(cnt);
        s.folioWeights.push(f.totalWords);
      }
    }

    // Filter to words with K ≥ MIN_FAMILY_FOLIOS
    const candidates = [...wordStats.entries()]
      .filter(([, s]) => s.K >= MIN_FAMILY_FOLIOS)
      .map(([word, s]) => {
        const onceRate = s.onceCount / s.K;
        const meanTok  = s.N / s.K;

        // LOR: in-family folio prevalence vs. out-family
        const inCount  = s.K;
        const inTotal  = famFolios.length;
        const outFoliosWithWord = otherFolios.filter((f) => f.counts.has(word)).length;
        const outTotal = otherFolios.length;
        const inRate  = (inCount + 0.5) / (inTotal + 1);
        const outRate = (outFoliosWithWord + 0.5) / (outTotal + 1);
        const lor = Math.log(inRate / (1 - inRate)) - Math.log(outRate / (1 - outRate));

        return { word, K: s.K, N: s.N, onceCount: s.onceCount, onceRate, meanTok, lor, folioCounts: s.folioCounts, folioWeights: s.folioWeights };
      })
      .sort((a, b) => b.onceRate - a.onceRate || b.lor - a.lor);

    // Show top 20 by once_rate
    console.log('');
    console.log(`Top words by once_rate (K ≥ ${MIN_FAMILY_FOLIOS} family folios):`);
    console.log('');
    console.log('Word'.padEnd(16) + 'K'.padEnd(6) + 'N'.padEnd(6) + 'once_rate'.padEnd(12) + 'mean_tok'.padEnd(12) + 'LOR'.padEnd(10) + 'Label?');
    console.log('-'.repeat(70));

    let shown = 0;
    for (const c of candidates) {
      if (shown >= 20) break;
      const isLabel = c.onceRate >= ONCE_RATE_THRESH && c.meanTok <= MEAN_TOK_THRESH && c.lor >= LOR_THRESH;
      console.log(
        c.word.padEnd(16) +
        String(c.K).padEnd(6) +
        String(c.N).padEnd(6) +
        c.onceRate.toFixed(3).padEnd(12) +
        c.meanTok.toFixed(3).padEnd(12) +
        c.lor.toFixed(2).padEnd(10) +
        (isLabel ? '*** LABEL' : ''),
      );
      shown++;
    }

    // Also show bottom (running-text words) for contrast
    const bottom = [...candidates].sort((a, b) => a.onceRate - b.onceRate || b.lor - a.lor).slice(0, 8);
    console.log('');
    console.log('Bottom (running-text words — low once_rate):');
    console.log('Word'.padEnd(16) + 'K'.padEnd(6) + 'N'.padEnd(6) + 'once_rate'.padEnd(12) + 'mean_tok'.padEnd(12) + 'LOR');
    console.log('-'.repeat(62));
    for (const c of bottom) {
      console.log(
        c.word.padEnd(16) +
        String(c.K).padEnd(6) +
        String(c.N).padEnd(6) +
        c.onceRate.toFixed(3).padEnd(12) +
        c.meanTok.toFixed(3).padEnd(12) +
        c.lor.toFixed(2),
      );
    }

    // Permutation test for label candidates
    const labelCandidates = candidates.filter((c) =>
      c.onceRate >= ONCE_RATE_THRESH && c.meanTok <= MEAN_TOK_THRESH && c.lor >= LOR_THRESH
    );

    if (labelCandidates.length > 0) {
      console.log('');
      console.log(`Permutation test for ${labelCandidates.length} label candidate(s):`);
      console.log(`Null: distribute N tokens across K family folios weighted by folio size`);
      console.log('');

      const rng = makeLCG(42 + fam.charCodeAt(0));

      for (const c of labelCandidates) {
        let atLeast = 0;
        for (let p = 0; p < N_PERMS; p++) {
          const counts = multinomialSample(c.N, c.folioWeights, rng);
          const nullOnce = counts.filter((x) => x === 1).length / c.K;
          if (nullOnce >= c.onceRate) atLeast++;
        }
        const pValue = atLeast / N_PERMS;
        const sig = pValue < 0.001 ? '***' : pValue < 0.01 ? '**' : pValue < 0.05 ? '*' : pValue < 0.10 ? '.' : '';
        console.log(
          `  ${c.word.padEnd(14)} once_rate=${c.onceRate.toFixed(3)}  N=${c.N}  K=${c.K}` +
          `  p=${pValue.toFixed(4)} ${sig}`,
        );
        allCandidates.push({ ...c, family: fam, pValue });
      }
    } else {
      console.log('\n  (no label candidates meeting all three criteria)');
    }
  }

  // Cross-family check: do label words from one family appear in others?
  console.log('\n');
  console.log('='.repeat(60));
  console.log('CROSS-FAMILY CHECK — label words appearing in wrong families');
  console.log('='.repeat(60));
  console.log('');

  const sigLabels = allCandidates.filter((c) => c.pValue !== null && c.pValue < 0.05);

  if (sigLabels.length === 0) {
    console.log('  No statistically significant label words to cross-check.');
  } else {
    console.log('Word'.padEnd(14) + 'Home'.padEnd(14) + 'Appearances in other families');
    console.log('-'.repeat(60));
    for (const lab of sigLabels) {
      const crossEntries: string[] = [];
      for (const fam of FAMILIES_TO_TEST) {
        if (fam === lab.family) continue;
        const famFolios = botanicalFolios.filter((f) => f.family === fam);
        const nFoliosWithWord = famFolios.filter((f) => f.counts.has(lab.word)).length;
        if (nFoliosWithWord > 0) {
          crossEntries.push(`${fam}:${nFoliosWithWord}/${famFolios.length}`);
        }
      }
      console.log(
        lab.word.padEnd(14) +
        lab.family.padEnd(14) +
        (crossEntries.length > 0 ? crossEntries.join(', ') : '—'),
      );
    }
  }

  // Summary
  console.log('\n');
  console.log('='.repeat(60));
  console.log('SUMMARY');
  console.log('='.repeat(60));
  console.log('');
  console.log(`Families tested: ${FAMILIES_TO_TEST.join(', ')}`);
  console.log(`Label candidates (once_rate≥${ONCE_RATE_THRESH}, mean_tok≤${MEAN_TOK_THRESH}, lor≥${LOR_THRESH}): ${allCandidates.length}`);
  console.log(`Statistically significant (p<0.05): ${sigLabels.length}`);
  console.log('');
  if (sigLabels.length > 0) {
    console.log('Significant label words:');
    for (const c of sigLabels.sort((a, b) => (a.pValue ?? 1) - (b.pValue ?? 1))) {
      console.log(`  ${c.family.padEnd(14)} ${c.word.padEnd(14)} once_rate=${c.onceRate.toFixed(3)}  p=${c.pValue?.toFixed(4)}`);
    }
    console.log('');
    console.log('VERDICT: EVA vocabulary contains label words with significantly non-random');
    console.log('  once-per-folio distributions. These words appear family-concentrated and');
    console.log('  with near-unit token counts — consistent with plant-name headings or');
    console.log('  identifiers rather than running pharmacological text.');
  } else {
    console.log('VERDICT: No words meet all label criteria with p<0.05.');
    console.log('  The once-per-folio pattern, where present, is consistent with random');
    console.log('  token placement at low N/K ratios.');
  }
}

main().catch((err) => {
  console.error('[label-word-test] fatal:', err);
  process.exit(1);
});

/**
 * Permutation test for the multi-family NPMI signal.
 *
 * Fixes the EVA word-folio sets (text doesn't change), shuffles the expected-
 * terms assignment across folios N times, and counts how many of the 15
 * characteristic words achieve NPMI ≥ 0.4 under each shuffle. The fraction
 * of shuffles that match or exceed the real score (15/15) is the empirical
 * p-value.
 *
 * This directly asks: could random expected-term assignment produce the
 * observed 15/15 coherence by chance?
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx permutation-test.ts
 *
 * Env:
 *   N_PERMS=500    number of shuffles (default 500)
 *   NPMI_THRESH=0.4
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const N_PERMS    = parseInt(process.env.N_PERMS ?? '500');
const THRESHOLD  = parseFloat(process.env.NPMI_THRESH ?? '0.4');
const MIN_JOINT  = 2;

// The 15 characteristic words from family-npmi-comparison.ts (5 per family)
const CHAR_WORDS: Record<string, string[]> = {
  solanaceae: ['kain', 'oldaiin', 'chedaiin', 'pchedy', 'lchedy'],
  thistle:    ['okeeor', 'dshy', 'chokor', 'okchor', 'keody'],
  plantago:   ['ytchor', 'qotchor', 'she', 'qotor', 'chees'],
};
const ALL_CHAR_WORDS = Object.values(CHAR_WORDS).flat();

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

function parseExpectedTerms(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as { latin?: unknown; italian?: unknown };
    const toArr = (v: unknown): string[] =>
      Array.isArray(v) ? v.map((s) => String(s).toLowerCase().trim()).filter(Boolean) : [];
    return [...new Set([...toArr(parsed.latin), ...toArr(parsed.italian)])];
  } catch { return []; }
}

function npmi(pxy: number, px: number, py: number): number {
  if (pxy === 0 || px === 0 || py === 0) return -1;
  return Math.log(pxy / (px * py)) / -Math.log(pxy);
}

function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/**
 * Count how many characteristic words achieve NPMI >= threshold given a
 * term→folio assignment (termFolioSet) and word→folio sets.
 */
function countCoherent(
  wordFolioSets: Map<string, Set<string>>,
  termFolioSet: Map<string, Set<string>>,
  N: number,
  threshold: number,
): number {
  let count = 0;
  for (const word of ALL_CHAR_WORDS) {
    const wordFolios = wordFolioSets.get(word);
    if (!wordFolios || wordFolios.size === 0) continue;
    const px = wordFolios.size / N;
    let bestNpmi = -1;
    for (const [, tFolios] of termFolioSet) {
      if (tFolios.size < 2) continue;
      let joint = 0;
      for (const fid of wordFolios) if (tFolios.has(fid)) joint++;
      if (joint < MIN_JOINT) continue;
      const py = tFolios.size / N;
      const pxy = joint / N;
      const score = npmi(pxy, px, py);
      if (score > bestNpmi) bestNpmi = score;
    }
    if (bestNpmi >= threshold) count++;
  }
  return count;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log(`permutation-test — empirical p-value for 15/15 NPMI signal`);
  console.log(`N_PERMS=${N_PERMS}, NPMI_THRESH=${THRESHOLD}`);
  console.log('');

  console.log('Loading data...');
  const [visionRows, evaRows] = await Promise.all([
    executeSql(`
      SELECT folio_id, expected_terms
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
    if (r.folio_id && r.eva_text) {
      evaMap.set(r.folio_id, new Set(extractEvaWords(r.eva_text)));
    }
  }

  // Build per-folio expected-terms list (parallel arrays for fast shuffling)
  const folioIds: string[] = [];
  const folioTerms: string[][] = [];

  for (const r of visionRows) {
    if (!r.folio_id) continue;
    if (!evaMap.has(r.folio_id)) continue;
    const words = evaMap.get(r.folio_id)!;
    if (words.size < 10) continue;
    folioIds.push(r.folio_id);
    folioTerms.push(parseExpectedTerms(r.expected_terms));
  }

  const N = folioIds.length;
  console.log(`  ${N} herbal folios with EVA text\n`);

  // Build word→folio sets (fixed — does not change across permutations)
  const wordFolioSets = new Map<string, Set<string>>();
  for (const word of ALL_CHAR_WORDS) {
    const s = new Set<string>();
    for (let i = 0; i < folioIds.length; i++) {
      if (evaMap.get(folioIds[i])!.has(word)) s.add(folioIds[i]);
    }
    wordFolioSets.set(word, s);
  }

  // Helper: build term→folio set from a given terms assignment
  function buildTermFolioSet(termAssignment: string[][]): Map<string, Set<string>> {
    const tfs = new Map<string, Set<string>>();
    for (let i = 0; i < folioIds.length; i++) {
      for (const t of termAssignment[i]) {
        const s = tfs.get(t) ?? new Set<string>();
        s.add(folioIds[i]);
        tfs.set(t, s);
      }
    }
    return tfs;
  }

  // Real score
  const realTermFolioSet = buildTermFolioSet(folioTerms);
  const realScore = countCoherent(wordFolioSets, realTermFolioSet, N, THRESHOLD);
  console.log(`Real score: ${realScore}/${ALL_CHAR_WORDS.length} characteristic words NPMI ≥ ${THRESHOLD}`);
  console.log('');

  // Permutation distribution
  console.log(`Running ${N_PERMS} permutations...`);
  const permScores: number[] = [];
  const distrib = new Array(ALL_CHAR_WORDS.length + 1).fill(0);

  for (let p = 0; p < N_PERMS; p++) {
    // Shuffle which folio gets which expected-terms list
    const shuffledIndices = shuffle([...Array(N).keys()]);
    const shuffledTerms = shuffledIndices.map((i) => folioTerms[i]);
    const permTermFolioSet = buildTermFolioSet(shuffledTerms);
    const score = countCoherent(wordFolioSets, permTermFolioSet, N, THRESHOLD);
    permScores.push(score);
    distrib[score]++;
    if ((p + 1) % 100 === 0) process.stdout.write(`  ${p + 1}/${N_PERMS}\r`);
  }
  process.stdout.write('\n');

  // Compute p-value: fraction of permutations that match or exceed real score
  const atLeastReal = permScores.filter((s) => s >= realScore).length;
  const pValue = atLeastReal / N_PERMS;

  // Distribution summary
  console.log('');
  console.log('Permutation distribution (score = # words NPMI ≥ threshold):');
  console.log('Score'.padEnd(8) + 'Count'.padEnd(8) + 'Fraction'.padEnd(10) + 'Histogram');
  const maxCount = Math.max(...distrib);
  for (let s = ALL_CHAR_WORDS.length; s >= 0; s--) {
    if (distrib[s] === 0 && s < Math.max(...distrib.map((_, i) => distrib[i] > 0 ? i : 0))) continue;
    if (distrib[s] === 0) continue;
    const bar = '█'.repeat(Math.round((distrib[s] / maxCount) * 30));
    const marker = s === realScore ? ' ← real' : '';
    console.log(
      String(s).padEnd(8) +
      String(distrib[s]).padEnd(8) +
      `${(distrib[s] / N_PERMS * 100).toFixed(1)}%`.padEnd(10) +
      bar + marker,
    );
  }
  console.log('');

  const permMean = permScores.reduce((s, x) => s + x, 0) / N_PERMS;
  const permMax  = Math.max(...permScores);
  console.log(`Permutation mean score: ${permMean.toFixed(2)}`);
  console.log(`Permutation max score:  ${permMax}`);
  console.log(`Real score:             ${realScore}`);
  console.log('');
  console.log(`Permutations ≥ real score: ${atLeastReal}/${N_PERMS}`);
  console.log(`Empirical p-value:         ${pValue.toFixed(4)}${pValue === 0 ? ' (p < ' + (1/N_PERMS).toFixed(4) + ')' : ''}`);
  console.log('');

  if (pValue < 0.01) {
    console.log(`VERDICT: p < 0.01 — the ${realScore}/15 NPMI signal is statistically significant.`);
    console.log('         Random expected-term assignment cannot reproduce this result.');
  } else if (pValue < 0.05) {
    console.log(`VERDICT: p < 0.05 — significant but marginal. Consider increasing N_PERMS.`);
  } else {
    console.log(`VERDICT: p = ${pValue.toFixed(3)} — NOT significant at 0.05 level.`);
    console.log('         The NPMI result may be a chance alignment with the expected-term database.');
  }
}

main().catch((err) => {
  console.error('[permutation-test] fatal:', err);
  process.exit(1);
});

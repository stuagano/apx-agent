/**
 * Negative control for the NPMI semantic signal.
 *
 * The multi-family NPMI result (15/15 characteristic words coherent across
 * solanaceae, thistle, plantago) could be an artifact if:
 *   (a) Any EVA word tends to associate with family-appropriate terms regardless
 *       of its family specificity, or
 *   (b) The expected-term database is so rich that any word finds a plausible
 *       high-NPMI association by chance.
 *
 * This script runs three negative controls:
 *
 *   CONTROL A: NEUTRAL WORDS
 *     EVA words with low absolute log-odds relative to ALL three major families
 *     (appear equally across families). These should show no family-specific
 *     NPMI if the signal is real.
 *
 *   CONTROL B: CROSS-FAMILY NPMI
 *     Solanaceae characteristic words tested against thistle folios' expected
 *     terms, thistle words against plantago folios, etc. If NPMI is high even
 *     in the wrong family pairing, the expected-term database is too permissive.
 *
 *   CONTROL C: HIGH-FREQUENCY CORPUS WORDS
 *     Top 20 most frequent EVA word types (corpus-wide). These are the "function
 *     words" equivalent — appear everywhere without family discrimination. Should
 *     not show high NPMI with any family's pharmacological terms.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx negative-control.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// Characteristic words from family-npmi-comparison.ts
const SOLANACEAE_WORDS = ['kain', 'oldaiin', 'chedaiin', 'pchedy', 'lchedy'];
const THISTLE_WORDS    = ['okeeor', 'dshy', 'chokor', 'okchor', 'keody'];
const PLANTAGO_WORDS   = ['ytchor', 'qotchor', 'she', 'qotor', 'chees'];

const MIN_JOINT = 2;
const TOP_N = 5;

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

function npmiScore(pxy: number, px: number, py: number): number {
  if (pxy === 0 || px === 0 || py === 0) return -1;
  return Math.log(pxy / (px * py)) / -Math.log(pxy);
}

/** Top NPMI terms for a word given a set of folios and a term→folio index. */
function topNpmi(
  wordFolios: Set<string>,
  candidateFolios: Set<string>,  // only count co-occurrences on these folios
  termFolioSet: Map<string, Set<string>>,
  totalN: number,
  topN: number,
): Array<{ term: string; npmi: number; joint: number }> {
  const px = wordFolios.size / totalN;
  const scores: Array<{ term: string; npmi: number; joint: number }> = [];
  for (const [term, tFolios] of termFolioSet) {
    if (tFolios.size < 2) continue;
    // Count joint occurrences only on candidateFolios
    let joint = 0;
    for (const fid of wordFolios) {
      if (candidateFolios.has(fid) && tFolios.has(fid)) joint++;
    }
    if (joint < MIN_JOINT) continue;
    const py = tFolios.size / totalN;
    const pxy = joint / totalN;
    scores.push({ term, npmi: npmiScore(pxy, px, py), joint });
  }
  return scores.sort((a, b) => b.npmi - a.npmi).slice(0, topN);
}

function printNpmiRow(word: string, results: Array<{ term: string; npmi: number; joint: number }>): void {
  if (results.length === 0) {
    console.log(`  ${word.padEnd(14)} —`);
  } else {
    const str = results.map((r) => `${r.term}(${r.npmi.toFixed(2)})`).join(', ');
    const best = results[0].npmi;
    const flag = best >= 0.5 ? ' ★' : best >= 0.4 ? ' ·' : '';
    console.log(`  ${word.padEnd(14)} ${str}${flag}`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('negative-control — three controls for the multi-family NPMI signal');
  console.log('');

  console.log('Loading data...');
  const [visionRows, evaRows] = await Promise.all([
    executeSql(`
      SELECT folio_id, subject_candidates, expected_terms
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

  const evaMap = new Map<string, string[]>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
  }

  interface FolioData {
    folioId: string;
    family: string;
    words: Set<string>;
    freq: Map<string, number>;
    expectedTerms: string[];
  }

  const folios: FolioData[] = [];
  const corpusFreq = new Map<string, number>();

  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const rawWords = evaMap.get(r.folio_id) ?? [];
    if (rawWords.length < 10) continue;
    const freq = new Map<string, number>();
    for (const w of rawWords) {
      freq.set(w, (freq.get(w) ?? 0) + 1);
      corpusFreq.set(w, (corpusFreq.get(w) ?? 0) + 1);
    }
    folios.push({
      folioId: r.folio_id,
      family: classifyPlantFamily(r.subject_candidates ?? '[]'),
      words: new Set(rawWords),
      freq,
      expectedTerms: parseExpectedTerms(r.expected_terms),
    });
  }

  const N = folios.length;
  console.log(`  ${N} herbal folios\n`);

  // Build family folio sets
  const BOTANICAL = new Set(['solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
    'mint-family', 'ranunculaceae', 'apiaceae', 'verbena', 'artemisia', 'brassicaceae', 'lily-family']);

  const familyFolioIds: Record<string, Set<string>> = {
    solanaceae: new Set(folios.filter((f) => f.family === 'solanaceae').map((f) => f.folioId)),
    thistle:    new Set(folios.filter((f) => f.family === 'thistle').map((f) => f.folioId)),
    plantago:   new Set(folios.filter((f) => f.family === 'plantago').map((f) => f.folioId)),
  };
  const allFolioIds = new Set(folios.map((f) => f.folioId));

  // Build term folio sets (all herbal folios)
  const termFolioSet = new Map<string, Set<string>>();
  for (const f of folios) {
    for (const t of f.expectedTerms) {
      const s = termFolioSet.get(t) ?? new Set<string>();
      s.add(f.folioId);
      termFolioSet.set(t, s);
    }
  }

  // Build word folio sets
  const wordFolioSet = new Map<string, Set<string>>();
  for (const f of folios) {
    for (const w of f.words) {
      const s = wordFolioSet.get(w) ?? new Set<string>();
      s.add(f.folioId);
      wordFolioSet.set(w, s);
    }
  }

  // -----------------------------------------------------------------------
  // CONTROL A: NEUTRAL WORDS (low LOR relative to all three families)
  // -----------------------------------------------------------------------
  console.log('='.repeat(60));
  console.log('CONTROL A: NEUTRAL WORDS (low family-specificity)');
  console.log('='.repeat(60));
  console.log('');
  console.log('Words that appear at similar rates across solanaceae, thistle,');
  console.log('plantago. Absolute sum of LOR across families < 0.5.');
  console.log('');

  // Build per-family aggregate freq for LOR computation
  const famFreq: Record<string, { freq: Map<string, number>; total: number }> = {};
  const outFreqAll: { freq: Map<string, number>; total: number } = { freq: new Map(), total: 0 };

  for (const fam of ['solanaceae', 'thistle', 'plantago']) {
    const fps = folios.filter((f) => f.family === fam);
    const freq = new Map<string, number>();
    let total = 0;
    for (const fp of fps) {
      for (const [w, c] of fp.freq) { freq.set(w, (freq.get(w) ?? 0) + c); total += c; }
    }
    famFreq[fam] = { freq, total };
  }

  // Pool of all botanical for "out"
  for (const f of folios) {
    if (!BOTANICAL.has(f.family)) continue;
    for (const [w, c] of f.freq) { outFreqAll.freq.set(w, (outFreqAll.freq.get(w) ?? 0) + c); outFreqAll.total += c; }
  }

  function lor(word: string, inFreq: Map<string, number>, inTotal: number, outFreq: Map<string, number>, outTotal: number): number {
    const cIn = inFreq.get(word) ?? 0;
    const cOut = outFreq.get(word) ?? 0;
    const pIn = (cIn + 0.5) / (inTotal + 1);
    const pOut = (cOut + 0.5) / (outTotal + 1);
    return Math.log(pIn / (1 - pIn)) - Math.log(pOut / (1 - pOut));
  }

  // Find neutral words: appear on ≥ 10 folios, |LOR| < 0.3 for all three families
  const neutralWords: Array<{ word: string; folioCount: number; lorS: number; lorT: number; lorP: number }> = [];
  for (const [word, wFolios] of wordFolioSet) {
    if (wFolios.size < 10) continue;
    const lorS = lor(word, famFreq.solanaceae.freq, famFreq.solanaceae.total, outFreqAll.freq, outFreqAll.total);
    const lorT = lor(word, famFreq.thistle.freq, famFreq.thistle.total, outFreqAll.freq, outFreqAll.total);
    const lorP = lor(word, famFreq.plantago.freq, famFreq.plantago.total, outFreqAll.freq, outFreqAll.total);
    if (Math.abs(lorS) < 0.3 && Math.abs(lorT) < 0.3 && Math.abs(lorP) < 0.3) {
      neutralWords.push({ word, folioCount: wFolios.size, lorS, lorT, lorP });
    }
  }
  // Sort by total absolute LOR (most neutral first), take top 15
  neutralWords.sort((a, b) =>
    (Math.abs(a.lorS) + Math.abs(a.lorT) + Math.abs(a.lorP)) -
    (Math.abs(b.lorS) + Math.abs(b.lorT) + Math.abs(b.lorP))
  );
  const selectedNeutral = neutralWords.slice(0, 15);

  console.log(`Found ${neutralWords.length} neutral words (≥10 folios, |LOR|<0.3 for all three families).`);
  console.log(`Testing top 15 most neutral:`);
  console.log('');
  console.log('Word'.padEnd(14) + 'folios'.padEnd(8) + 'LOR_S'.padEnd(8) + 'LOR_T'.padEnd(8) + 'LOR_P'.padEnd(8) + 'Top NPMI (any term)');
  console.log('-'.repeat(80));

  let neutralHighNpmi = 0;
  for (const { word, folioCount, lorS, lorT, lorP } of selectedNeutral) {
    const wFolios = wordFolioSet.get(word)!;
    const results = topNpmi(wFolios, allFolioIds, termFolioSet, N, TOP_N);
    if (results.length > 0 && results[0].npmi >= 0.4) neutralHighNpmi++;
    const str = results.length > 0
      ? results.slice(0, 3).map((r) => `${r.term}(${r.npmi.toFixed(2)})`).join(', ')
      : '—';
    console.log(
      word.padEnd(14) +
      String(folioCount).padEnd(8) +
      lorS.toFixed(2).padEnd(8) +
      lorT.toFixed(2).padEnd(8) +
      lorP.toFixed(2).padEnd(8) +
      str,
    );
  }
  console.log('');
  console.log(`Control A result: ${neutralHighNpmi}/${selectedNeutral.length} neutral words achieve NPMI ≥ 0.4`);
  console.log(`(Real result was 15/15 for characteristic words)`);
  console.log('');

  // -----------------------------------------------------------------------
  // CONTROL B: CROSS-FAMILY NPMI
  // -----------------------------------------------------------------------
  console.log('='.repeat(60));
  console.log('CONTROL B: CROSS-FAMILY NPMI (wrong-family pairing)');
  console.log('='.repeat(60));
  console.log('');
  console.log('Solanaceae words vs. thistle folios, thistle words vs. plantago');
  console.log('folios, plantago words vs. solanaceae folios.');
  console.log('★ = NPMI ≥ 0.5  · = NPMI ≥ 0.4  (if high → signal is diffuse)');
  console.log('');

  const crossFamilyTests: Array<{ label: string; words: string[]; testFolios: Set<string> }> = [
    { label: 'Solanaceae words × Thistle folios',  words: SOLANACEAE_WORDS, testFolios: familyFolioIds.thistle },
    { label: 'Solanaceae words × Plantago folios', words: SOLANACEAE_WORDS, testFolios: familyFolioIds.plantago },
    { label: 'Thistle words × Solanaceae folios',  words: THISTLE_WORDS,    testFolios: familyFolioIds.solanaceae },
    { label: 'Thistle words × Plantago folios',    words: THISTLE_WORDS,    testFolios: familyFolioIds.plantago },
    { label: 'Plantago words × Solanaceae folios', words: PLANTAGO_WORDS,   testFolios: familyFolioIds.solanaceae },
    { label: 'Plantago words × Thistle folios',    words: PLANTAGO_WORDS,   testFolios: familyFolioIds.thistle },
  ];

  let crossHighTotal = 0, crossTotal = 0;
  for (const { label, words, testFolios } of crossFamilyTests) {
    console.log(`${label}:`);
    let highCount = 0;
    for (const word of words) {
      const wFolios = wordFolioSet.get(word) ?? new Set<string>();
      const results = topNpmi(wFolios, testFolios, termFolioSet, N, TOP_N);
      if (results.length > 0 && results[0].npmi >= 0.4) highCount++;
      printNpmiRow(word, results);
    }
    console.log(`  → ${highCount}/${words.length} words with NPMI ≥ 0.4 in wrong family`);
    console.log('');
    crossHighTotal += highCount;
    crossTotal += words.length;
  }
  console.log(`Control B result: ${crossHighTotal}/${crossTotal} cross-family pairs achieve NPMI ≥ 0.4`);
  console.log(`(Real same-family result was 15/15)`);
  console.log('');

  // -----------------------------------------------------------------------
  // CONTROL C: HIGH-FREQUENCY CORPUS WORDS
  // -----------------------------------------------------------------------
  console.log('='.repeat(60));
  console.log('CONTROL C: HIGH-FREQUENCY CORPUS WORDS (function words)');
  console.log('='.repeat(60));
  console.log('');
  console.log('Top 20 most frequent EVA word types. These appear everywhere');
  console.log('without family discrimination — should show low NPMI.');
  console.log('★ = NPMI ≥ 0.5  · = NPMI ≥ 0.4');
  console.log('');

  const topCorpusWords = [...corpusFreq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 20)
    .map(([w]) => w);

  console.log('Word'.padEnd(14) + 'corpus_n'.padEnd(10) + 'Top NPMI (any term)');
  console.log('-'.repeat(70));

  let freqHighNpmi = 0;
  for (const word of topCorpusWords) {
    const wFolios = wordFolioSet.get(word) ?? new Set<string>();
    const results = topNpmi(wFolios, allFolioIds, termFolioSet, N, TOP_N);
    const best = results[0]?.npmi ?? 0;
    if (best >= 0.4) freqHighNpmi++;
    const str = results.length > 0
      ? results.slice(0, 3).map((r) => `${r.term}(${r.npmi.toFixed(2)})`).join(', ')
      : '—';
    const flag = best >= 0.5 ? ' ★' : best >= 0.4 ? ' ·' : '';
    console.log(
      word.padEnd(14) +
      String(corpusFreq.get(word) ?? 0).padEnd(10) +
      str + flag,
    );
  }
  console.log('');
  console.log(`Control C result: ${freqHighNpmi}/${topCorpusWords.length} high-frequency words achieve NPMI ≥ 0.4`);
  console.log(`(Real result was 15/15 for characteristic words)`);
  console.log('');

  // -----------------------------------------------------------------------
  // Summary
  // -----------------------------------------------------------------------
  console.log('='.repeat(60));
  console.log('SUMMARY');
  console.log('='.repeat(60));
  console.log('');
  console.log(`Characteristic words (real signal): 15/15 NPMI ≥ 0.4`);
  console.log(`Control A (neutral words):          ${neutralHighNpmi}/${selectedNeutral.length} NPMI ≥ 0.4`);
  console.log(`Control B (cross-family):           ${crossHighTotal}/${crossTotal} NPMI ≥ 0.4`);
  console.log(`Control C (high-frequency words):   ${freqHighNpmi}/${topCorpusWords.length} NPMI ≥ 0.4`);
  console.log('');

  const controlsLow = neutralHighNpmi <= selectedNeutral.length / 3 &&
                      crossHighTotal  <= crossTotal / 3 &&
                      freqHighNpmi    <= topCorpusWords.length / 3;
  if (controlsLow) {
    console.log('NEGATIVE CONTROLS PASS: Characteristic word NPMI is not an artifact.');
    console.log('All three controls show substantially lower coherence than the real signal.');
  } else {
    console.log('WARNING: One or more controls show high NPMI rates — signal may be inflated.');
    if (neutralHighNpmi > selectedNeutral.length / 3) console.log('  Control A high → expected-term database may be too permissive');
    if (crossHighTotal > crossTotal / 3) console.log('  Control B high → NPMI is not family-specific');
    if (freqHighNpmi > topCorpusWords.length / 3) console.log('  Control C high → high-frequency words inflate NPMI');
  }
}

main().catch((err) => {
  console.error('[negative-control] fatal:', err);
  process.exit(1);
});

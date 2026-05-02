/**
 * Multi-family NPMI comparison.
 *
 * Extends the solanaceae-signal-test to all major botanical families.
 * For each family with n ≥ 10 folios, identifies the top characteristic
 * EVA words (by log-odds ratio vs. other botanical families), then computes
 * NPMI between those words and expected Latin/Italian terms.
 *
 * If EVA vocabulary encodes botanical content, each family's characteristic
 * words should associate with that family's appropriate pharmacological terms:
 *   solanaceae → mandrake, narcotic, soporific
 *   thistle     → carduus, spina, diureticus, bilis
 *   plantago    → nervosus, fibrosus, ulcus, vulnerarius
 *
 * Also checks: do pairs of same-family characteristic words appear on the
 * SAME folios (synonym behavior) or DIFFERENT folios (complementary roles)?
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx family-npmi-comparison.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MIN_FAMILY_N    = parseInt(process.env.MIN_FAMILY_N ?? '10');
const TOP_WORDS_N     = parseInt(process.env.TOP_WORDS_N ?? '8');
const MIN_WORD_FOLIOS = parseInt(process.env.MIN_WORD_FOLIOS ?? '3');
const MIN_JOINT       = parseInt(process.env.MIN_JOINT ?? '2');
const TOP_TERMS_N     = parseInt(process.env.TOP_TERMS_N ?? '6');

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

function npmi(pxy: number, px: number, py: number): number {
  if (pxy === 0 || px === 0 || py === 0) return -1;
  return Math.log(pxy / (px * py)) / -Math.log(pxy);
}

function logOddsTop(
  inFreq: Map<string, number>, inTotal: number,
  outFreq: Map<string, number>, outTotal: number,
  minCount: number, topN: number,
): Array<{ word: string; lor: number; countIn: number }> {
  const results: Array<{ word: string; lor: number; countIn: number }> = [];
  for (const [w, cIn] of inFreq) {
    if (cIn < minCount) continue;
    const cOut = outFreq.get(w) ?? 0;
    const pIn = (cIn + 0.5) / (inTotal + 1);
    const pOut = (cOut + 0.5) / (outTotal + 1);
    const lor = Math.log(pIn / (1 - pIn)) - Math.log(pOut / (1 - pOut));
    results.push({ word: w, lor, countIn: cIn });
  }
  return results.sort((a, b) => b.lor - a.lor).slice(0, topN);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('family-npmi-comparison — multi-family characteristic word NPMI test');
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
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const rawWords = evaMap.get(r.folio_id) ?? [];
    if (rawWords.length < 10) continue;
    const freq = new Map<string, number>();
    for (const w of rawWords) freq.set(w, (freq.get(w) ?? 0) + 1);
    folios.push({
      folioId: r.folio_id,
      family: classifyPlantFamily(r.subject_candidates ?? '[]'),
      words: new Set(rawWords),
      freq,
      expectedTerms: parseExpectedTerms(r.expected_terms),
    });
  }

  const N = folios.length;
  console.log(`  ${N} herbal folios loaded\n`);

  // Build per-family aggregate frequency maps
  const byFamily = new Map<string, FolioData[]>();
  for (const f of folios) {
    const arr = byFamily.get(f.family) ?? [];
    arr.push(f);
    byFamily.set(f.family, arr);
  }

  const familyAggFreq = new Map<string, { freq: Map<string, number>; total: number }>();
  for (const [fam, fps] of byFamily) {
    const freq = new Map<string, number>();
    let total = 0;
    for (const fp of fps) {
      for (const [w, c] of fp.freq) { freq.set(w, (freq.get(w) ?? 0) + c); total += c; }
    }
    familyAggFreq.set(fam, { freq, total });
  }

  // Pool of "other botanical" for contrast (all recognized botanical families together)
  const BOTANICAL_FAMILIES = new Set([
    'solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
    'mint-family', 'ranunculaceae', 'apiaceae', 'verbena',
    'artemisia', 'brassicaceae', 'lily-family',
  ]);

  // Build term folio sets (all herbal)
  const termFolioSet = new Map<string, Set<string>>();
  for (const f of folios) {
    for (const t of f.expectedTerms) {
      const s = termFolioSet.get(t) ?? new Set<string>();
      s.add(f.folioId);
      termFolioSet.set(t, s);
    }
  }

  // Analyze each family with n >= MIN_FAMILY_N
  const targetFamilies = [...byFamily.entries()]
    .filter(([fam, fps]) => BOTANICAL_FAMILIES.has(fam) && fps.length >= MIN_FAMILY_N)
    .sort((a, b) => b[1].length - a[1].length);

  for (const [targetFamily, targetFolios] of targetFamilies) {
    console.log(`${'='.repeat(60)}`);
    console.log(`FAMILY: ${targetFamily.toUpperCase()}  (n=${targetFolios.length} folios)`);
    console.log(`${'='.repeat(60)}`);
    console.log('');

    // Build "out" frequency: all other recognized botanical families
    const outFreq = new Map<string, number>();
    let outTotal = 0;
    for (const [fam, data] of familyAggFreq) {
      if (fam === targetFamily || !BOTANICAL_FAMILIES.has(fam)) continue;
      for (const [w, c] of data.freq) { outFreq.set(w, (outFreq.get(w) ?? 0) + c); outTotal += c; }
    }

    const inData = familyAggFreq.get(targetFamily)!;
    const topWords = logOddsTop(inData.freq, inData.total, outFreq, outTotal, MIN_WORD_FOLIOS, TOP_WORDS_N);

    if (topWords.length === 0) {
      console.log('  No characteristic words found (insufficient frequency).\n');
      continue;
    }

    console.log(`Top ${TOP_WORDS_N} characteristic EVA words (log-odds vs. other botanical):`);
    console.log('Word'.padEnd(14) + 'LOR'.padEnd(8) + 'Count'.padEnd(8) + 'Top NPMI associations');
    console.log('-'.repeat(80));

    const wordFolioSets: Record<string, Set<string>> = {};

    for (const { word, lor, countIn } of topWords) {
      // Which folios does this word appear on?
      const wordFolios = new Set(targetFolios.filter((f) => f.words.has(word)).map((f) => f.folioId));
      wordFolioSets[word] = wordFolios;

      const px = wordFolios.size / N;

      const termScores: Array<{ term: string; npmi: number; joint: number }> = [];
      for (const [term, tFolios] of termFolioSet) {
        if (tFolios.size < 2) continue;
        let joint = 0;
        for (const fid of wordFolios) if (tFolios.has(fid)) joint++;
        if (joint < MIN_JOINT) continue;
        const py = tFolios.size / N;
        const pxy = joint / N;
        const score = npmi(pxy, px, py);
        termScores.push({ term, npmi: score, joint });
      }
      termScores.sort((a, b) => b.npmi - a.npmi);
      const top = termScores.slice(0, TOP_TERMS_N);

      const termStr = top.length > 0
        ? top.map((t) => `${t.term}(${t.npmi.toFixed(2)})`).join(', ')
        : '—';

      console.log(word.padEnd(14) + lor.toFixed(2).padEnd(8) + String(countIn).padEnd(8) + termStr);
    }
    console.log('');

    // Folio overlap matrix for top characteristic words
    const wordList = topWords.map((w) => w.word);
    console.log('Folio overlap matrix (fraction of word-A folios that also have word-B):');
    const header = ''.padEnd(12) + wordList.map((w) => w.padEnd(10)).join('');
    console.log(header);
    for (const wa of wordList) {
      const setA = wordFolioSets[wa];
      if (!setA || setA.size === 0) continue;
      const row = wa.padEnd(12) + wordList.map((wb) => {
        if (wa === wb) return '  —      ';
        const setB = wordFolioSets[wb];
        if (!setB || setB.size === 0) return '  n/a    ';
        let overlap = 0;
        for (const fid of setA) if (setB.has(fid)) overlap++;
        const frac = overlap / setA.size;
        return `${(frac * 100).toFixed(0)}%`.padEnd(10);
      }).join('');
      console.log(row);
    }
    console.log('');

    // Per-word token frequency distribution (label vs. common word behavior)
    console.log('Per-folio token counts for characteristic words (on folios where word appears):');
    console.log('Word'.padEnd(14) + 'folios'.padEnd(8) + 'mean_count'.padEnd(12) + 'max_count'.padEnd(12) + 'once-only%');
    console.log('-'.repeat(58));
    for (const { word } of topWords) {
      const counts = targetFolios
        .filter((f) => f.words.has(word))
        .map((f) => f.freq.get(word) ?? 0)
        .filter((c) => c > 0);
      if (counts.length === 0) continue;
      const meanC = counts.reduce((s, c) => s + c, 0) / counts.length;
      const maxC = Math.max(...counts);
      const onceOnly = counts.filter((c) => c === 1).length / counts.length;
      console.log(
        word.padEnd(14) +
        String(counts.length).padEnd(8) +
        meanC.toFixed(1).padEnd(12) +
        String(maxC).padEnd(12) +
        `${(onceOnly * 100).toFixed(0)}%`,
      );
    }
    console.log('');
  }

  // ---------------------------------------------------------------------------
  // Cross-family NPMI coherence summary
  // ---------------------------------------------------------------------------
  console.log('='.repeat(60));
  console.log('CROSS-FAMILY COHERENCE SUMMARY');
  console.log('='.repeat(60));
  console.log('');
  console.log('For each family, do top characteristic words associate with');
  console.log('family-appropriate expected terms? (qualitative assessment)');
  console.log('');
  console.log('A word is "coherent" if its top NPMI term is semantically');
  console.log('appropriate for the plant family.');
  console.log('');

  // Re-run condensed version for the summary
  for (const [targetFamily, targetFolios] of targetFamilies) {
    const outFreq = new Map<string, number>();
    let outTotal = 0;
    for (const [fam, data] of familyAggFreq) {
      if (fam === targetFamily || !BOTANICAL_FAMILIES.has(fam)) continue;
      for (const [w, c] of data.freq) { outFreq.set(w, (outFreq.get(w) ?? 0) + c); outTotal += c; }
    }
    const inData = familyAggFreq.get(targetFamily)!;
    const topWords = logOddsTop(inData.freq, inData.total, outFreq, outTotal, MIN_WORD_FOLIOS, 5);

    let coherentCount = 0;
    const summaryLines: string[] = [];

    for (const { word } of topWords) {
      const wordFolios = new Set(targetFolios.filter((f) => f.words.has(word)).map((f) => f.folioId));
      const px = wordFolios.size / N;
      const termScores: Array<{ term: string; npmi: number }> = [];
      for (const [term, tFolios] of termFolioSet) {
        if (tFolios.size < 2) continue;
        let joint = 0;
        for (const fid of wordFolios) if (tFolios.has(fid)) joint++;
        if (joint < MIN_JOINT) continue;
        const py = tFolios.size / N;
        const pxy = joint / N;
        termScores.push({ term, npmi: npmi(pxy, px, py) });
      }
      termScores.sort((a, b) => b.npmi - a.npmi);
      const top1 = termScores[0];
      if (top1 && top1.npmi > 0.4) coherentCount++;
      summaryLines.push(`  ${word.padEnd(12)} top="${top1?.term ?? '—'}" npmi=${top1?.npmi.toFixed(3) ?? '—'}`);
    }

    console.log(`${targetFamily} (n=${targetFolios.length}): ${coherentCount}/${topWords.length} words with NPMI>0.4`);
    for (const line of summaryLines) console.log(line);
    console.log('');
  }
}

main().catch((err) => {
  console.error('[family-npmi-comparison] fatal:', err);
  process.exit(1);
});

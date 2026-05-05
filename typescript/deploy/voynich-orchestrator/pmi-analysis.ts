/**
 * PMI (Pointwise Mutual Information) analysis.
 *
 * For each (EVA word W, expected Latin/Italian term T), computes how often
 * W appears on folios where T is expected vs. by chance across all 226 herbal
 * folios. High PMI = W and T co-occur more than random.
 *
 * This is the data layer for the vocabulary-guided codebook hypothesis.
 * Inspect the output manually before building the codebook decoder.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx pmi-analysis.ts
 *
 * Env vars:
 *   MIN_WORD_FOLIOS=3   EVA word must appear on this many folios to be included
 *   MIN_TERM_FOLIOS=3   expected term must appear on this many folios
 *   MIN_JOINT=2         minimum joint co-occurrences for a pair to be reported
 *   TOP_N=50            pairs to display in ranked table
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MIN_WORD_FOLIOS = parseInt(process.env.MIN_WORD_FOLIOS ?? '3');
const MIN_TERM_FOLIOS = parseInt(process.env.MIN_TERM_FOLIOS ?? '3');
const MIN_JOINT       = parseInt(process.env.MIN_JOINT ?? '2');
const TOP_N           = parseInt(process.env.TOP_N ?? '50');

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
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
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
// Parsing helpers
// ---------------------------------------------------------------------------

function parseExpectedTerms(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as { latin?: unknown; italian?: unknown };
    const toArr = (v: unknown): string[] =>
      Array.isArray(v) ? v.map((s) => String(s).toLowerCase().trim()).filter(Boolean) : [];
    return [...new Set([...toArr(parsed.latin), ...toArr(parsed.italian)])];
  } catch {
    return [];
  }
}

function extractEvaWordSet(evaText: string): Set<string> {
  const words = evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
  return new Set(words);
}

function classifyFamily(subjectCandidates: string | null): string {
  let cands: Array<{ name: string; latin: string; confidence: number }> = [];
  try { cands = JSON.parse(subjectCandidates ?? '[]'); } catch { return 'unknown'; }
  const top = cands[0];
  if (!top || top.confidence < 0.35) return 'uncertain';
  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus/.test(latin + name)) return 'solanaceae';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name))     return 'thistle';
  if (/plantago|plantain/.test(latin + name))                             return 'plantago';
  if (/papaver|poppy/.test(latin + name))                                 return 'poppy';
  if (/artemisia|absinthium|wormwood/.test(latin + name))                 return 'artemisia';
  if (/borago|borage/.test(latin + name))                                 return 'borago';
  const genus = latin.split(/[\s/,]/)[0];
  return genus || 'unknown';
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('pmi-analysis — EVA word × expected Latin/Italian term co-occurrence');
  console.log(`MIN_WORD_FOLIOS=${MIN_WORD_FOLIOS}  MIN_TERM_FOLIOS=${MIN_TERM_FOLIOS}  MIN_JOINT=${MIN_JOINT}  TOP_N=${TOP_N}`);
  console.log('');

  // Load data
  console.log('Loading folio_vision_analysis...');
  const visionRows = await executeSql(`
    SELECT folio_id, expected_terms, subject_candidates
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
  `);

  console.log('Loading EVA corpus...');
  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
  `);

  const evaByFolio = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaByFolio.set(r.folio_id, r.eva_text);
  }

  // Build per-folio data
  interface FolioData {
    folioId: string;
    family: string;
    terms: string[];
    evaWords: Set<string>;
  }

  const folios: FolioData[] = [];
  for (const r of visionRows) {
    const folioId = r.folio_id ?? '';
    const evaText = evaByFolio.get(folioId);
    if (!evaText) continue;
    const terms = parseExpectedTerms(r.expected_terms);
    if (terms.length === 0) continue;
    folios.push({
      folioId,
      family: classifyFamily(r.subject_candidates),
      terms,
      evaWords: extractEvaWordSet(evaText),
    });
  }

  const N = folios.length;
  console.log(`  ${N} folios with both vision + EVA data`);

  // ---------------------------------------------------------------------------
  // Count marginal frequencies
  // ---------------------------------------------------------------------------

  const wordFolioCount = new Map<string, number>();
  for (const f of folios) {
    for (const w of f.evaWords) wordFolioCount.set(w, (wordFolioCount.get(w) ?? 0) + 1);
  }

  const termFolioCount = new Map<string, number>();
  for (const f of folios) {
    for (const t of f.terms) termFolioCount.set(t, (termFolioCount.get(t) ?? 0) + 1);
  }

  const candidateWords = new Set([...wordFolioCount.entries()]
    .filter(([, c]) => c >= MIN_WORD_FOLIOS).map(([w]) => w));
  const candidateTerms = new Set([...termFolioCount.entries()]
    .filter(([, c]) => c >= MIN_TERM_FOLIOS).map(([t]) => t));

  console.log(`  ${candidateWords.size} EVA words on ≥${MIN_WORD_FOLIOS} folios`);
  console.log(`  ${candidateTerms.size} expected terms on ≥${MIN_TERM_FOLIOS} folios`);
  console.log(`  Computing joint counts...`);

  // ---------------------------------------------------------------------------
  // Accumulate joint counts by iterating over folios (efficient)
  // ---------------------------------------------------------------------------

  // key = `word\0term`
  const jointCounts  = new Map<string, number>();
  const jointFolios  = new Map<string, string[]>();  // keep up to 4 example folios
  const jointFamilies = new Map<string, Set<string>>();

  for (const f of folios) {
    const relevantWords = [...f.evaWords].filter((w) => candidateWords.has(w));
    const relevantTerms = f.terms.filter((t) => candidateTerms.has(t));

    for (const w of relevantWords) {
      for (const t of relevantTerms) {
        const key = `${w}\0${t}`;
        jointCounts.set(key, (jointCounts.get(key) ?? 0) + 1);
        const jf = jointFolios.get(key);
        if (!jf) { jointFolios.set(key, [f.folioId]); }
        else if (jf.length < 4) { jf.push(f.folioId); }
        const jfam = jointFamilies.get(key);
        if (!jfam) { jointFamilies.set(key, new Set([f.family])); }
        else { jfam.add(f.family); }
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Compute PMI for all pairs meeting minimum joint threshold
  // ---------------------------------------------------------------------------

  interface PMIPair {
    word: string;
    term: string;
    nW: number;
    nT: number;
    nWT: number;
    pmi: number;
    npmi: number;           // normalized PMI = PMI / -log2(P(W,T)), range [-1,1]
    exampleFolios: string[];
    families: string[];
  }

  const pairs: PMIPair[] = [];

  for (const [key, nWT] of jointCounts) {
    if (nWT < MIN_JOINT) continue;
    const [word, term] = key.split('\0');
    const nW = wordFolioCount.get(word) ?? 0;
    const nT = termFolioCount.get(term) ?? 0;

    const pWT = nWT / N;
    const pW  = nW  / N;
    const pT  = nT  / N;

    const pmi  = Math.log2(pWT / (pW * pT));
    if (pmi <= 0) continue;

    const npmi = pmi / (-Math.log2(pWT));  // normalized: 1 = perfect co-occurrence

    pairs.push({
      word,
      term,
      nW,
      nT,
      nWT,
      pmi,
      npmi,
      exampleFolios: jointFolios.get(key) ?? [],
      families: [...(jointFamilies.get(key) ?? new Set())],
    });
  }

  // Sort by NPMI desc (normalized is more comparable across rare/common pairs)
  pairs.sort((a, b) => b.npmi - a.npmi || b.nWT - a.nWT);

  console.log(`  ${pairs.length} positive PMI pairs (nWT≥${MIN_JOINT})`);

  // ---------------------------------------------------------------------------
  // Display: ranked table
  // ---------------------------------------------------------------------------

  console.log('');
  console.log(`=== Top ${Math.min(TOP_N, pairs.length)} pairs by normalized PMI ===`);
  console.log('');
  console.log(
    '#'.padEnd(4) +
    'NPMI'.padStart(6) + ' ' +
    'PMI'.padStart(5) + ' ' +
    'nWT/nW/nT'.padEnd(12) +
    'EVA word'.padEnd(18) +
    'expected term'.padEnd(22) +
    'families'
  );
  console.log('─'.repeat(90));

  for (let i = 0; i < Math.min(TOP_N, pairs.length); i++) {
    const p = pairs[i];
    const freqStr = `${p.nWT}/${p.nW}/${p.nT}`;
    console.log(
      (i + 1).toString().padEnd(4) +
      p.npmi.toFixed(3).padStart(6) + ' ' +
      p.pmi.toFixed(2).padStart(5) + ' ' +
      freqStr.padEnd(12) +
      p.word.padEnd(18) +
      p.term.padEnd(22) +
      p.families.slice(0, 3).join(', ')
    );
  }

  // ---------------------------------------------------------------------------
  // Display: best term per EVA word (candidate codebook)
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Candidate codebook — best term per EVA word (top 40 by NPMI) ===');
  console.log('');

  const seenWords = new Set<string>();
  let shown = 0;
  for (const p of pairs) {
    if (seenWords.has(p.word)) continue;
    seenWords.add(p.word);
    shown++;
    if (shown > 40) break;
    const exStr = p.exampleFolios.slice(0, 2).join(', ');
    console.log(
      `  ${p.word.padEnd(18)} → ${p.term.padEnd(22)} npmi=${p.npmi.toFixed(3)}  nWT=${p.nWT}  e.g. ${exStr}`
    );
  }

  // ---------------------------------------------------------------------------
  // Display: best EVA word per expected term (reverse index)
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Reverse index — best EVA word per expected term (top 40 by NPMI) ===');
  console.log('');

  const seenTerms = new Set<string>();
  shown = 0;
  for (const p of pairs) {
    if (seenTerms.has(p.term)) continue;
    seenTerms.add(p.term);
    shown++;
    if (shown > 40) break;
    const exStr = p.exampleFolios.slice(0, 2).join(', ');
    console.log(
      `  ${p.term.padEnd(22)} ← ${p.word.padEnd(18)} npmi=${p.npmi.toFixed(3)}  nWT=${p.nWT}  e.g. ${exStr}`
    );
  }

  // ---------------------------------------------------------------------------
  // Display: per-family view — what are the top EVA words for each family's terms?
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Per-family: top EVA words associated with family-specific terms ===');
  console.log('');

  const familyGroups = new Map<string, FolioData[]>();
  for (const f of folios) {
    if (!familyGroups.has(f.family)) familyGroups.set(f.family, []);
    familyGroups.get(f.family)!.push(f);
  }

  const topFamilies = [...familyGroups.entries()]
    .filter(([, fps]) => fps.length >= 5)
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, 6);

  for (const [family, fps] of topFamilies) {
    // Find pairs where this family is the dominant co-occurrence family
    const familyPairs = pairs
      .filter((p) => p.families.includes(family) && p.nWT >= 3)
      .slice(0, 6);

    if (familyPairs.length === 0) continue;
    console.log(`  ${family} (${fps.length} folios):`);
    for (const p of familyPairs) {
      console.log(`    ${p.word.padEnd(18)} ↔ ${p.term.padEnd(22)} npmi=${p.npmi.toFixed(3)} nWT=${p.nWT}`);
    }
    console.log('');
  }
}

main().catch((err) => {
  console.error('[pmi-analysis] fatal:', err);
  process.exit(1);
});

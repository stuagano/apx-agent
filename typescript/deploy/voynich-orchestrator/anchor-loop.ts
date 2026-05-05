/**
 * Anchor discovery loop.
 *
 * Finds EVA words that pass two independent statistical gates:
 *   1. High NPMI (≥MIN_NPMI) against a discriminating expected term (≤MAX_TERM_FOLIOS folios)
 *   2. High full-distribution family hit rate (≥MIN_HIT_RATE)
 *      Denominator = ALL herbal folios the word appears on (n_total, no confidence filter)
 *      Numerator   = those folios where vision confidence ≥FAMILY_CONF and classified as home family
 *      This is the same test that falsified oldar/oldam beyond oldaiin.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx anchor-loop.ts
 *
 * Env:
 *   MAX_TERM_FOLIOS=40    upper bound on term folio count (discriminating = rare terms only)
 *   MIN_WORD_FOLIOS=5     minimum total folio appearances for a word (full distribution)
 *   MIN_JOINT=2           minimum joint co-occurrence count
 *   MIN_NPMI=0.40         minimum normalized PMI
 *   MIN_HIT_RATE=0.70     minimum full-distribution family hit rate
 *   FAMILY_CONF=0.35      vision confidence threshold to assign a folio to a named family
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MAX_TERM_FOLIOS = parseInt(process.env.MAX_TERM_FOLIOS ?? '40');
const MIN_WORD_FOLIOS = parseInt(process.env.MIN_WORD_FOLIOS ?? '5');
const MIN_JOINT       = parseInt(process.env.MIN_JOINT ?? '2');
const MIN_NPMI        = parseFloat(process.env.MIN_NPMI ?? '0.40');
const MIN_HIT_RATE    = parseFloat(process.env.MIN_HIT_RATE ?? '0.70');
const FAMILY_CONF     = parseFloat(process.env.FAMILY_CONF ?? '0.35');

// Already confirmed — exclude from discovery output
const KNOWN_ANCHORS = new Set(['oldaiin']);

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host        = resolveHost();
  const token       = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?:   { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?:   { state?: string; error?: { message?: string } };
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
// Parsing helpers (mirrors pmi-analysis.ts)
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

// Returns null if confidence below threshold (folio is unclassified for family purposes)
function classifyFamily(subjectCandidates: string | null): string | null {
  let cands: Array<{ name?: string; latin?: string; confidence?: number }> = [];
  try { cands = JSON.parse(subjectCandidates ?? '[]'); } catch { return null; }
  const top = cands[0];
  if (!top || (top.confidence ?? 0) < FAMILY_CONF) return null;
  const key = ((top.latin ?? '') + ' ' + (top.name ?? '')).toLowerCase();
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus/.test(key))             return 'solanaceae';
  if (/cirsium|carduus|thistle|carlina|centaurea|silybum|onopordum|echinops/.test(key)) return 'thistle';
  if (/plantago|plantain/.test(key))                                          return 'plantago';
  if (/papaver|poppy/.test(key))                                              return 'poppy';
  if (/artemisia|absinthium|wormwood/.test(key))                              return 'artemisia';
  if (/borago|borage/.test(key))                                              return 'borago';
  const genus = (top.latin ?? '').toLowerCase().split(/[\s/,]/)[0];
  return genus || null;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

interface AnchorCandidate {
  word:       string;
  bestTerm:   string;      // best discriminating term (highest NPMI) that passes PMI gate
  npmi:       number;
  pmi:        number;
  nW:         number;      // total folio appearances across ALL herbal folios
  nT:         number;      // total folio appearances for bestTerm
  nWT:        number;      // joint appearances
  homeFamily: string;      // family with highest hit rate
  nHome:      number;      // word appearances on homeFamily folios (conf ≥ FAMILY_CONF)
  hitRate:    number;      // nHome / nW
  score:      number;      // npmi × hitRate (ranking metric)
  passesGate: boolean;
}

async function main(): Promise<void> {
  console.log('anchor-loop — full-distribution gate anchor discovery');
  console.log(`MAX_TERM_FOLIOS=${MAX_TERM_FOLIOS}  MIN_WORD_FOLIOS=${MIN_WORD_FOLIOS}  MIN_JOINT=${MIN_JOINT}`);
  console.log(`MIN_NPMI=${MIN_NPMI}  MIN_HIT_RATE=${MIN_HIT_RATE}  FAMILY_CONF=${FAMILY_CONF}`);
  console.log(`Known anchors (excluded): ${[...KNOWN_ANCHORS].join(', ')}`);
  console.log('');

  // ---------------------------------------------------------------------------
  // Load raw data
  // ---------------------------------------------------------------------------

  console.log('Loading folio_vision_analysis (herbal)...');
  const visionRows = await executeSql(`
    SELECT folio_id, expected_terms, subject_candidates
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
  `);

  console.log('Loading EVA corpus (herbal)...');
  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
  `);

  const evaByFolio = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaByFolio.set(r.folio_id, r.eva_text);
  }

  // ---------------------------------------------------------------------------
  // Build per-folio structures
  // allFolios  — ALL herbal folios with EVA data (used for full-distribution counts)
  // pmiCandidates — only folios that also have expected_terms (used for PMI)
  // ---------------------------------------------------------------------------

  interface FolioData {
    folioId:  string;
    family:   string | null;   // null = unclassified (conf too low)
    terms:    string[];
    evaWords: Set<string>;
  }

  const allFolios: FolioData[] = [];
  for (const r of visionRows) {
    const folioId = r.folio_id ?? '';
    const evaText = evaByFolio.get(folioId);
    if (!evaText) continue;
    allFolios.push({
      folioId,
      family:   classifyFamily(r.subject_candidates),
      terms:    parseExpectedTerms(r.expected_terms),
      evaWords: extractEvaWordSet(evaText),
    });
  }

  const pmiCandidates = allFolios.filter((f) => f.terms.length > 0);
  const N = pmiCandidates.length;  // PMI universe size

  console.log(`  ${allFolios.length} herbal folios with EVA data (full-distribution universe)`);
  console.log(`  ${pmiCandidates.length} of those have expected_terms (PMI universe)`);
  console.log('');

  // ---------------------------------------------------------------------------
  // Full-distribution counts — computed over allFolios
  // wordTotalCount[w]     = # distinct folios in allFolios where w appears
  // wordFamilyCounts[w][f]= # of those folios classified as family f (conf ≥ FAMILY_CONF)
  // ---------------------------------------------------------------------------

  const wordTotalCount   = new Map<string, number>();
  const wordFamilyCounts = new Map<string, Map<string, number>>();

  for (const f of allFolios) {
    for (const w of f.evaWords) {
      wordTotalCount.set(w, (wordTotalCount.get(w) ?? 0) + 1);
      if (f.family !== null) {
        let fm = wordFamilyCounts.get(w);
        if (!fm) { fm = new Map<string, number>(); wordFamilyCounts.set(w, fm); }
        fm.set(f.family, (fm.get(f.family) ?? 0) + 1);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // PMI computation — computed over pmiCandidates only
  // ---------------------------------------------------------------------------

  const wordFolioCount = new Map<string, number>();
  const termFolioCount = new Map<string, number>();

  for (const f of pmiCandidates) {
    for (const w of f.evaWords) wordFolioCount.set(w, (wordFolioCount.get(w) ?? 0) + 1);
    for (const t of f.terms)    termFolioCount.set(t, (termFolioCount.get(t) ?? 0) + 1);
  }

  // Discriminating terms: appear on ≤MAX_TERM_FOLIOS folios in the PMI universe
  const discriminatingTerms = new Set(
    [...termFolioCount.entries()]
      .filter(([, c]) => c >= MIN_JOINT && c <= MAX_TERM_FOLIOS)
      .map(([t]) => t)
  );

  // Words that appear on ≥MIN_WORD_FOLIOS folios in full distribution
  const candidateWordsPMI = new Set(
    [...wordFolioCount.entries()]
      .filter(([w]) => (wordTotalCount.get(w) ?? 0) >= MIN_WORD_FOLIOS)
      .map(([w]) => w)
  );

  console.log(`  ${discriminatingTerms.size} discriminating terms (2 ≤ nT ≤ ${MAX_TERM_FOLIOS})`);
  console.log(`  ${candidateWordsPMI.size} candidate EVA words (n_total ≥ ${MIN_WORD_FOLIOS})`);
  console.log(`  Computing PMI pairs...`);

  const jointCounts = new Map<string, number>();  // key = `word\0term`
  for (const f of pmiCandidates) {
    const relWords = [...f.evaWords].filter((w) => candidateWordsPMI.has(w));
    const relTerms = f.terms.filter((t) => discriminatingTerms.has(t));
    for (const w of relWords) {
      for (const t of relTerms) {
        const key = `${w}\0${t}`;
        jointCounts.set(key, (jointCounts.get(key) ?? 0) + 1);
      }
    }
  }

  // For each word, keep only the best term (highest NPMI passing gates)
  interface PMIPair {
    word: string;
    term: string;
    nW:   number;
    nT:   number;
    nWT:  number;
    pmi:  number;
    npmi: number;
  }

  // Best pair per word — only the highest-NPMI pair that passes MIN_NPMI and MIN_JOINT
  const bestPairPerWord = new Map<string, PMIPair>();

  for (const [key, nWT] of jointCounts) {
    if (nWT < MIN_JOINT) continue;
    const [word, term] = key.split('\0');
    const nW = wordFolioCount.get(word) ?? 0;
    const nT = termFolioCount.get(term) ?? 0;

    const pWT  = nWT / N;
    const pW   = nW  / N;
    const pT   = nT  / N;
    const pmi  = Math.log2(pWT / (pW * pT));
    if (pmi <= 0) continue;
    const npmi = pmi / (-Math.log2(pWT));
    if (npmi < MIN_NPMI) continue;

    const existing = bestPairPerWord.get(word);
    if (!existing || npmi > existing.npmi) {
      bestPairPerWord.set(word, { word, term, nW, nT, nWT, pmi, npmi });
    }
  }

  console.log(`  ${bestPairPerWord.size} words with ≥1 discriminating PMI pair (NPMI ≥ ${MIN_NPMI})`);
  console.log('');

  // ---------------------------------------------------------------------------
  // Apply full-distribution hit rate gate
  // ---------------------------------------------------------------------------

  console.log('=== Gate results — NPMI + full-distribution family concentration ===');
  console.log('');
  console.log(
    'word'.padEnd(14) +
    'best_term'.padEnd(20) +
    'NPMI'.padStart(6) + ' ' +
    'nW'.padStart(4) + ' ' +
    'home_fam'.padEnd(14) +
    'nHome'.padStart(6) + ' ' +
    'hitRate'.padStart(8) + '  ' +
    'gate'
  );
  console.log('─'.repeat(80));

  const candidates: AnchorCandidate[] = [];

  for (const [word, pair] of bestPairPerWord) {
    const nW        = wordTotalCount.get(word) ?? 0;
    const famCounts = wordFamilyCounts.get(word) ?? new Map<string, number>();

    // Find the family with the highest hit count
    let homeFamily = '';
    let nHome      = 0;
    for (const [fam, cnt] of famCounts) {
      if (cnt > nHome) { nHome = cnt; homeFamily = fam; }
    }

    const hitRate    = nW > 0 ? nHome / nW : 0;
    const passesGate = nW >= MIN_WORD_FOLIOS && hitRate >= MIN_HIT_RATE;
    const score      = pair.npmi * hitRate;

    const candidate: AnchorCandidate = {
      word, bestTerm: pair.term, npmi: pair.npmi, pmi: pair.pmi,
      nW, nT: pair.nT, nWT: pair.nWT,
      homeFamily, nHome, hitRate, score, passesGate,
    };
    candidates.push(candidate);

    // Print all candidates (not just passing) so we can see what's on the margin
    const gateStr = passesGate
      ? (KNOWN_ANCHORS.has(word) ? 'KNOWN' : 'PASS')
      : 'fail';
    console.log(
      word.padEnd(14) +
      pair.term.padEnd(20) +
      pair.npmi.toFixed(3).padStart(6) + ' ' +
      nW.toString().padStart(4) + ' ' +
      (homeFamily || 'unclassified').padEnd(14) +
      nHome.toString().padStart(6) + ' ' +
      (hitRate * 100).toFixed(1).padStart(7) + '%  ' +
      gateStr
    );
  }

  // ---------------------------------------------------------------------------
  // Ranked new anchors
  // ---------------------------------------------------------------------------

  const newAnchors = candidates
    .filter((c) => c.passesGate && !KNOWN_ANCHORS.has(c.word))
    .sort((a, b) => b.score - a.score);

  console.log('');
  console.log(`=== New anchor candidates (passing gate, not already known) ===`);
  console.log('');

  if (newAnchors.length === 0) {
    console.log('  No new anchors found at current thresholds.');
    console.log(`  (gate: nW ≥ ${MIN_WORD_FOLIOS}, hitRate ≥ ${(MIN_HIT_RATE * 100).toFixed(0)}%, NPMI ≥ ${MIN_NPMI})`);
    console.log('');
    console.log('  Candidates closest to passing:');
    const near = candidates
      .filter((c) => !KNOWN_ANCHORS.has(c.word))
      .sort((a, b) => b.score - a.score)
      .slice(0, 10);
    for (const c of near) {
      const why = [];
      if (c.nW < MIN_WORD_FOLIOS) why.push(`nW=${c.nW}<${MIN_WORD_FOLIOS}`);
      if (c.hitRate < MIN_HIT_RATE) why.push(`hitRate=${(c.hitRate * 100).toFixed(1)}%<${(MIN_HIT_RATE * 100).toFixed(0)}%`);
      if (c.npmi < MIN_NPMI) why.push(`NPMI=${c.npmi.toFixed(3)}<${MIN_NPMI}`);
      console.log(
        `  ${c.word.padEnd(14)} → ${c.bestTerm.padEnd(18)} npmi=${c.npmi.toFixed(3)} ` +
        `hit=${(c.hitRate * 100).toFixed(1)}% nW=${c.nW}  [${why.join(', ')}]`
      );
    }
  } else {
    for (let i = 0; i < newAnchors.length; i++) {
      const c = newAnchors[i];
      console.log(
        `  ${i + 1}. ${c.word.padEnd(14)} → ${c.bestTerm.padEnd(18)}` +
        `  NPMI=${c.npmi.toFixed(3)}  family=${c.homeFamily}` +
        `  n_total=${c.nW}  n_home=${c.nHome}  hit_rate=${(c.hitRate * 100).toFixed(1)}%` +
        `  score=${c.score.toFixed(3)}`
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Known anchors — confirm they still pass
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Known anchors — gate verification ===');
  console.log('');

  for (const knownWord of KNOWN_ANCHORS) {
    const c = candidates.find((x) => x.word === knownWord);
    if (!c) {
      console.log(`  ${knownWord.padEnd(14)} — not in PMI candidates (check MIN_WORD_FOLIOS or MIN_NPMI)`);
      continue;
    }
    const status = c.passesGate ? 'CONFIRMED' : 'WARNING: fails gate!';
    console.log(
      `  ${c.word.padEnd(14)} → ${c.bestTerm.padEnd(18)}` +
      `  NPMI=${c.npmi.toFixed(3)}  hit=${(c.hitRate * 100).toFixed(1)}%  nW=${c.nW}  [${status}]`
    );
  }

  // ---------------------------------------------------------------------------
  // Extended codebook preview
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Extended codebook preview ===');
  console.log('');

  const allConfirmed = [
    { word: 'oldaiin', term: 'somnus', family: 'solanaceae', source: 'known-confirmed' },
    ...newAnchors.map((c) => ({ word: c.word, term: c.bestTerm, family: c.homeFamily, source: 'loop-discovered' })),
  ];

  for (const a of allConfirmed) {
    console.log(`  ${a.word.padEnd(14)} → ${a.term.padEnd(18)}  [${a.family}, ${a.source}]`);
  }

  // ---------------------------------------------------------------------------
  // Coverage over allFolios with current confirmed set
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Coverage with confirmed anchors ===');
  console.log('');

  const codebookWords = new Set(allConfirmed.map((a) => a.word));
  let totalTokens = 0;
  let anchorHits  = 0;

  for (const f of allFolios) {
    for (const w of f.evaWords) {
      totalTokens++;
      if (codebookWords.has(w)) anchorHits++;
    }
  }

  // Note: evaWords is a Set, so counts distinct words per folio, not raw token count
  // For raw token coverage we'd need to not deduplicate — but this is still meaningful
  // as a fraction of (folio, word) pairs
  const coverage = totalTokens > 0 ? anchorHits / totalTokens * 100 : 0;
  console.log(`  Confirmed anchors: ${allConfirmed.length}`);
  console.log(`  Unique (folio, word) pairs: ${totalTokens}`);
  console.log(`  Anchor hits: ${anchorHits}`);
  console.log(`  Coverage (distinct word-level): ${coverage.toFixed(2)}%`);

  // Per-anchor hits
  console.log('');
  console.log('  Per-anchor hits across allFolios:');
  for (const a of allConfirmed) {
    const hits = wordTotalCount.get(a.word) ?? 0;
    console.log(`    ${a.word.padEnd(14)} → ${a.term.padEnd(16)}  n_total=${hits}`);
  }
}

main().catch((err) => {
  console.error('[anchor-loop] fatal:', err);
  process.exit(1);
});

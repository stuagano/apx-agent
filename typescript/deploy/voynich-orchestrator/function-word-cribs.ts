// Function-word crib attack — using positional grammar findings from language-statistics.ts.
//
// Statistical findings (from language-statistics.ts and section-vocabulary.ts):
//
//   LINE-FINAL cluster (>= 62% line-final rate vs 14% baseline):
//     "am"  (72%), "oly" (68%), "dam" (62%)
//
//   LINE-INITIAL cluster (>= 39% line-initial rate vs 14% baseline):
//     "sain" (57%), "sol" (55%), "saiin" (45%), "sar" (39%)
//
//   CROSS-SECTION function words (>= 50% folio presence in ALL major sections):
//     "daiin", "or", "dar", "dy"
//
// Hypothesis: line-final EVA words encode sentence-final particles; line-initial
// words encode sentence-initial words; cross-section words encode high-frequency
// function words common to any text regardless of topic.
//
// Method:
//   1. For each (EVA-word, Latin-candidate) pair, derive the partial char-level
//      substitution map (each EVA char → one Latin char, bijective within crib).
//   2. Test compatibility between all active cribs (no character-level contradictions).
//   3. Run simulated annealing on the full herbal corpus with the compatible
//      partial map locked.
//   4. Score using trigram model of medieval Latin.
//   5. Report the best-scoring compatible crib sets and sample decodes.
//
// Run: npx tsx function-word-cribs.ts

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';
const SA_STEPS  = 3000;
const N_RESTARTS = 4;
const SCORE_GATE = 0.28;  // min score to report as promising

// ── Positional grammar cribs ─────────────────────────────────────────────────
//
// Format: { evaWord, candidates: [Latin word(s)], role, evidence }
// Each EVA word is tokenized as a sequence of individual characters (single-char
// glyph assumption). crib is valid only if len(evaWord) === len(latinWord).

interface CribSpec {
  evaWord:    string;
  candidates: string[];
  role:       string;
  evidence:   string;
}

const POSITIONAL_CRIBS: CribSpec[] = [
  // ── Sentence-final particles ──────────────────────────────────────────────
  {
    evaWord: 'am',
    candidates: ['et', 'ac', 'at', 'ut', 'in', 'an', 'se', 've'],
    role: 'line-final',
    evidence: '72% line-final vs 14% baseline; near-exclusive line terminator',
  },
  {
    evaWord: 'oly',
    candidates: ['vel', 'aut', 'nec', 'nam', 'iam', 'ubi', 'dum', 'ergo'.slice(0,3)],
    role: 'line-final',
    evidence: '68% line-final; one of two dominant line terminators',
  },
  {
    evaWord: 'dam',
    candidates: ['nam', 'iam', 'vel', 'aut', 'nec', 'seu', 'seu'],
    role: 'line-final',
    evidence: '62% line-final; third strongest line terminator',
  },

  // ── Sentence-initial class ────────────────────────────────────────────────
  {
    evaWord: 'sol',
    candidates: ['sol', 'sed', 'sub', 'sic', 'sin', 'sis', 'sit', 'qui', 'hic'],
    role: 'line-initial',
    evidence: '55% line-initial; part of coherent s-initial sentence-opener class',
  },
  {
    evaWord: 'sain',
    candidates: ['sint', 'sane', 'sine', 'sive', 'soli', 'quod', 'quae', 'haec'],
    role: 'line-initial',
    evidence: '57% line-initial; highest line-initial rate in the s-initial class',
  },
  {
    evaWord: 'sar',
    candidates: ['sar', 'sub', 'sed', 'sex', 'nam', 'hac', 'hoc', 'vel'],
    role: 'line-initial',
    evidence: '39% line-initial; part of the s-initial sentence-opener class',
  },

  // ── Cross-section function words ──────────────────────────────────────────
  // Appear in ≥ 50% of folios in EVERY section (herbal, balneo, stars, pharma).
  // Cannot be botanical terms — they appear in sections with no plant content.
  {
    evaWord: 'daiin',
    candidates: ['autem', 'etiam', 'tamen', 'erunt', 'ipsum', 'illud', 'dicta',
                 'terra', 'bella', 'missa', 'bulla', 'penna', 'nulla', 'cella'],
    role: 'function-word',
    evidence: '95% herbal, 89% balneo, 92% stars, 100% pharma folio presence',
  },
  {
    evaWord: 'or',
    candidates: ['et', 'ac', 'at', 'in', 'ut', 'se', 've', 'id', 'is', 'ea'],
    role: 'function-word',
    evidence: '51-96% folio presence across all sections; high-frequency 2-char word',
  },
  {
    evaWord: 'dar',
    candidates: ['vel', 'aut', 'nec', 'nam', 'iam', 'sed', 'sub', 'per', 'pro'],
    role: 'function-word',
    evidence: '52-84% folio presence across all sections; likely a conjunction/particle',
  },
  {
    evaWord: 'dy',
    candidates: ['de', 're', 'ad', 'ab', 'ex', 'et', 'ut', 'si', 'ob', 'in'],
    role: 'function-word',
    evidence: '61-89% folio presence; also appears as standalone suffix in -dy',
  },
];

// ── EVA parser (ZL3b-n.txt) ──────────────────────────────────────────────────

function extractWords(line: string): string[] {
  let text = line.replace(/<[^>]+>/g, ' ');
  text = text.replace(/\{[^}]*\}/g, '').replace(/\[([^:\]]+):[^\]]*\]/g, '$1').replace(/@\d+;?/g, '').replace(/[!?%$]/g, '');
  return text.split(/[\s.,;-]+/).map(w => w.trim().toLowerCase()).filter(w => w.length >= 2 && /^[a-z]+$/.test(w));
}

interface ParsedLine { section: string; words: string[] }

function parseZL(raw: string): ParsedLine[] {
  const result: ParsedLine[] = [];
  let section = '?';
  for (const lineRaw of raw.split('\n')) {
    const t = lineRaw.trim();
    if (!t || t.startsWith('#')) continue;
    const sm = t.match(/\$I=([A-Za-z])/);
    if (sm) section = sm[1].toUpperCase();
    if (/^<f\d+[rv]/.test(t) && t.includes('>')) {
      const words = extractWords(t);
      if (words.length > 0) result.push({ section, words });
    }
  }
  return result;
}

// ── Latin scorer ─────────────────────────────────────────────────────────────
// Trigram model of medieval botanical Latin.

const LATIN_CORPUS_TEXT = `
et in non est sed quod aut vel nam iam nec cum per sub super de ad ex ab ut si
mandragora herba radix folia flores succus cortex semen fructus
habet vires virtutes partes colores odorem saporem
frigida calida sicca humida temperata medicina
recipe tere misce bibe pone impone accipe
contra dolorem febrem tumoris vulnus morbum
herba plantago betonica artemisia absinthium verbena melissa
sal mel vinum acetum aqua oleum
radix alba longa nigra parva magna
circa hortum locis cultis nascuntur iacent
sol luna stella caelum terra aqua ignis
ergo igitur autem tamen etiam enim ipsum
sed non est quod quia qua quae
illud hoc haec hic ille quomodo
omnis omne nullus nulla nihil
super supra intra extra ultra
`.trim();

function buildTrigramModel(corpus: string): Map<string, number> {
  const text = (' ' + corpus.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ') + ' ');
  const m = new Map<string, number>();
  for (let i = 0; i < text.length - 2; i++) m.set(text.slice(i, i + 3), (m.get(text.slice(i, i + 3)) ?? 0) + 1);
  return m;
}

const TRIGRAM_MODEL = buildTrigramModel(LATIN_CORPUS_TEXT);
const TRIGRAM_TOTAL = [...TRIGRAM_MODEL.values()].reduce((a, b) => a + b, 0);

function scoreLatinLm(text: string): number {
  const clean = ' ' + text.toLowerCase().replace(/[^a-z ]/g, '').replace(/\s+/g, ' ') + ' ';
  if (clean.length < 5) return 0;
  let logP = 0, n = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    logP += Math.log(((TRIGRAM_MODEL.get(tg) ?? 0) + 1) / (TRIGRAM_TOTAL + TRIGRAM_MODEL.size));
    n++;
  }
  return n === 0 ? 0 : Math.max(0, Math.min(1, (logP / n - (-8.5)) / ((-5.5) - (-8.5))));
}

// ── Crib utilities ───────────────────────────────────────────────────────────

type CharMap = Record<string, string>;

// Derive a partial EVA-char → Latin-char map from one crib pair.
// Returns null if length mismatch or internal contradiction.
function deriveCribMap(evaWord: string, latinWord: string): CharMap | null {
  if (evaWord.length !== latinWord.length) return null;
  const map: CharMap = {};
  for (let i = 0; i < evaWord.length; i++) {
    const ev = evaWord[i], la = latinWord[i];
    if (map[ev] !== undefined && map[ev] !== la) return null;  // internal contradiction
    map[ev] = la;
  }
  // Check many-to-one collision (two EVA chars → same Latin char)
  const usedLatin = new Set<string>();
  for (const v of Object.values(map)) {
    if (usedLatin.has(v)) return null;
    usedLatin.add(v);
  }
  return map;
}

// Merge two partial maps. Returns null on contradiction.
function mergeMaps(a: CharMap, b: CharMap): CharMap | null {
  const merged: CharMap = { ...a };
  const usedLatin = new Set(Object.values(a));
  for (const [ev, la] of Object.entries(b)) {
    if (merged[ev] !== undefined) {
      if (merged[ev] !== la) return null;  // same EVA char → different Latin char
    } else {
      if (usedLatin.has(la)) return null;  // different EVA char → same Latin char (bijection)
      merged[ev] = la;
      usedLatin.add(la);
    }
  }
  return merged;
}

// ── SA hill-climber ──────────────────────────────────────────────────────────

const LATIN_ALPHABET = 'etainsrolcumpdbghvfywkqjxz'.split('');

function buildFullMap(partial: CharMap, evaGlyphs: string[]): CharMap {
  const usedLatin = new Set(Object.values(partial));
  const freeTargets = LATIN_ALPHABET.filter(c => !usedLatin.has(c));
  const shuffled = [...freeTargets].sort(() => Math.random() - 0.5);
  const map: CharMap = { ...partial };
  let idx = 0;
  for (const g of evaGlyphs) {
    if (!(g in map)) { map[g] = shuffled[idx % shuffled.length]; idx++; }
  }
  return map;
}

function applyMap(words: string[], map: CharMap): string {
  return words.map(w => w.split('').map(c => map[c] ?? c).join('')).join(' ');
}

function runSA(
  sampleLines: ParsedLine[],
  partial: CharMap,
  evaGlyphs: string[],
): { map: CharMap; score: number } {
  const sampleWords = sampleLines.slice(0, 30).flatMap(l => l.words);
  const lockedGlyphs = new Set(Object.keys(partial));
  const mutableGlyphs = evaGlyphs.filter(g => !lockedGlyphs.has(g));

  let map = buildFullMap(partial, evaGlyphs);
  let bestScore = scoreLatinLm(applyMap(sampleWords, map));
  let bestMap = { ...map };

  for (let step = 0; step < SA_STEPS; step++) {
    const temp = 0.3 * Math.exp(-4 * step / SA_STEPS);
    if (mutableGlyphs.length < 2) break;
    const i = Math.floor(Math.random() * mutableGlyphs.length);
    const j = Math.floor(Math.random() * mutableGlyphs.length);
    if (i === j) continue;
    const gi = mutableGlyphs[i], gj = mutableGlyphs[j];
    const vi = map[gi], vj = map[gj];
    map[gi] = vj; map[gj] = vi;
    const s = scoreLatinLm(applyMap(sampleWords, map));
    if (s > bestScore || Math.random() < Math.exp((s - bestScore) / Math.max(temp, 1e-6))) {
      if (s > bestScore) { bestScore = s; bestMap = { ...map }; }
    } else { map[gi] = vi; map[gj] = vj; }
  }
  return { map: bestMap, score: bestScore };
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  console.log('Function-word crib attack — positional grammar findings');
  console.log(`Source: ${ZL_URL}\n`);

  const raw = await fetch(ZL_URL).then(r => r.text());
  const allLines = parseZL(raw);
  const herbalLines = allLines.filter(l => l.section === 'H');
  const allWords = allLines.flatMap(l => l.words);
  console.log(`Corpus: ${allWords.length} tokens, ${herbalLines.length} herbal lines\n`);

  // EVA glyph vocabulary
  const glyphFreq = new Map<string, number>();
  for (const w of allWords) for (const c of w) glyphFreq.set(c, (glyphFreq.get(c) ?? 0) + 1);
  const evaGlyphs = [...glyphFreq.entries()].sort((a, b) => b[1] - a[1]).map(([g]) => g);
  console.log(`EVA glyph set (${evaGlyphs.length}): ${evaGlyphs.slice(0, 15).join(' ')}\n`);

  // ── Phase 1: enumerate individual crib maps ──────────────────────────────
  console.log('═══ Phase 1: Individual crib compatibility ═══\n');

  interface CribEntry { spec: CribSpec; latin: string; partial: CharMap }
  const validCribs: CribEntry[] = [];

  for (const spec of POSITIONAL_CRIBS) {
    const compatible: string[] = [];
    const incompatible: string[] = [];
    for (const lat of spec.candidates) {
      const m = deriveCribMap(spec.evaWord, lat);
      if (m) { compatible.push(lat); validCribs.push({ spec, latin: lat, partial: m }); }
      else incompatible.push(lat);
    }
    const map0 = validCribs.find(e => e.spec === spec)?.partial;
    const mapStr = map0 ? Object.entries(map0).map(([k, v]) => `${k}→${v}`).join(' ') : '—';
    console.log(`  ${spec.evaWord.padEnd(8)} (${spec.role.padEnd(14)}) OK: ${compatible.join(', ').padEnd(30)} SKIP: ${incompatible.join(', ')}`);
    if (map0) console.log(`    chars: ${mapStr}`);
  }

  // ── Phase 2: pairwise compatibility check ────────────────────────────────
  console.log('\n═══ Phase 2: Pairwise compatibility ═══\n');

  // Group by role for targeted cross-role merging
  const byRole: Record<string, CribEntry[]> = {};
  for (const e of validCribs) {
    byRole[e.spec.role] = byRole[e.spec.role] ?? [];
    byRole[e.spec.role].push(e);
  }

  // Build candidate crib SETS: one line-final + one line-initial + one function-word
  // (representative selection — testing all triples is too large)
  const lineFinal  = byRole['line-final']   ?? [];
  const lineInit   = byRole['line-initial'] ?? [];
  const funcWord   = byRole['function-word'] ?? [];

  interface CribSet { entries: CribEntry[]; merged: CharMap }
  const sets: CribSet[] = [];

  // Try all combinations of one from each role (bounded)
  for (const lf of lineFinal) {
    for (const li of lineInit) {
      const m1 = mergeMaps(lf.partial, li.partial);
      if (!m1) continue;
      for (const fw of funcWord) {
        const m2 = mergeMaps(m1, fw.partial);
        if (!m2) continue;
        sets.push({ entries: [lf, li, fw], merged: m2 });
      }
    }
  }

  console.log(`Valid 3-crib sets (line-final + line-initial + function-word): ${sets.length}`);
  if (sets.length > 0) {
    const sample = sets.slice(0, 5);
    for (const s of sample) {
      const cribStr = s.entries.map(e => `${e.spec.evaWord}→${e.latin}`).join(' + ');
      const lockStr = Object.entries(s.merged).map(([k, v]) => `${k}→${v}`).join(' ');
      console.log(`  ${cribStr}`);
      console.log(`    locked: ${lockStr}\n`);
    }
  }

  // ── Phase 3: SA on compatible sets ───────────────────────────────────────
  console.log('\n═══ Phase 3: SA decoding on compatible crib sets ═══\n');
  console.log(`Running ${N_RESTARTS} SA restarts per set, ${SA_STEPS} steps each.\n`);

  interface Result { cribStr: string; locked: CharMap; bestScore: number; decode: string }
  const results: Result[] = [];

  // Test all sets (cap at 120 to keep runtime reasonable)
  const setsToTest = sets.slice(0, 120);
  console.log(`Testing ${setsToTest.length} sets...\n`);

  for (const s of setsToTest) {
    const cribStr = s.entries.map(e => `${e.spec.evaWord}→${e.latin}`).join(' + ');
    let bestScore = -Infinity;
    let bestMap: CharMap = {};

    for (let r = 0; r < N_RESTARTS; r++) {
      const { map, score } = runSA(herbalLines, s.merged, evaGlyphs);
      if (score > bestScore) { bestScore = score; bestMap = map; }
    }

    const sampleWords = herbalLines.slice(0, 5).flatMap(l => l.words);
    const decode = applyMap(sampleWords, bestMap).slice(0, 150);
    results.push({ cribStr, locked: s.merged, bestScore, decode });
  }

  // Also test single cribs (unconstrained, for baseline)
  console.log('Also testing top single cribs as baseline...\n');
  const singleCandidates = validCribs
    .filter(e => e.spec.evaWord === 'am' || e.spec.evaWord === 'sol' || e.spec.evaWord === 'daiin')
    .slice(0, 8);
  for (const e of singleCandidates) {
    let bestScore = -Infinity;
    let bestMap: CharMap = {};
    for (let r = 0; r < N_RESTARTS; r++) {
      const { map, score } = runSA(herbalLines, e.partial, evaGlyphs);
      if (score > bestScore) { bestScore = score; bestMap = map; }
    }
    const sampleWords = herbalLines.slice(0, 5).flatMap(l => l.words);
    const decode = applyMap(sampleWords, bestMap).slice(0, 150);
    results.push({ cribStr: `${e.spec.evaWord}→${e.latin} (single)`, locked: e.partial, bestScore, decode });
  }

  // ── Summary ──────────────────────────────────────────────────────────────
  results.sort((a, b) => b.bestScore - a.bestScore);

  console.log('\n═══ Top results ═══\n');
  console.log(`Score gate: ${SCORE_GATE}`);
  console.log('Rank  Score   Cribs + decode\n' + '─'.repeat(100));
  let shown = 0;
  for (const r of results.slice(0, 25)) {
    const flag = r.bestScore >= SCORE_GATE ? '★' : ' ';
    console.log(`${flag} ${r.bestScore.toFixed(4)}  ${r.cribStr}`);
    console.log(`          sample: "${r.decode}"\n`);
    shown++;
  }

  const promising = results.filter(r => r.bestScore >= SCORE_GATE);
  console.log(`\n${promising.length} crib set(s) cleared the gate (score ≥ ${SCORE_GATE}).`);

  if (promising.length > 0) {
    console.log('\n═══ Promising crib sets — CONSENSUS_LOCKED candidates ═══\n');
    console.log('These partial maps can be added to theory-loop.ts CONSENSUS_LOCKED');
    console.log('to seed the SA hill-climber with statistical constraints:\n');
    for (const r of promising.slice(0, 3)) {
      console.log(`  // ${r.cribStr}`);
      const entries = Object.entries(r.locked).map(([k, v]) => `${k}: '${v}'`).join(', ');
      console.log(`  { ${entries} }  // score=${r.bestScore.toFixed(4)}`);
      console.log();
    }
  } else {
    console.log('\nNo crib set cleared the gate.');
    console.log('Interpretation: positional cribs alone do not constrain the letter-level');
    console.log('substitution enough to reach readable Latin. Possible explanations:');
    console.log('  1. The cipher is not a simple letter substitution');
    console.log('  2. The target language is not Latin or Italian');
    console.log('  3. Word-level (not character-level) substitution is the correct model');
    console.log('  4. Function words are ciphered differently from content words');
  }

  // ── What the locked chars imply for "daiin" ───────────────────────────────
  console.log('\n═══ Sanity check: "daiin" decode across top locked maps ═══\n');
  for (const r of results.slice(0, 10)) {
    const decoded = 'daiin'.split('').map(c => r.locked[c] ?? '?').join('');
    const complete = !decoded.includes('?');
    console.log(`  ${r.cribStr.padEnd(45)} → "daiin"="${decoded}" ${complete ? '(complete)' : '(partial)'}`);
  }

  console.log('\nDone.');
}

main().catch(e => { console.error(e); process.exit(1); });

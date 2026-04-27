/**
 * Theory-Driven Decoding Loop — v3: Frequency-Constrained Generation
 *
 * Approach:
 * 1. Count EVA glyph frequencies across the corpus
 * 2. Generate candidate symbol maps by matching EVA frequencies to target
 *    language letter frequencies, with random perturbations to explore
 * 3. Mechanically apply each map to the EVA text
 * 4. Use the LLM as an EVALUATOR — score whether the decoded output looks
 *    like real language (not as a map generator)
 * 5. Test the best map across other folios for consistency
 * 6. Keep theories that score on both grounding and consistency
 */

import { resolveToken, resolveHost } from '../../../src/index.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FolioInfo {
  folio_id: string;
  plant_name: string;
  plant_latin: string;
  confidence: number;
  botanical_features: string[];
  eva_sample: string; // placeholder — we don't have per-folio EVA yet
}

export interface Theory {
  id: string;
  proposed_at: string;
  source_language: string;
  cipher_type: 'substitution' | 'polyalphabetic';
  target_folio: string;
  target_plant: string;
  symbol_map: Record<string, string>;
  keyword?: string;
  decoded_text: string;
  grounding_score: number;
  consistency_score: number;
  cross_folio_results: Array<{
    folio_id: string;
    plant_expected: string;
    decoded_text: string;
    grounding_score: number;
  }>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Per-folio EVA text cache — loaded from Delta on first use
let evaCorpusCache: Map<string, string> | null = null;

async function loadEvaCorpus(): Promise<Map<string, string>> {
  if (evaCorpusCache) return evaCorpusCache;
  const rows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
  `);
  evaCorpusCache = new Map(rows.map((r) => [r.folio_id, r.eva_text]));
  return evaCorpusCache;
}

const FALLBACK_EVA = 'daiin.chedy.qokeedy.shedy.otedy.qokain.chol.chor.shol.shory.cthy.dar.aly';

// ---------------------------------------------------------------------------
// Target language letter frequencies (by descending frequency)
// ---------------------------------------------------------------------------

const LANG_FREQ: Record<string, string[]> = {
  latin:   ['e','i','a','u','t','s','n','r','o','l','c','m','d','p','b','q','g','v','f','h','x','y','k','z','j','w'],
  italian: ['e','a','i','o','n','l','r','t','s','c','d','u','p','m','v','g','b','f','h','z','q','x','y','w','k','j'],
  greek:   ['α','ι','ο','ε','ν','σ','τ','η','ρ','κ','π','μ','λ','υ','δ','θ','γ','ω','φ','χ','β','ξ','ζ','ψ'],
};

// ---------------------------------------------------------------------------
// EVA glyph tokenizer — handles multi-char glyphs (ch, sh, th, etc.)
// ---------------------------------------------------------------------------

const EVA_MULTI_GLYPHS = ['ch', 'sh', 'th', 'ct', 'ck', 'qo', 'ok', 'ol', 'ai', 'ee', 'dy', 'ey', 'or', 'ar'];

function tokenizeEva(evaText: string): string[] {
  const text = evaText.replace(/\./g, ' ');
  const tokens: string[] = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] === ' ') { i++; continue; }
    let matched = false;
    for (const glyph of EVA_MULTI_GLYPHS) {
      if (text.substring(i, i + glyph.length) === glyph) {
        tokens.push(glyph);
        i += glyph.length;
        matched = true;
        break;
      }
    }
    if (!matched) { tokens.push(text[i]); i++; }
  }
  return tokens;
}

/** Count glyph frequencies across all provided EVA texts, sorted descending. */
function countEvaFrequencies(evaTexts: string[]): Array<[string, number]> {
  const counts: Record<string, number> = {};
  for (const text of evaTexts) {
    for (const glyph of tokenizeEva(text)) {
      counts[glyph] = (counts[glyph] ?? 0) + 1;
    }
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]);
}

// ---------------------------------------------------------------------------
// Frequency-matched map generator
// ---------------------------------------------------------------------------

/**
 * Generate a symbol map by aligning EVA glyph frequencies with target language
 * letter frequencies. `perturbationRate` controls how many random swaps to
 * apply (0 = pure frequency match, 1 = fully random).
 */
function generateFrequencyMap(
  evaFreqs: Array<[string, number]>,
  language: string,
  perturbationRate: number = 0,
): Record<string, string> {
  const targetLetters = [...(LANG_FREQ[language] ?? LANG_FREQ.latin)];
  const map: Record<string, string> = {};

  // Assign by frequency rank
  for (let i = 0; i < evaFreqs.length; i++) {
    const evaGlyph = evaFreqs[i][0];
    const letterIdx = Math.min(i, targetLetters.length - 1);
    map[evaGlyph] = targetLetters[letterIdx];
  }

  // Apply random perturbations — swap N pairs
  const glyphs = Object.keys(map);
  const numSwaps = Math.floor(glyphs.length * perturbationRate);
  for (let s = 0; s < numSwaps; s++) {
    const i = Math.floor(Math.random() * glyphs.length);
    const j = Math.floor(Math.random() * glyphs.length);
    if (i !== j) {
      const tmp = map[glyphs[i]];
      map[glyphs[i]] = map[glyphs[j]];
      map[glyphs[j]] = tmp;
    }
  }

  return map;
}

// ---------------------------------------------------------------------------
// LLM evaluator — scores decoded text for language plausibility
// ---------------------------------------------------------------------------

async function llmEvaluateDecoding(
  decodedText: string,
  language: string,
  plantName: string,
): Promise<{ plausibility: number; reasoning: string }> {
  const prompt = `You are a medieval language expert evaluating whether decoded text is real ${language}.

DECODED TEXT: "${decodedText}"
EXPECTED TOPIC: A description of the plant "${plantName}"

Score this text on a scale from 0.0 to 1.0:
- 0.0 = complete gibberish, no recognizable words
- 0.2 = a few letter sequences that could be word fragments
- 0.4 = some recognizable words but mostly nonsense
- 0.6 = multiple real ${language} words, some grammatical structure
- 0.8 = mostly readable ${language}, botanical/medical context visible
- 1.0 = fluent medieval ${language} about this plant

Return ONLY a JSON object:
{"plausibility": 0.0, "reasoning": "brief explanation"}`;

  try {
    const response = await callFMAPI(prompt);
    const cleaned = response.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
    const parsed = JSON.parse(cleaned);
    return {
      plausibility: typeof parsed.plausibility === 'number' ? parsed.plausibility : 0,
      reasoning: parsed.reasoning || '',
    };
  } catch {
    return { plausibility: 0, reasoning: 'evaluation failed' };
  }
}

// ---------------------------------------------------------------------------
// Dictionary + bigram scoring for hill-climbing
// ---------------------------------------------------------------------------

/** Common bigrams by language. */
const COMMON_BIGRAMS: Record<string, string[]> = {
  latin:   ['er','in','um','us','is','it','at','en','re','nt','am','es','ra','an','ti','ur','ta','tu','ae','et','ar','al','de','te','or','ri'],
  italian: ['er','in','de','la','re','an','le','al','en','el','on','ar','to','ra','at','ne','te','co','io','di','no','ti','ta','li','un','si'],
  greek:   ['αι','ου','ει','ον','τη','εν','ις','αν','ερ','ος','ασ','ιν','ησ','τα','ων','οι','εσ','ρο','κα','με','νο','πο','λα','αρ'],
};

/**
 * Medieval Latin word list — common words in herbal/medical manuscripts.
 * Includes botanical terms, body parts, ailments, and high-frequency function words.
 */
const LATIN_DICT = new Set([
  // Function words
  'et','in','de','ad','cum','per','est','non','ex','ut','ab','hoc','quod','qui','que','sed',
  'aut','vel','si','eius','habet','sunt','fuit','esse','item','vero','nam','enim','ita','sic',
  // Botanical
  'herba','radix','folia','folium','flos','flores','semen','cortex','ramus','rami','arbor',
  'planta','fructus','succus','oleum','aqua','terra','humus','viridis','alba','nigra','rubra',
  // Medical / pharmaceutical
  'virtus','vires','calida','frigida','humida','sicca','cura','morbus','dolor','febris',
  'venenum','remedium','potio','emplastrum','pulvis','unguentum','medicina','sanguis',
  'corpus','caput','oculus','manus','pes','stomachus','hepar','vulnus','apostema',
  // Properties
  'calor','humor','natura','forma','species','genus','color','odor','sapor',
  // Verbs / descriptors
  'facit','valet','sanat','tollit','purgat','mundificat','confortat','iuvat','crescit',
  'habet','datur','bibitur','coquitur','ponitur','colligitur','nascitur','invenitur',
  // Plant names that might appear
  'mandragora','cannabis','papaver','rosa','lilium','salvia','urtica','absinthium',
  'artemisia','centaurea','plantago','verbena','malva','betonica','ruta','melissa',
  'eryngium','campanula','hedera','carduus','cirsium','ranunculus','aconitum',
]);

/**
 * Medieval Italian word list — common words in herbals and recipe books.
 */
const ITALIAN_DICT = new Set([
  // Function words
  'e','il','la','le','lo','di','da','in','con','per','che','non','si','del','dei','della',
  'delle','una','uno','ha','sono','sua','suo','questo','quella','come','molto','bene','ogni',
  // Botanical
  'erba','radice','foglia','foglie','fiore','fiori','seme','semi','corteccia','ramo','rami',
  'albero','pianta','frutto','succo','olio','acqua','terra','verde','bianco','nero','rosso',
  // Medical
  'virtu','cura','male','dolore','febbre','veleno','rimedio','polvere','unguento','medicina',
  'sangue','corpo','testa','occhio','mano','piede','stomaco','fegato','ferita',
  // Properties
  'caldo','freddo','umido','secco','forte','grande','piccolo','buono','amaro','dolce',
  // Verbs
  'fa','vale','guarisce','purga','cresce','nasce','trova','mette','prende','beve','cuoce',
  // Plants
  'mandragora','canapa','papavero','rosa','giglio','salvia','ortica','assenzio',
]);

const DICT_BY_LANG: Record<string, Set<string>> = {
  latin: LATIN_DICT,
  italian: ITALIAN_DICT,
};

function quickBigramScore(text: string, language: string): number {
  const clean = text.toLowerCase().replace(/[^a-zα-ω]/g, '');
  if (clean.length < 4) return 0;

  const bigrams = COMMON_BIGRAMS[language] ?? COMMON_BIGRAMS.latin;
  let hits = 0;
  for (let i = 0; i < clean.length - 1; i++) {
    const bg = clean.slice(i, i + 2);
    if (bigrams.includes(bg)) hits++;
  }
  return hits / (clean.length - 1);
}

/**
 * Dictionary score — fraction of decoded words that appear in the word list.
 * Much stronger signal than bigrams: rewards actual word formation.
 */
function dictionaryScore(text: string, language: string): number {
  const dict = DICT_BY_LANG[language];
  if (!dict) return 0;

  const words = text.toLowerCase().replace(/[^a-z\s]/g, '').split(/\s+/).filter((w) => w.length >= 2);
  if (words.length === 0) return 0;

  let hits = 0;
  for (const word of words) {
    if (dict.has(word)) {
      hits++;
    } else {
      // Partial credit: check if any dict word is a prefix/suffix of the decoded word
      // This catches inflected forms (e.g. "herbam" matches "herba")
      for (const entry of dict) {
        if (entry.length >= 4 && (word.startsWith(entry) || word.endsWith(entry.slice(-4)))) {
          hits += 0.3;
          break;
        }
      }
    }
  }
  return hits / words.length;
}

/**
 * Combined hill-climbing score: dictionary match (strong signal) + bigram
 * similarity (weak but continuous signal). Dictionary is weighted 3x because
 * a single word match is worth far more than bigram statistics.
 */
function hillClimbScore(text: string, language: string): number {
  const dict = dictionaryScore(text, language);
  const bigram = quickBigramScore(text, language);
  return dict * 0.75 + bigram * 0.25;
}

async function executeSql(statement: string): Promise<Array<Record<string, string>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');

  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
  });

  const data = (await res.json()) as {
    result?: { data_array?: string[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };

  if (data.status?.state === 'FAILED') {
    throw new Error(`SQL failed: ${data.status.error?.message}`);
  }

  const columns = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string> = {};
    columns.forEach((col, i) => { obj[col] = row[i]; });
    return obj;
  });
}

async function callFMAPI(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await resolveToken();
  const model = process.env.MODEL ?? 'databricks-claude-sonnet-4-6';

  const res = await fetch(`${host}/serving-endpoints/${model}/invocations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 4096,
    }),
  });

  const data = (await res.json()) as {
    choices?: Array<{ message?: { content?: string } }>;
  };

  return data.choices?.[0]?.message?.content ?? '';
}

// ---------------------------------------------------------------------------
// Load folio data
// ---------------------------------------------------------------------------

let folioCache: FolioInfo[] | null = null;

export async function loadFolios(): Promise<FolioInfo[]> {
  if (folioCache) return folioCache;

  const [rows, evaCorpus] = await Promise.all([
    executeSql(`
      SELECT folio_id, subject_candidates, botanical_features
      FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
      WHERE section = 'herbal'
      ORDER BY folio_id
    `),
    loadEvaCorpus(),
  ]);

  folioCache = rows.map((r) => {
    const candidates = JSON.parse(r.subject_candidates || '[]');
    const top = candidates[0] || {};
    return {
      folio_id: r.folio_id,
      plant_name: top.name || 'unknown',
      plant_latin: top.latin || '',
      confidence: top.confidence || 0,
      botanical_features: JSON.parse(r.botanical_features || '[]'),
      eva_sample: evaCorpus.get(r.folio_id) || FALLBACK_EVA,
    };
  });

  console.log(`[theory-loop] Loaded ${folioCache.length} folios, ${evaCorpus.size} with distinct EVA text`);
  return folioCache;
}

// ---------------------------------------------------------------------------
// Theory generation
// ---------------------------------------------------------------------------

/** Hill-climbing iterations per theory round (cheap — no LLM calls during climb). */
const HILL_CLIMB_STEPS = 500;
/** Number of initial seed maps to try before hill-climbing the best. */
const SEED_MAPS = 4;

/**
 * Mutate a symbol map by swapping 1-2 random character assignments.
 * Returns a new map (does not modify the input).
 */
function mutateMap(map: Record<string, string>): Record<string, string> {
  const result = { ...map };
  const glyphs = Object.keys(result);
  if (glyphs.length < 2) return result;

  const numSwaps = Math.random() < 0.7 ? 1 : 2;
  for (let s = 0; s < numSwaps; s++) {
    const i = Math.floor(Math.random() * glyphs.length);
    const j = Math.floor(Math.random() * glyphs.length);
    if (i !== j) {
      const tmp = result[glyphs[i]];
      result[glyphs[i]] = result[glyphs[j]];
      result[glyphs[j]] = tmp;
    }
  }
  return result;
}

export async function proposeTheory(
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  cipherType: 'substitution' | 'polyalphabetic' = 'substitution',
): Promise<Theory> {
  const theoryId = Math.random().toString(36).slice(2, 10);
  const evaText = targetFolio.eva_sample;

  // Step 1: Count EVA glyph frequencies across ALL folios
  const allEvaTexts = allFolios.map((f) => f.eva_sample).filter(Boolean);
  const evaFreqs = countEvaFrequencies(allEvaTexts);

  // Step 2: Generate seed maps with varying perturbation, score with dictionary+bigram
  const seeds: Array<{ map: Record<string, string>; decoded: string; score: number }> = [];

  for (let s = 0; s < SEED_MAPS; s++) {
    const perturbation = s / (SEED_MAPS - 1) * 0.4;
    const map = generateFrequencyMap(evaFreqs, sourceLanguage, perturbation);
    const decoded = applyMap(evaText, map);
    const score = hillClimbScore(decoded, sourceLanguage);
    seeds.push({ map, decoded, score });
  }

  // Pick the best seed
  seeds.sort((a, b) => b.score - a.score);
  let bestMap = seeds[0].map;
  let bestDecoded = seeds[0].decoded;
  let bestScore = seeds[0].score;

  console.log(`[theory-loop]   seeds=${SEED_MAPS} best_seed=${bestScore.toFixed(3)} dict=${dictionaryScore(bestDecoded, sourceLanguage).toFixed(3)} starting hill-climb...`);

  // Step 3: Hill-climb using dictionary + bigram score as the gradient.
  // Dictionary hits are the strong signal; bigrams provide continuity.
  // No LLM calls during the climb — pure algorithmic search.
  let bestHillScore = hillClimbScore(bestDecoded, sourceLanguage);
  let improvements = 0;

  for (let step = 0; step < HILL_CLIMB_STEPS; step++) {
    const candidate = mutateMap(bestMap);
    const decoded = applyMap(evaText, candidate);
    const score = hillClimbScore(decoded, sourceLanguage);

    if (score > bestHillScore) {
      bestMap = candidate;
      bestDecoded = decoded;
      bestHillScore = score;
      improvements++;
    }
  }

  const dictScore = dictionaryScore(bestDecoded, sourceLanguage);
  const bigramFinal = quickBigramScore(bestDecoded, sourceLanguage);
  bestScore = bestHillScore;

  let keyword: string | undefined;
  if (cipherType === 'polyalphabetic') {
    const keywords = ['HERBA', 'FLORA', 'RADIX', 'FOLIA', 'SEMEN', 'VIRTUS', 'MEDICA', 'PLANTA'];
    keyword = keywords[Math.floor(Math.random() * keywords.length)];
  }

  console.log(`[theory-loop]   hill-climb: ${improvements} improvements in ${HILL_CLIMB_STEPS} steps, dict=${dictScore.toFixed(3)} bigram=${bigramFinal.toFixed(3)} combined=${bestHillScore.toFixed(3)}`);

  // Step 4: Test cross-folio consistency
  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 5);

  for (let fi = 0; fi < testFolios.length; fi++) {
    const testFolio = testFolios[fi];
    const effectiveMap = cipherType === 'polyalphabetic' && keyword
      ? applyKeywordShift(bestMap, keyword, fi)
      : bestMap;
    const decoded = applyMap(testFolio.eva_sample, effectiveMap);

    const expectedTerms = [
      testFolio.plant_name.toLowerCase(),
      testFolio.plant_latin.toLowerCase(),
      ...testFolio.botanical_features.map((f) => f.toLowerCase()),
    ].filter(Boolean);

    const matchScore = scoreOverlap(decoded, expectedTerms);

    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: matchScore,
    });
  }

  // Step 5: Score the MECHANICAL decode against expected terms
  const primaryTerms = [
    targetFolio.plant_name.toLowerCase(),
    targetFolio.plant_latin.toLowerCase(),
    ...targetFolio.botanical_features.map((f) => f.toLowerCase()),
  ].filter(Boolean);
  const primaryGrounding = scoreOverlap(bestDecoded, primaryTerms);

  const consistencyScore = crossFolioResults.length > 0
    ? crossFolioResults.reduce((sum, r) => sum + r.grounding_score, 0) / crossFolioResults.length
    : 0;

  return {
    id: theoryId,
    proposed_at: new Date().toISOString(),
    source_language: sourceLanguage,
    cipher_type: cipherType,
    target_folio: targetFolio.folio_id,
    target_plant: targetFolio.plant_name,
    symbol_map: bestMap,
    keyword,
    decoded_text: bestDecoded,
    grounding_score: primaryGrounding,
    consistency_score: consistencyScore,
    cross_folio_results: crossFolioResults,
  };
}

// ---------------------------------------------------------------------------
// Skeptic: challenge a theory
// ---------------------------------------------------------------------------

export async function challengeTheory(theory: Theory, allFolios: FolioInfo[]): Promise<string> {
  const skepticPrompt = `You are a skeptical cryptanalyst reviewing a Voynich Manuscript decoding theory.

THEORY: Folio ${theory.target_folio} depicts ${theory.target_plant}.
The proposed symbol map decodes the EVA text as: "${theory.decoded_text}"
Source language: ${theory.source_language}

CROSS-FOLIO TEST RESULTS:
${theory.cross_folio_results.map((r) =>
    `  ${r.folio_id} (expected: ${r.plant_expected}): decoded to "${r.decoded_text}" — grounding: ${r.grounding_score.toFixed(2)}`
  ).join('\n')}

PRIMARY GROUNDING: ${theory.grounding_score.toFixed(3)}
CROSS-FOLIO CONSISTENCY: ${theory.consistency_score.toFixed(3)}

TASK: Identify the strongest objection to this theory. Consider:
1. Does the decoded text contain anachronistic terms?
2. Is the symbol map internally consistent (same EVA char always maps to same letter)?
3. Do the cross-folio results make sense, or does the map produce gibberish elsewhere?
4. Is the decoded text grammatically plausible for ${theory.source_language}?

Return a JSON object:
{
  "verdict": "plausible" | "weak" | "rejected",
  "strongest_objection": "...",
  "confidence": 0.0-1.0
}`;

  const response = await callFMAPI(skepticPrompt);
  return response;
}

// ---------------------------------------------------------------------------
// Scoring helpers (same as grounder)
// ---------------------------------------------------------------------------

function applyKeywordShift(
  baseMap: Record<string, string>,
  keyword: string,
  folioIndex: number,
): Record<string, string> {
  const kw = keyword.toUpperCase();
  const shiftChar = kw[folioIndex % kw.length];
  const shift = shiftChar.charCodeAt(0) - 'A'.charCodeAt(0);
  if (shift === 0) return baseMap;

  const shifted: Record<string, string> = {};
  for (const [eva, plainChar] of Object.entries(baseMap)) {
    if (/^[a-z]$/i.test(plainChar)) {
      const base = plainChar.toLowerCase().charCodeAt(0) - 'a'.charCodeAt(0);
      const newChar = String.fromCharCode(((base + shift) % 26) + 'a'.charCodeAt(0));
      shifted[eva] = newChar;
    } else {
      shifted[eva] = plainChar;
    }
  }
  return shifted;
}

function applyMap(evaText: string, symbolMap: Record<string, string>): string {
  const keys = Object.keys(symbolMap).sort((a, b) => b.length - a.length);
  let result = '';
  let i = 0;
  const text = evaText.replace(/\./g, ' ');
  while (i < text.length) {
    if (text[i] === ' ') { result += ' '; i++; continue; }
    let matched = false;
    for (const key of keys) {
      if (text.substring(i, i + key.length) === key) {
        result += symbolMap[key] || key;
        i += key.length;
        matched = true;
        break;
      }
    }
    if (!matched) { result += text[i]; i++; }
  }
  return result;
}

function scoreOverlap(text: string, terms: string[]): number {
  if (terms.length === 0) return 0;
  const decoded = text.toLowerCase().replace(/[^a-z\s]/g, ' ');
  const tokens = decoded.split(/\s+/).filter((t) => t.length > 2);
  if (tokens.length === 0) return 0;

  let score = 0;
  for (const term of terms) {
    const tl = term.toLowerCase();
    if (tokens.includes(tl)) { score += 1.0; continue; }
    if (decoded.includes(tl)) { score += 0.7; continue; }
    if (tl.length >= 4 && tokens.some((t) => t.startsWith(tl.slice(0, 5)))) { score += 0.4; continue; }
  }
  return Math.min(1.0, score / Math.max(tokens.length, 1));
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

export async function runTheoryLoop(maxRounds: number = 20): Promise<Theory[]> {
  const folios = await loadFolios();
  const highConfidence = folios.filter((f) => f.confidence >= 0.5);
  const languages = ['latin', 'italian', 'greek'];

  console.log(`[theory-loop] Starting with ${highConfidence.length} high-confidence folios`);

  const theories: Theory[] = [];

  for (let round = 0; round < maxRounds; round++) {
    // Pick a random folio and language
    const folio = highConfidence[Math.floor(Math.random() * highConfidence.length)];
    const lang = languages[Math.floor(Math.random() * languages.length)];

    const cipherType = round % 3 === 2 ? 'polyalphabetic' : 'substitution';

    console.log(`[theory-loop] Round ${round}: ${folio.folio_id} (${folio.plant_name}) in ${lang} [${cipherType}]`);

    try {
      const theory = await proposeTheory(folio, folios, lang, cipherType);

      console.log(`[theory-loop]   grounding=${theory.grounding_score.toFixed(3)} consistency=${theory.consistency_score.toFixed(3)} decoded="${theory.decoded_text.slice(0, 50)}"`);

      // Challenge the theory
      const challenge = await challengeTheory(theory, folios);
      let verdict = 'unknown';
      try {
        const cleaned = challenge.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
        const parsed = JSON.parse(cleaned);
        verdict = parsed.verdict || 'unknown';
        console.log(`[theory-loop]   skeptic: ${verdict} — ${(parsed.strongest_objection || '').slice(0, 80)}`);
      } catch {
        console.log(`[theory-loop]   skeptic: unparseable`);
      }

      theories.push(theory);

      // Persist to Delta
      await persistTheory(theory, verdict);

    } catch (err) {
      console.error(`[theory-loop] Round ${round} failed:`, err);
    }
  }

  // Report best theories
  const sorted = theories.sort((a, b) =>
    (b.grounding_score + b.consistency_score) - (a.grounding_score + a.consistency_score)
  );

  console.log(`\n[theory-loop] === TOP 5 THEORIES ===`);
  for (const t of sorted.slice(0, 5)) {
    console.log(`  ${t.target_folio} (${t.target_plant}): grd=${t.grounding_score.toFixed(3)} cons=${t.consistency_score.toFixed(3)} lang=${t.source_language} cipher=${t.cipher_type}`);
    console.log(`    "${t.decoded_text.slice(0, 60)}"`);
  }

  return sorted;
}

async function persistTheory(theory: Theory, verdict: string): Promise<void> {
  try {
    const id = theory.id.replace(/'/g, "''");
    const folio = theory.target_folio.replace(/'/g, "''");
    const plant = theory.target_plant.replace(/'/g, "''");
    const lang = theory.source_language.replace(/'/g, "''");
    const decoded = theory.decoded_text.replace(/'/g, "''").slice(0, 500);
    const symbolMap = JSON.stringify(theory.symbol_map).replace(/'/g, "''");
    const crossFolio = JSON.stringify(theory.cross_folio_results).replace(/'/g, "''");

    const cipherType = theory.cipher_type.replace(/'/g, "''");

    await executeSql(`
      CREATE TABLE IF NOT EXISTS serverless_stable_qh44kx_catalog.voynich.theories (
        id STRING, proposed_at TIMESTAMP, source_language STRING,
        cipher_type STRING,
        target_folio STRING, target_plant STRING,
        symbol_map STRING, decoded_text STRING,
        grounding_score DOUBLE, consistency_score DOUBLE,
        cross_folio_results STRING, verdict STRING
      )
    `);
    // Schema migration: add cipher_type if table predates it
    await executeSql(`ALTER TABLE serverless_stable_qh44kx_catalog.voynich.theories ADD COLUMNS (cipher_type STRING)`).catch(() => {
      // Column already exists — ignore
    });

    await executeSql(`
      INSERT INTO serverless_stable_qh44kx_catalog.voynich.theories
        (id, proposed_at, source_language, cipher_type, target_folio, target_plant,
         symbol_map, decoded_text, grounding_score, consistency_score,
         cross_folio_results, verdict)
      VALUES (
        '${id}', current_timestamp(), '${lang}',
        '${cipherType}',
        '${folio}', '${plant}',
        '${symbolMap}', '${decoded}',
        ${theory.grounding_score}, ${theory.consistency_score},
        '${crossFolio}', '${verdict}'
      )
    `);
  } catch (err) {
    console.error(`[theory-loop] Failed to persist theory:`, err);
  }
}

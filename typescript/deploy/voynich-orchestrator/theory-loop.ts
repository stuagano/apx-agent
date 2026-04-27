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

import { resolveToken, resolveHost } from './appkit-agent/index.mjs';

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

/** Number of candidate maps to generate and evaluate per theory round. */
const CANDIDATES_PER_ROUND = 8;

export async function proposeTheory(
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  cipherType: 'substitution' | 'polyalphabetic' = 'substitution',
): Promise<Theory> {
  const theoryId = Math.random().toString(36).slice(2, 10);
  const evaText = targetFolio.eva_sample;

  // Step 1: Count EVA glyph frequencies across ALL folios (not just target)
  const allEvaTexts = allFolios.map((f) => f.eva_sample).filter(Boolean);
  const evaFreqs = countEvaFrequencies(allEvaTexts);

  // Step 2: Generate N candidate maps with varying perturbation rates
  const candidates: Array<{ map: Record<string, string>; keyword?: string; decoded: string }> = [];

  for (let c = 0; c < CANDIDATES_PER_ROUND; c++) {
    // Perturbation ramps from 0 (pure frequency match) to 0.6 (heavily shuffled)
    const perturbation = c / (CANDIDATES_PER_ROUND - 1) * 0.6;
    const map = generateFrequencyMap(evaFreqs, sourceLanguage, perturbation);
    let keyword: string | undefined;

    if (cipherType === 'polyalphabetic') {
      // Pick a random keyword from common medieval Latin/Italian words
      const keywords = ['HERBA', 'FLORA', 'RADIX', 'FOLIA', 'SEMEN', 'VIRTUS', 'MEDICA', 'PLANTA'];
      keyword = keywords[Math.floor(Math.random() * keywords.length)];
    }

    const decoded = applyMap(evaText, map);
    candidates.push({ map, keyword, decoded });
  }

  // Step 3: Use LLM to evaluate the top candidates for language plausibility
  // Pre-filter: skip candidates that are too short or all same character
  const viable = candidates.filter((c) => {
    const unique = new Set(c.decoded.replace(/\s/g, '').split(''));
    return unique.size >= 5 && c.decoded.length >= 20;
  });

  // Evaluate top candidates with LLM (limit to 4 to control cost)
  const toEvaluate = viable.slice(0, 4);
  const evaluations = await Promise.all(
    toEvaluate.map(async (c) => {
      const eval_ = await llmEvaluateDecoding(c.decoded, sourceLanguage, targetFolio.plant_name);
      return { ...c, plausibility: eval_.plausibility, reasoning: eval_.reasoning };
    }),
  );

  // Pick the best candidate by plausibility
  evaluations.sort((a, b) => b.plausibility - a.plausibility);
  const best = evaluations[0] ?? candidates[0];

  const symbolMap = best.map;
  const keyword = best.keyword;
  const primaryDecoded = best.decoded;
  const plausibility = 'plausibility' in best ? (best as any).plausibility : 0;

  console.log(`[theory-loop]   candidates=${candidates.length} viable=${viable.length} best_plausibility=${plausibility.toFixed(3)}`);

  // Step 4: Test cross-folio consistency — apply the SAME map to other folios
  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 5);

  for (let fi = 0; fi < testFolios.length; fi++) {
    const testFolio = testFolios[fi];
    const effectiveMap = cipherType === 'polyalphabetic' && keyword
      ? applyKeywordShift(symbolMap, keyword, fi)
      : symbolMap;
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
  const primaryGrounding = scoreOverlap(primaryDecoded, primaryTerms);

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
    symbol_map: symbolMap,
    keyword,
    decoded_text: primaryDecoded,
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

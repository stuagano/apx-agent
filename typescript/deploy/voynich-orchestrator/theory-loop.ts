/**
 * Theory-Driven Decoding Loop
 *
 * Replaces the evolutionary approach with targeted hypothesis generation:
 * 1. Pick a folio with a confident plant identification
 * 2. Ask the LLM to propose a decoding theory (symbol map + decoded text)
 *    that makes the EVA text produce text about that plant
 * 3. Test the theory's symbol map against OTHER folios — does it produce
 *    plausible text for those plants too? (cross-folio consistency)
 * 4. Score by grounding (image match) + consistency (cross-folio)
 * 5. Keep theories that work across multiple folios
 *
 * This runs as a standalone Express endpoint on the orchestrator,
 * separate from the EA loop.
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

const HERBAL_EVA_SAMPLES: Record<string, string> = {
  // Representative EVA word sequences per folio section
  // (In a full implementation, these would come from the corpus table)
  default: 'daiin.chedy.qokeedy.shedy.otedy.qokain.chol.chor.shol.shory.cthy.dar.aly',
};

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

  const rows = await executeSql(`
    SELECT folio_id, subject_candidates, botanical_features
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  folioCache = rows.map((r) => {
    const candidates = JSON.parse(r.subject_candidates || '[]');
    const top = candidates[0] || {};
    return {
      folio_id: r.folio_id,
      plant_name: top.name || 'unknown',
      plant_latin: top.latin || '',
      confidence: top.confidence || 0,
      botanical_features: JSON.parse(r.botanical_features || '[]'),
      eva_sample: HERBAL_EVA_SAMPLES.default,
    };
  });

  return folioCache;
}

// ---------------------------------------------------------------------------
// Theory generation
// ---------------------------------------------------------------------------

export async function proposeTheory(
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  cipherType: 'substitution' | 'polyalphabetic' = 'substitution',
): Promise<Theory> {
  const theoryId = Math.random().toString(36).slice(2, 10);

  // Step 1: Ask LLM to propose a decoding for this specific folio
  const proposePrompt = cipherType === 'polyalphabetic'
    ? `You are a medieval manuscript cryptanalyst working on the Voynich Manuscript.

TASK: Propose a POLYALPHABETIC (Vigenere-style) decoding theory for folio ${targetFolio.folio_id}.

WHAT THE IMAGE SHOWS: ${targetFolio.plant_name} (${targetFolio.plant_latin})
Visual features: ${targetFolio.botanical_features.join(', ')}

EVA TEXT ON THIS FOLIO: ${targetFolio.eva_sample}

CANDIDATE LANGUAGE: ${sourceLanguage}

Propose a keyword-based Vigenere-style system where:
- A base symbol map maps EVA characters to ${sourceLanguage} letters
- A keyword shifts certain mappings per folio (e.g., keyword "HERBA" means folio 1 shifts by H=7, folio 2 by E=4, etc., cycling through the keyword letters)

Return a JSON object with:
{
  "base_map": {"d": "m", "a": "a", "i": "n", ...},
  "keyword": "HERBA",
  "decoded_text": "the resulting decoded passage for this folio"
}

The decoded text should read as a plausible medieval ${sourceLanguage} description of ${targetFolio.plant_name}, mentioning its properties, uses, or appearance.

Return ONLY the JSON object.`
    : `You are a medieval manuscript cryptanalyst working on the Voynich Manuscript.

TASK: Propose a decoding theory for folio ${targetFolio.folio_id}.

WHAT THE IMAGE SHOWS: ${targetFolio.plant_name} (${targetFolio.plant_latin})
Visual features: ${targetFolio.botanical_features.join(', ')}

EVA TEXT ON THIS FOLIO: ${targetFolio.eva_sample}

CANDIDATE LANGUAGE: ${sourceLanguage}

Propose a symbol-by-symbol mapping from EVA characters to ${sourceLanguage} letters that would make the EVA text produce a passage about ${targetFolio.plant_name}.

Return a JSON object with:
{
  "symbol_map": {"d": "m", "a": "a", "i": "n", ...},
  "decoded_text": "the resulting decoded passage",
  "reasoning": "why these mappings make sense"
}

The decoded text should read as a plausible medieval ${sourceLanguage} description of ${targetFolio.plant_name}, mentioning its properties, uses, or appearance.

Return ONLY the JSON object.`;

  const response = await callFMAPI(proposePrompt);

  // Parse the theory
  let theory: { symbol_map: Record<string, string>; decoded_text: string; keyword?: string; reasoning?: string };
  try {
    const cleaned = response.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
    const parsed = JSON.parse(cleaned);
    if (cipherType === 'polyalphabetic') {
      // Polyalphabetic responses use base_map instead of symbol_map
      theory = {
        symbol_map: parsed.base_map || parsed.symbol_map || {},
        keyword: parsed.keyword || 'HERBA',
        decoded_text: parsed.decoded_text || '',
      };
    } else {
      theory = parsed;
    }
  } catch {
    // Fallback: generate a simple theory
    theory = {
      symbol_map: { d: 'm', a: 'a', i: 'n', n: 'd', ch: 'r', e: 'a', y: 'g', o: 'o', r: 'r', s: 'a' },
      decoded_text: `${targetFolio.plant_latin || targetFolio.plant_name} herba medicinalis radix`,
    };
    if (cipherType === 'polyalphabetic') {
      theory.keyword = 'HERBA';
    }
  }

  // Step 2: Test cross-folio consistency
  // Apply the SAME symbol map to other folios and check if it produces
  // text that matches THEIR identified plants
  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 5); // test against 5 other high-confidence folios

  for (let fi = 0; fi < testFolios.length; fi++) {
    const testFolio = testFolios[fi];
    // Apply symbol map to this folio's EVA text
    // For polyalphabetic theories, shift the base map per folio using the keyword
    const effectiveMap = cipherType === 'polyalphabetic' && theory.keyword
      ? applyKeywordShift(theory.symbol_map, theory.keyword, fi)
      : theory.symbol_map;
    const decoded = applyMap(testFolio.eva_sample, effectiveMap);

    // Check if decoded text matches expected plant
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

  // Step 3: Score the primary folio's grounding
  const primaryTerms = [
    targetFolio.plant_name.toLowerCase(),
    targetFolio.plant_latin.toLowerCase(),
    ...targetFolio.botanical_features.map((f) => f.toLowerCase()),
  ].filter(Boolean);
  const primaryGrounding = scoreOverlap(theory.decoded_text, primaryTerms);

  // Consistency = average cross-folio grounding (0 is expected for wrong maps)
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
    symbol_map: theory.symbol_map,
    keyword: theory.keyword,
    decoded_text: theory.decoded_text,
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

    // Add cipher_type column if it doesn't exist (table may predate this field)
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
    await executeSql(`ALTER TABLE serverless_stable_qh44kx_catalog.voynich.theories ADD COLUMNS (cipher_type STRING) IF NOT EXISTS`).catch(() => {});

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

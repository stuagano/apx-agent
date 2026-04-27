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
 * Expanded medieval Latin word list — ~500 words from herbal/medical manuscripts,
 * Dioscorides, Pliny, and medieval pharmacopoeias.
 */
const LATIN_DICT = new Set([
  // High-frequency function words & particles
  'et','in','de','ad','cum','per','est','non','ex','ut','ab','hoc','quod','qui','que','sed',
  'aut','vel','si','eius','sunt','fuit','esse','item','vero','nam','enim','ita','sic','nec',
  'ac','at','tam','tum','iam','pro','sub','ante','post','inter','contra','super','infra',
  'autem','idem','ipse','ille','hic','haec','ubi','unde','ergo','igitur','quidem',
  // Pronouns / demonstratives
  'is','ea','id','nos','vos','se','me','te','suo','sua','suum','meum','tuum',
  // Common verbs (various forms)
  'est','sunt','sit','fuit','habet','facit','valet','sanat','tollit','purgat','iuvat',
  'crescit','datur','bibitur','coquitur','ponitur','nascitur','invenitur','dicitur',
  'potest','debet','solet','videtur','vocatur','appellatur','fit','dat','fert',
  'curat','mundificat','confortat','colligitur','miscetur','additur','teritur',
  'lavatur','siccatur','conteritur','bibat','sumat','ponat','accipiat',
  // Botanical — plant parts
  'herba','radix','folia','folium','flos','flores','semen','semina','cortex','ramus','rami',
  'planta','fructus','succus','oleum','gummi','resina','spina','truncus','caulis',
  'bacca','nux','bulbus','tuber','fibra','stipes','petiolus','calyx','pistillum',
  // Botanical — descriptors
  'viridis','alba','nigra','rubra','flava','purpurea','amara','dulcis','acris','aspera',
  'laevis','mollis','dura','tenuis','crassa','longa','brevis','lata','rotunda','acuta',
  'pilosa','glabra','spinosa','ramosa','repens','erecta','scandens',
  // Medical — body parts
  'corpus','caput','oculus','oculi','manus','pes','pedes','stomachus','hepar','vulnus',
  'apostema','venter','pectus','dorsum','cutis','sanguis','os','ossa','nervus','vena',
  'pulmo','cor','ren','renes','vesica','matrix','uterus','dens','dentes','gula','lingua',
  // Medical — conditions & symptoms
  'morbus','dolor','febris','tussis','tumor','ulcus','abscessus','inflammatio',
  'venenum','pestis','lepra','scabies','pruritus','vertigo','paralysis','epilepsia',
  'dysenteria','hydrops','icterus','calculus','fluxus','constipatio',
  // Medical — treatments & preparations
  'remedium','potio','emplastrum','pulvis','unguentum','medicina','cura','cataplasma',
  'decoctum','infusum','electuarium','syrupus','pilula','collyrium','gargarisma',
  'dosis','pondus','mensura','cochlear','manipulus','fasciculus',
  // Properties — qualities
  'calor','humor','natura','forma','species','genus','color','odor','sapor',
  'virtus','vires','vis','potentia','qualitas','complexio','temperamentum',
  'calida','frigida','humida','sicca','temperata','acuta','obtusa',
  // Elements / substances
  'aqua','ignis','terra','aer','sal','mel','vinum','acetum','lac','butyrum',
  'cera','piper','ciminum','zingiber','crocus','cinnamomum',
  // Numbers / measures
  'unum','duo','tres','quatuor','quinque','sex','septem','octo','novem','decem',
  'libra','uncia','drachma','scrupulum','granum','partem','dimidium','tertium',
  // Plant names
  'mandragora','cannabis','papaver','rosa','lilium','salvia','urtica','absinthium',
  'artemisia','centaurea','plantago','verbena','malva','betonica','ruta','melissa',
  'eryngium','campanula','hedera','carduus','cirsium','ranunculus','aconitum',
  'mentha','anethum','foeniculum','apium','petroselinum','coriandrum','cuminum',
  'origanum','thymus','rosmarinus','lavandula','sambucus','hypericum','gentiana',
  'valeriana','symphytum','consolida','cicuta','helleborus','atropa','hyoscyamus',
  'solanum','datura','digitalis','colchicum','veratrum','opium','aloe','myrrha',
]);

/**
 * Expanded medieval Italian word list — ~350 words from herbals and recipe books.
 */
const ITALIAN_DICT = new Set([
  // Function words
  'e','il','la','le','lo','di','da','in','con','per','che','non','si','del','dei','della',
  'delle','una','uno','ha','sono','sua','suo','questo','quella','come','molto','bene','ogni',
  'ma','se','anche','poi','dove','quando','ancora','sempre','mai','piu','meno','tanto',
  'quale','chi','cosa','tutto','ogni','altro','stesso','primo','secondo','terzo',
  'al','nel','dal','sul','col','fra','tra','senza','sopra','sotto','dentro','fuori',
  // Botanical — plant parts
  'erba','radice','foglia','foglie','fiore','fiori','seme','semi','corteccia','ramo','rami',
  'albero','pianta','frutto','frutti','succo','olio','resina','spina','spine','tronco',
  'gambo','bacca','noce','bulbo','tubero','fibra','petalo','calice','bocciolo',
  // Botanical — descriptors
  'verde','bianco','nero','rosso','giallo','azzurro','amaro','dolce','acre','aspro',
  'liscio','molle','duro','sottile','grosso','lungo','corto','largo','rotondo','acuto',
  'peloso','spinoso','ramoso','strisciante','eretto',
  // Medical — body parts
  'corpo','testa','occhio','occhi','mano','mani','piede','piedi','stomaco','fegato',
  'ferita','ventre','petto','schiena','pelle','sangue','osso','ossa','nervo','vena',
  'polmone','cuore','rene','reni','vescica','dente','denti','gola','lingua',
  // Medical — conditions
  'male','dolore','febbre','tosse','tumore','ulcera','veleno','peste','rogna',
  'vertigine','paralisi','dissenteria','itterizia','flusso',
  // Medical — treatments
  'cura','rimedio','polvere','unguento','medicina','impiastro','decotto','infuso',
  'sciroppo','pillola','dose','peso','misura','cucchiaio',
  // Properties
  'virtu','caldo','freddo','umido','secco','forte','grande','piccolo','buono',
  'natura','forma','colore','odore','sapore','qualita',
  // Verbs
  'fa','vale','guarisce','purga','cresce','nasce','trova','mette','prende','beve','cuoce',
  'cura','lava','secca','macina','mescola','aggiunge','pesta','taglia','raccoglie',
  // Elements / substances
  'acqua','fuoco','terra','aria','sale','miele','vino','aceto','latte','burro',
  'cera','pepe','zafferano','cannella',
  // Plants
  'mandragora','canapa','papavero','rosa','giglio','salvia','ortica','assenzio',
  'menta','finocchio','prezzemolo','coriandolo','origano','timo','rosmarino',
  'lavanda','sambuco','valeriana','consolida','cicuta','elleboro',
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
const HILL_CLIMB_STEPS = 2000;
/** Number of initial seed maps to try before hill-climbing the best. */
const SEED_MAPS = 6;

// ---------------------------------------------------------------------------
// Consensus map — derived from analysis of top-scoring theories.
// High-stability assignments (>= 35% agreement across top 20 maps) are locked.
// Low-stability assignments are left undefined for the hill-climber to explore.
// ---------------------------------------------------------------------------

/** Locked assignments — converging across top maps (>= 35% agreement). */
const CONSENSUS_LOCKED: Record<string, Record<string, string>> = {
  latin: {
    e: 't', r: 'k', s: 'z', sh: 'a', ct: 'h', h: 'g', ok: 'q', qo: 'p',
    y: 'd', l: 'v', t: 'v', ch: 'i',  // ch→i: 7/10 top maps agree
  },
  italian: {
    e: 't', r: 'k', s: 'z', sh: 'a', ct: 'h', h: 'g', ok: 'q', qo: 'p',
    y: 'd', l: 'v', t: 'v', ch: 'i',
  },
};

/** Uncertain glyphs — the hill-climber focuses mutations here. */
const UNCERTAIN_GLYPHS = ['d', 'a', 'i', 'n', 'o', 'k', 'c', 'f', 'p', 'm'];

// ---------------------------------------------------------------------------
// Crossbreeding — recombine uncertain glyphs from top-scoring maps
// ---------------------------------------------------------------------------

/**
 * Elite pool of best maps, accumulated across rounds within one loop run.
 * Used for crossbreeding — offspring inherit uncertain glyph assignments
 * from two parents selected from this pool.
 */
const elitePool: Array<{ map: Record<string, string>; score: number; language: string }> = [];
const ELITE_POOL_SIZE = 20;
let elitePoolLoaded = false;

/** Load elite pool from the best theories persisted in Delta. */
async function loadElitePool(): Promise<void> {
  if (elitePoolLoaded) return;
  elitePoolLoaded = true;
  try {
    const rows = await executeSql(`
      SELECT symbol_map, source_language,
        ROUND(grounding_score + consistency_score, 4) AS combined
      FROM serverless_stable_qh44kx_catalog.voynich.theories
      WHERE source_language IN ('latin', 'italian')
        AND grounding_score + consistency_score > 0.2
      ORDER BY grounding_score + consistency_score DESC
      LIMIT 20
    `);
    for (const row of rows) {
      try {
        const map = JSON.parse(row.symbol_map);
        const score = parseFloat(row.combined);
        elitePool.push({ map, score, language: row.source_language });
      } catch { /* skip unparseable */ }
    }
    if (elitePool.length > 0) {
      console.log(`[theory-loop] Loaded ${elitePool.length} elite maps from Delta (best=${elitePool[0].score.toFixed(3)})`);
    }
  } catch (err) {
    console.warn('[theory-loop] Failed to load elite pool from Delta:', err);
  }
}

function addToElitePool(map: Record<string, string>, score: number, language: string): void {
  elitePool.push({ map, score, language });
  elitePool.sort((a, b) => b.score - a.score);
  if (elitePool.length > ELITE_POOL_SIZE) elitePool.length = ELITE_POOL_SIZE;
}

/**
 * Crossbreed two parent maps — each uncertain glyph is inherited from
 * one parent (50/50 coin flip per glyph). Locked glyphs are preserved.
 */
function crossbreed(
  parentA: Record<string, string>,
  parentB: Record<string, string>,
  language: string,
): Record<string, string> {
  const locked = CONSENSUS_LOCKED[language] ?? CONSENSUS_LOCKED.latin;
  const child = { ...parentA };

  for (const glyph of UNCERTAIN_GLYPHS) {
    if (glyph in parentB && Math.random() < 0.5) {
      child[glyph] = parentB[glyph];
    }
  }

  // Ensure locked assignments are preserved
  Object.assign(child, locked);
  return child;
}

/**
 * Build a consensus-seeded map: lock high-stability assignments,
 * fill uncertain glyphs from frequency matching with perturbation.
 */
function generateConsensusMap(
  evaFreqs: Array<[string, number]>,
  language: string,
  perturbationRate: number = 0,
): Record<string, string> {
  const locked = CONSENSUS_LOCKED[language] ?? CONSENSUS_LOCKED.latin;
  const freqMap = generateFrequencyMap(evaFreqs, language, perturbationRate);

  // Start with frequency map, then overwrite with locked consensus
  const map = { ...freqMap, ...locked };
  return map;
}

/**
 * Mutate a symbol map — but only swap UNCERTAIN glyphs.
 * Locked consensus assignments are preserved.
 */
function mutateMapFocused(
  map: Record<string, string>,
  language: string,
): Record<string, string> {
  const result = { ...map };
  const locked = CONSENSUS_LOCKED[language] ?? CONSENSUS_LOCKED.latin;
  const lockedGlyphs = new Set(Object.keys(locked));

  // Only mutate unlocked glyphs
  const mutableGlyphs = Object.keys(result).filter((g) => !lockedGlyphs.has(g));
  if (mutableGlyphs.length < 2) return result;

  const numSwaps = Math.random() < 0.7 ? 1 : 2;
  for (let s = 0; s < numSwaps; s++) {
    const i = Math.floor(Math.random() * mutableGlyphs.length);
    const j = Math.floor(Math.random() * mutableGlyphs.length);
    if (i !== j) {
      const gi = mutableGlyphs[i];
      const gj = mutableGlyphs[j];
      const tmp = result[gi];
      result[gi] = result[gj];
      result[gj] = tmp;
    }
  }
  return result;
}

/**
 * Unfocused mutation — swaps any glyphs, including locked ones.
 * Used for a fraction of mutations to escape local optima.
 */
function mutateMapWild(map: Record<string, string>): Record<string, string> {
  const result = { ...map };
  const glyphs = Object.keys(result);
  if (glyphs.length < 2) return result;

  const numSwaps = Math.random() < 0.5 ? 1 : 2;
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

  // Load elite pool from Delta on first call (persists across deploys)
  await loadElitePool();

  // Step 1: Count EVA glyph frequencies across ALL folios
  const allEvaTexts = allFolios.map((f) => f.eva_sample).filter(Boolean);
  const evaFreqs = countEvaFrequencies(allEvaTexts);

  // Step 2: Generate seed maps — consensus, crossbred offspring, and perturbed variants
  const seeds: Array<{ map: Record<string, string>; decoded: string; score: number }> = [];

  // Seed 0: pure consensus map
  const consensusMap = generateConsensusMap(evaFreqs, sourceLanguage, 0);
  const consensusDecoded = applyMap(evaText, consensusMap);
  seeds.push({ map: consensusMap, decoded: consensusDecoded, score: hillClimbScore(consensusDecoded, sourceLanguage) });

  // Seeds from crossbreeding elite pool (if we have enough elites for this language)
  const elitesForLang = elitePool.filter((e) => e.language === sourceLanguage);
  if (elitesForLang.length >= 2) {
    for (let s = 0; s < 2; s++) {
      // Tournament selection: pick 2 random elites, breed the better ones
      const idxA = Math.floor(Math.random() * elitesForLang.length);
      let idxB = Math.floor(Math.random() * elitesForLang.length);
      while (idxB === idxA && elitesForLang.length > 1) idxB = Math.floor(Math.random() * elitesForLang.length);
      const child = crossbreed(elitesForLang[idxA].map, elitesForLang[idxB].map, sourceLanguage);
      const decoded = applyMap(evaText, child);
      seeds.push({ map: child, decoded, score: hillClimbScore(decoded, sourceLanguage) });
    }
  }

  // Fill remaining seeds with perturbed consensus maps
  while (seeds.length < SEED_MAPS) {
    const perturbation = seeds.length / (SEED_MAPS - 1) * 0.5;
    const map = generateConsensusMap(evaFreqs, sourceLanguage, perturbation);
    const decoded = applyMap(evaText, map);
    seeds.push({ map, decoded, score: hillClimbScore(decoded, sourceLanguage) });
  }

  seeds.sort((a, b) => b.score - a.score);
  let bestMap = seeds[0].map;
  let bestDecoded = seeds[0].decoded;
  let bestScore = seeds[0].score;

  console.log(`[theory-loop]   seeds=${SEED_MAPS} best_seed=${bestScore.toFixed(3)} dict=${dictionaryScore(bestDecoded, sourceLanguage).toFixed(3)} starting hill-climb...`);

  // Step 3: Hill-climb — mostly focused mutations (uncertain glyphs only),
  // with 10% wild mutations to escape local optima.
  let bestHillScore = hillClimbScore(bestDecoded, sourceLanguage);
  let improvements = 0;

  for (let step = 0; step < HILL_CLIMB_STEPS; step++) {
    // 90% focused (only uncertain glyphs), 10% wild (any glyph)
    const candidate = Math.random() < 0.9
      ? mutateMapFocused(bestMap, sourceLanguage)
      : mutateMapWild(bestMap);
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

  // Add to elite pool for crossbreeding in future rounds
  addToElitePool(bestMap, bestHillScore, sourceLanguage);

  console.log(`[theory-loop]   hill-climb: ${improvements} improvements in ${HILL_CLIMB_STEPS} steps, dict=${dictScore.toFixed(3)} bigram=${bigramFinal.toFixed(3)} combined=${bestHillScore.toFixed(3)} elites=${elitePool.length}`);

  // Step 4: Test cross-folio consistency
  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 10);

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

    const matchScore = broadGrounding(decoded, sourceLanguage, expectedTerms);

    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: matchScore,
    });
  }

  // Step 5: Broad grounding — plant terms + dictionary + bigrams
  const primaryTerms = [
    targetFolio.plant_name.toLowerCase(),
    targetFolio.plant_latin.toLowerCase(),
    ...targetFolio.botanical_features.map((f) => f.toLowerCase()),
  ].filter(Boolean);
  const primaryGrounding = broadGrounding(bestDecoded, sourceLanguage, primaryTerms);

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

/**
 * Score how well decoded text matches expected plant terms.
 * Returns 0-1 based on exact matches, substring matches, and prefix matches.
 */
function scoreTermOverlap(text: string, terms: string[]): number {
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
  return Math.min(1.0, score / Math.max(terms.length, 1));
}

/**
 * Broad grounding score — combines three signals:
 *
 *   1. Plant-term overlap (30%) — does decoded text contain the expected
 *      plant name, Latin binomial, or botanical features?
 *   2. Dictionary coverage (40%) — what fraction of decoded words are real
 *      words in the target language? (strongest signal for language quality)
 *   3. Bigram plausibility (30%) — does the decoded text have character-pair
 *      statistics consistent with the target language?
 *
 * This replaces the old scoreOverlap which ONLY checked plant terms —
 * meaning maps that produced real language but not the exact plant name
 * scored zero. Now any real-language output gets partial credit.
 */
function broadGrounding(text: string, language: string, plantTerms: string[]): number {
  const termScore = scoreTermOverlap(text, plantTerms);
  const dictScore_ = dictionaryScore(text, language);
  const bigramScore = quickBigramScore(text, language);

  return termScore * 0.3 + dictScore_ * 0.4 + bigramScore * 0.3;
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

export async function runTheoryLoop(maxRounds: number = 200): Promise<Theory[]> {
  const folios = await loadFolios();
  const highConfidence = folios.filter((f) => f.confidence >= 0.5);
  // Focus on Latin (strongest signal) and Italian (runner-up). Greek has no dictionary.
  const languages = ['latin', 'latin', 'latin', 'italian', 'italian'];

  console.log(`[theory-loop] Starting with ${highConfidence.length} high-confidence folios, ${maxRounds} rounds`);

  const theories: Theory[] = [];

  for (let round = 0; round < maxRounds; round++) {
    // Pick a random folio and language (weighted toward Latin)
    const folio = highConfidence[Math.floor(Math.random() * highConfidence.length)];
    const lang = languages[Math.floor(Math.random() * languages.length)];

    // Mostly substitution — it's outperforming polyalphabetic
    const cipherType = round % 5 === 4 ? 'polyalphabetic' : 'substitution';

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

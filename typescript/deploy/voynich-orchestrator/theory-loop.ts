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

import {
  resolveToken,
  resolveHost,
  createTrace,
  addSpan,
  endTrace,
  runWithContext,
  truncate,
} from './appkit-agent/index.mjs';

/**
 * Local equivalent of the framework's withAutonomousTrace — inlined here
 * because the vendored appkit-agent bundle in this deploy predates that
 * helper. After the next `scripts/build-deploy.sh` run, this can be
 * replaced with a direct `withAutonomousTrace` import.
 */
async function withRoundTrace<T>(
  agentName: string,
  label: string,
  fn: () => Promise<T>,
): Promise<T> {
  const trace = createTrace(agentName);
  addSpan(trace, { type: 'request', name: label, input: label });
  try {
    const result = await runWithContext({ oboHeaders: {}, trace }, fn);
    addSpan(trace, { type: 'response', name: 'response', output: truncate(result) });
    endTrace(trace);
    return result;
  } catch (err) {
    addSpan(trace, {
      type: 'error',
      name: 'error',
      metadata: { error: (err as Error).message ?? String(err) },
    });
    endTrace(trace, 'error');
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FolioInfo {
  folio_id: string;
  plant_name: string;
  plant_latin: string;
  confidence: number;
  botanical_features: string[];
  /** Image-derived term lists per language. Populated from
   * folio_vision_analysis.expected_terms (LLM-generated from visual_description).
   * Each list has ~50 medieval botanical/anatomical/descriptive terms relevant
   * to the actual plant depicted on the folio. */
  expected_terms: Record<string, string[]>;
  eva_sample: string;
}

export type CipherType = 'substitution' | 'polyalphabetic' | 'substitution-strip' | 'verbose' | 'positional' | 'homophonic';

export interface Theory {
  id: string;
  proposed_at: string;
  source_language: string;
  cipher_type: CipherType;
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

export type SeedMode = 'elite' | 'cold';

export interface Strategy {
  language: 'latin' | 'italian';
  cipherType: CipherType;
  seedMode: SeedMode;
}

const STRATEGIES: Strategy[] = [
  // Substitution + substitution-strip run first — they populate the elite
  // pool. Verbose seeds from those elites, so it must run after.
  { language: 'latin',   cipherType: 'substitution',         seedMode: 'elite' },
  { language: 'latin',   cipherType: 'substitution',         seedMode: 'cold'  },
  { language: 'italian', cipherType: 'substitution',         seedMode: 'elite' },
  { language: 'italian', cipherType: 'substitution',         seedMode: 'cold'  },
  { language: 'latin',   cipherType: 'substitution-strip',   seedMode: 'elite' },
  { language: 'latin',   cipherType: 'substitution-strip',   seedMode: 'cold'  },
  { language: 'italian', cipherType: 'substitution-strip',   seedMode: 'elite' },
  { language: 'italian', cipherType: 'substitution-strip',   seedMode: 'cold'  },
  // Verbose runs after substitution — elite pool is populated under the
  // corrected scorer, so elite seeding has real signal to work with.
  { language: 'latin',   cipherType: 'verbose',              seedMode: 'elite' },
  { language: 'latin',   cipherType: 'verbose',              seedMode: 'cold'  },
  { language: 'italian', cipherType: 'verbose',              seedMode: 'elite' },
  { language: 'italian', cipherType: 'verbose',              seedMode: 'cold'  },
  // Positional cipher — addresses Voynich's most distinctive structural
  // anomaly (glyphs cluster strongly by word position). Elite mode seeds
  // all three position-buckets with a substitution elite, then SA breaks
  // the symmetry to find genuinely position-specific mappings.
  { language: 'latin',   cipherType: 'positional',           seedMode: 'elite' },
  { language: 'latin',   cipherType: 'positional',           seedMode: 'cold'  },
  { language: 'italian', cipherType: 'positional',           seedMode: 'elite' },
  { language: 'italian', cipherType: 'positional',           seedMode: 'cold'  },
  // Homophonic (Naibbe-style) — many EVA tokens collapse to one plaintext
  // letter. Inverse cardinality of verbose. Most likely to break the
  // ~0.37 plateau under the 2025 Cryptologia hypothesis.
  { language: 'latin',   cipherType: 'homophonic',           seedMode: 'elite' },
  { language: 'latin',   cipherType: 'homophonic',           seedMode: 'cold'  },
  { language: 'italian', cipherType: 'homophonic',           seedMode: 'elite' },
  { language: 'italian', cipherType: 'homophonic',           seedMode: 'cold'  },
  // Polyalphabetic (keyword-shifted substitution)
  { language: 'latin',   cipherType: 'polyalphabetic',       seedMode: 'elite' },
  { language: 'italian', cipherType: 'polyalphabetic',       seedMode: 'cold'  },
];

const ROUNDS_PER_BURST = 20;
const PROGRESS_THRESHOLD = 0.02;

interface StrategyStat {
  attempts: number;
  best_score: number;
  last_attempted_at: string;
  exhausted: boolean;
}

const strategyStats = new Map<string, StrategyStat>();
let strategyStatsLoaded = false;

function strategyKey(s: Strategy): string {
  return `${s.language}|${s.cipherType}|${s.seedMode}`;
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

// ---------------------------------------------------------------------------
// Character n-gram language model
// ---------------------------------------------------------------------------
//
// Reference corpora in medieval herbal Latin and Italian (volgare) style.
// Used to train a character trigram model so the scorer can distinguish
// real-language-shaped output from text that merely matches glyph frequencies.
// Replaces the old hand-curated bigram list which was too coarse to break
// past the 0.37 plateau seen in earlier runs.

const LATIN_CORPUS = `
herba haec nascitur in locis humidis et umbrosis radix eius est longa et alba
recipe radicem mandragorae et folia eius pista et misce cum aqua frigida
folium est latum et pilosum flores sunt purpurei semen est nigrum et acutum
herba haec sanat morbos stomachi et iuvat digestionem cum bibitur in vino
calida et sicca est in primo gradu valet contra dolorem capitis et oculorum
recipe herbam istam et tere cum oleo rosaceo et impone super vulnus et sanat
cocta in aqua cum melle et oleo facit emplastrum bonum contra apostemata
herba sancti iohannis nascitur per agros et habet flores luteos et parvos
folia papaveris cum semine eius valent contra insomniam et tussim antiquam
infusio florum sambuci sumitur contra febrem et purgat humores grossos
oleum rosarum frigidum est et confortat caput et stomachum et oculos
mel et vinum coctum cum cinnamomo iuvat virtutes corporis et sanat tussim
radix gentianae amara est et purgat venenum et sanat morsus serpentis
folia salviae sicca cum vino calido valent contra dolorem dentium et gingivarum
herba urtica est calida et purgat sanguinem et iuvat reumata articulorum
flores camomillae cum oleo rosaceo applicantur super ventrem et tollit dolorem
semina foeniculi cum aqua calida bibita iuvat oculos et purgat ventrem
radix aristolochiae cum melle et vino tollit dolorem matricis et purgat humores
unguentum compositum ex foliis rutae et cera valet contra paralysim membrorum
herba betonica est virtutum multarum et sanat capitis dolorem antiquum
recipe folia menthae et tere cum aceto et impone super tempora capitis
malva calida est et humida temperat humores corporis et iuvat ventrem
plantago herba est frigida et sicca et confortat virtutes stomachi et hepatis
hedera nascitur in locis silvestribus et habet folia viridia et obscura
verbena herba sacra est et habet flores parvos et purpureos contra venenum
absinthium amarum est et tollit vermes et purgat hepar et lien et stomachum
artemisia mater herbarum dicitur et iuvat morbos matricis et menstrua provocat
melissa habet odorem suavem et confortat cor et oculos et virtutes vitales
recipe semen anethi et tere subtiliter et misce cum oleo et vino calido
folium hederae cum aceto bibitum tollit dolorem capitis et tussim purgat
`;

const ITALIAN_CORPUS = `
piglia la radice di mandragora et le sue foglie et pesta con acqua fredda
questa erba nasce nei luoghi umidi et ombrosi la sua radice è bianca et lunga
la foglia è larga et pelosa i fiori sono porpora il seme è nero et acuto
questa erba sana le malattie dello stomaco et giova la digestione con vino
è calda et secca nel primo grado vale contro il dolore della testa et degli occhi
piglia questa erba et pestala con olio rosato et mettila sopra la ferita et sana
cotta in acqua con miele et olio fa un buon impiastro contro le apostemi
erba di san giovanni nasce per i campi et ha fiori gialli et piccoli
foglie di papavero con il suo seme valgono contro insonnia et tosse antica
infuso di fiori di sambuco si prende contro la febbre et purga gli umori
olio di rose è freddo et conforta la testa et lo stomaco et gli occhi
miele et vino cotto con la cannella giova le virtù del corpo et sana la tosse
radice di genziana è amara et purga il veleno et sana morsi di serpe
foglie di salvia secca con vino caldo valgono contro il dolore dei denti
ortica è calda et purga il sangue et giova ai reumi delle giunture
fiori di camomilla con olio rosato si pongono sopra il ventre et tolgono dolore
semi di finocchio con acqua calda bevuti giovano agli occhi et purgano il ventre
radice di aristolochia con miele et vino toglie il dolore della matrice et purga
unguento composto di foglie di ruta et cera vale contro la paralisia delle membra
erba betonica è di molte virtù et sana il dolore della testa antico
piglia foglie di menta et pestale con aceto et mettile sopra le tempie
malva è calda et umida et tempera gli umori del corpo et giova il ventre
piantaggine è erba fredda et secca et conforta le virtù dello stomaco
edera nasce nei luoghi silvestri et ha foglie verdi et oscure
verbena è erba sacra et ha fiori piccoli et porpora contro il veleno
assenzio è amaro et toglie i vermi et purga il fegato et la milza
artemisia è detta madre delle erbe et giova le malattie della matrice
melissa ha odore soave et conforta il cuore et gli occhi et le virtù vitali
piglia il seme di aneto et pestalo sottilmente et mescola con olio et vino
foglia di edera con aceto bevuta toglie il dolore della testa et purga la tosse
`;

interface TrigramModel {
  ngrams: Map<string, number>;
  total: number;
  vocabSize: number;
}

function buildTrigramModel(corpus: string): TrigramModel {
  const text = ` ${corpus.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  const ngrams = new Map<string, number>();
  let total = 0;
  for (let i = 0; i < text.length - 2; i++) {
    const tg = text.slice(i, i + 3);
    ngrams.set(tg, (ngrams.get(tg) ?? 0) + 1);
    total++;
  }
  return { ngrams, total, vocabSize: ngrams.size };
}

const NGRAM_MODELS: Record<string, TrigramModel> = {
  latin: buildTrigramModel(LATIN_CORPUS),
  italian: buildTrigramModel(ITALIAN_CORPUS),
};

console.log(
  `[theory-loop] n-gram models: latin=${NGRAM_MODELS.latin.total} trigrams (${NGRAM_MODELS.latin.vocabSize} unique), ` +
  `italian=${NGRAM_MODELS.italian.total} trigrams (${NGRAM_MODELS.italian.vocabSize} unique)`,
);

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

/**
 * Score decoded text under the language's character trigram model.
 * Uses Laplace-smoothed log-probability, then linearly maps avg-log-prob
 * to [0, 1] using empirically-tuned reference points for "real text" vs
 * "random gibberish". Returns ~0.7+ for genuine Latin/Italian and ~0.1
 * for scrambled output, replacing the old bigram-overlap heuristic.
 */
function langModelScore(text: string, language: string): number {
  const model = NGRAM_MODELS[language] ?? NGRAM_MODELS.latin;
  const clean = ` ${text.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  if (clean.length < 5) return 0;

  const N = model.total;
  const V = model.vocabSize;
  let logProbSum = 0;
  let count = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    const c = model.ngrams.get(tg) ?? 0;
    // Laplace smoothing: P(tg) = (count + 1) / (N + V)
    logProbSum += Math.log((c + 1) / (N + V));
    count++;
  }
  if (count === 0) return 0;
  const avgLogProb = logProbSum / count;
  // Calibration tuned empirically (see local sanity check):
  //   real Latin ~ -6.3, real-out-of-domain ~ -6.8, decoded gibberish ~ -7.5,
  //   random scramble ~ -8.0. These references give real text ~0.7-0.85 and
  //   gibberish ~0.20-0.35, a clear discriminative gradient.
  const REF_BAD = -8.5;
  const REF_GOOD = -5.5;
  return Math.max(0, Math.min(1, (avgLogProb - REF_BAD) / (REF_GOOD - REF_BAD)));
}

/**
 * Dictionary score — fraction of decoded words that appear in the word list.
 * Much stronger signal than bigrams: rewards actual word formation.
 *
 * Per-type cap: a single dictionary word contributes at most 2 hits, even if
 * it appears 12 times. Without this cap, the hill-climber finds maps that
 * saturate the output with one content word (e.g. `vino` ×12) and bank cheap
 * dict score. The cap forces vocabulary diversity for high scores.
 */
const PER_TYPE_HIT_CAP = 2;

function dictionaryScore(text: string, language: string): number {
  const dict = DICT_BY_LANG[language];
  if (!dict) return 0;

  const words = text.toLowerCase().replace(/[^a-z\s]/g, '').split(/\s+/).filter((w) => w.length >= 2);
  if (words.length === 0) return 0;

  const typeCounts = new Map<string, number>();
  let partialHits = 0;
  for (const word of words) {
    if (dict.has(word)) {
      typeCounts.set(word, (typeCounts.get(word) ?? 0) + 1);
    } else {
      // Partial credit: check if any dict word is a prefix/suffix of the decoded word
      // This catches inflected forms (e.g. "herbam" matches "herba")
      for (const entry of dict) {
        if (entry.length >= 4 && (word.startsWith(entry) || word.endsWith(entry.slice(-4)))) {
          partialHits += 0.3;
          break;
        }
      }
    }
  }

  let hits = partialHits;
  for (const count of typeCounts.values()) {
    hits += Math.min(count, PER_TYPE_HIT_CAP);
  }
  return hits / words.length;
}

/**
 * High-frequency function words per language. The hill-climber loves to map a
 * few common EVA glyphs onto these (because they give cheap dictionary hits),
 * producing decoded text saturated with `est`, `ita`, `ad`, `post`… while the
 * rest stays gibberish. We use this set to penalise such "function-word trap"
 * solutions in `dictionaryQuality`.
 */
const FUNCTION_WORDS: Record<string, Set<string>> = {
  latin: new Set([
    'est','et','ad','ut','in','de','ex','non','qui','quod','cum','sed','ita','sic',
    'sunt','enim','aut','ab','ac','at','pro','per','nec','vel','si','nam','quam',
    'quae','post','ante','sub','super','iam','tamen','etiam','autem','itaque',
    'vero','dum','tum','tunc','hic','haec','hoc','eo','eam','eos','suum','suam',
    'a','o','e','is','ea','id','me','te','se','nos','vos','iis','eis',
  ]),
  italian: new Set([
    'e','il','la','di','che','a','in','un','non','per','con','del','come','ma','se',
    'lo','gli','le','da','al','dei','delle','degli','nei','nelle','sul','sulla','sui',
    'è','ha','ho','hai','sia','fu','era','ed','o','ne','ci','vi','si','mi','ti','tu',
    'io','lui','lei','noi','voi','loro','suo','sua','suoi','sue','quel','quella',
    'questo','questa','questi','queste','i','una','uno','agli','dal','dalla','nel',
  ]),
};

/**
 * Quality multiplier (0..1) applied to dictionary score. Penalises decoded
 * text dominated by function words and rewards content-word diversity.
 *
 * - P1 (saturation): if >50% of words are function-word hits, decay linearly
 *   to 0 at 100% (the trap).
 * - P2 (diversity): unique dictionary-matched word types ÷ total words. Below
 *   0.35 is thin vocabulary; 0.0 returns 0.
 */
function dictionaryQuality(text: string, language: string): number {
  const dict = DICT_BY_LANG[language];
  const funcSet = FUNCTION_WORDS[language];
  if (!dict || !funcSet) return 1;

  const words = text.toLowerCase().replace(/[^a-z\s]/g, '').split(/\s+/).filter((w) => w.length >= 2);
  if (words.length < 4) return 1;

  let funcHits = 0;
  const uniqueDictTypes = new Set<string>();
  for (const w of words) {
    if (dict.has(w)) {
      uniqueDictTypes.add(w);
      if (funcSet.has(w)) funcHits++;
    }
  }

  const funcRatio = funcHits / words.length;
  const p1 = Math.max(0, Math.min(1, 1 - Math.max(0, funcRatio - 0.5) * 2.0));

  const diversity = uniqueDictTypes.size / words.length;
  const p2 = Math.max(0, Math.min(1, diversity / 0.35));

  return p1 * p2;
}

/**
 * Combined hill-climbing score: quality-adjusted dictionary match (strong
 * signal) + bigram similarity (weak but continuous signal). The quality
 * multiplier defends against the function-word trap — see `dictionaryQuality`.
 */
function hillClimbScore(text: string, language: string): number {
  const dict = dictionaryScore(text, language);
  const quality = dictionaryQuality(text, language);
  const lm = langModelScore(text, language);
  return (dict * quality) * 0.75 + lm * 0.25;
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
      SELECT folio_id, subject_candidates, botanical_features, expected_terms
      FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
      WHERE section = 'herbal'
      ORDER BY folio_id
    `),
    loadEvaCorpus(),
  ]);

  folioCache = rows.map((r) => {
    const candidates = JSON.parse(r.subject_candidates || '[]');
    const top = candidates[0] || {};
    let expected: Record<string, string[]> = {};
    try {
      const parsed = JSON.parse(r.expected_terms || '{}') as Record<string, string[]>;
      for (const [lang, terms] of Object.entries(parsed)) {
        if (Array.isArray(terms)) expected[lang] = terms.map((t) => String(t).toLowerCase());
      }
    } catch { /* leave empty */ }
    return {
      folio_id: r.folio_id,
      plant_name: top.name || 'unknown',
      plant_latin: top.latin || '',
      confidence: top.confidence || 0,
      botanical_features: JSON.parse(r.botanical_features || '[]'),
      expected_terms: expected,
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
        AND cipher_type IN ('substitution', 'substitution-strip', 'verbose')
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
  cipherType: CipherType = 'substitution',
  seedMode: SeedMode = 'elite',
): Promise<Theory> {
  const theoryId = Math.random().toString(36).slice(2, 10);
  // For substitution-strip, preprocess all EVA text before any further work.
  const stripPreprocess = cipherType === 'substitution-strip';
  const evaText = stripPreprocess ? stripNulls(targetFolio.eva_sample) : targetFolio.eva_sample;

  // Verbose cipher uses simulated annealing over a many-to-many map. Branch
  // off entirely — the seed/hill-climb/elite-pool flow below targets 1:1 maps.
  if (cipherType === 'verbose') {
    return await proposeVerboseTheory(theoryId, targetFolio, allFolios, sourceLanguage, seedMode);
  }
  // Positional cipher uses a position-conditional substitution (3 sub-maps
  // for word-initial / middle / final). Also branches off — the maps are
  // stored with i:/m:/f: key prefixes, incompatible with applyMap.
  if (cipherType === 'positional') {
    return await proposePositionalTheory(theoryId, targetFolio, allFolios, sourceLanguage, seedMode);
  }
  // Homophonic cipher (Naibbe-style) — many EVA tokens collapse to one
  // plaintext letter. Inverse cardinality of verbose. Branches off because
  // the seed map structure is curated (fixed token list) rather than
  // frequency-derived.
  if (cipherType === 'homophonic') {
    return await proposeHomophonicTheory(theoryId, targetFolio, allFolios, sourceLanguage, seedMode);
  }

  // Load elite pool from Delta on first call (persists across deploys)
  await loadElitePool();

  // Step 1: Count EVA glyph frequencies across ALL folios. For substitution-strip,
  // run the same null preprocessor over the corpus so the seed alphabet matches.
  const allEvaTexts = allFolios.map((f) => stripPreprocess ? stripNulls(f.eva_sample) : f.eva_sample).filter(Boolean);
  const evaFreqs = countEvaFrequencies(allEvaTexts);

  // Step 2: Generate diverse seed maps — exploitation AND exploration.
  // Mix of: consensus/elite (exploit), radical new architectures (explore).
  const seeds: Array<{ map: Record<string, string>; decoded: string; score: number; origin: string }> = [];

  // --- EXPLOITATION SEEDS (build on what works) ---
  // Cold mode skips these entirely — no consensus anchoring, no elite influence.
  if (seedMode === 'elite') {
    // Seed: pure consensus map
    const consensusMap = generateConsensusMap(evaFreqs, sourceLanguage, 0);
    const consensusDecoded = applyMap(evaText, consensusMap);
    seeds.push({ map: consensusMap, decoded: consensusDecoded, score: hillClimbScore(consensusDecoded, sourceLanguage), origin: 'consensus' });

    // Seeds from crossbreeding elite pool
    const elitesForLang = elitePool.filter((e) => e.language === sourceLanguage);
    if (elitesForLang.length >= 2) {
      for (let s = 0; s < 2; s++) {
        const idxA = Math.floor(Math.random() * elitesForLang.length);
        let idxB = Math.floor(Math.random() * elitesForLang.length);
        while (idxB === idxA && elitesForLang.length > 1) idxB = Math.floor(Math.random() * elitesForLang.length);
        const child = crossbreed(elitesForLang[idxA].map, elitesForLang[idxB].map, sourceLanguage);
        const decoded = applyMap(evaText, child);
        seeds.push({ map: child, decoded, score: hillClimbScore(decoded, sourceLanguage), origin: 'crossbred' });
      }
    }
  }

  // --- EXPLORATION SEEDS (radically different starting points) ---

  // Seed: reverse frequency — map most common EVA to LEAST common target letters
  const reverseLetters = [...(LANG_FREQ[sourceLanguage] ?? LANG_FREQ.latin)].reverse();
  const reverseMap: Record<string, string> = {};
  for (let i = 0; i < evaFreqs.length; i++) {
    reverseMap[evaFreqs[i][0]] = reverseLetters[Math.min(i, reverseLetters.length - 1)];
  }
  const reverseDecoded = applyMap(evaText, reverseMap);
  seeds.push({ map: reverseMap, decoded: reverseDecoded, score: hillClimbScore(reverseDecoded, sourceLanguage), origin: 'reverse-freq' });

  // Seed: vowel hypothesis — map high-freq EVA glyphs to vowels, rest to consonants
  const vowels = sourceLanguage === 'italian' ? ['e','a','i','o','u'] : ['e','i','a','u','o'];
  const consonants = sourceLanguage === 'italian'
    ? ['n','l','r','t','s','c','d','p','m','v','g','b','f','h','z','q']
    : ['t','s','n','r','l','c','m','d','p','b','q','g','v','f','h','x'];
  const vowelMap: Record<string, string> = {};
  for (let i = 0; i < evaFreqs.length; i++) {
    if (i < vowels.length) {
      vowelMap[evaFreqs[i][0]] = vowels[i];
    } else {
      vowelMap[evaFreqs[i][0]] = consonants[Math.min(i - vowels.length, consonants.length - 1)];
    }
  }
  const vowelDecoded = applyMap(evaText, vowelMap);
  seeds.push({ map: vowelMap, decoded: vowelDecoded, score: hillClimbScore(vowelDecoded, sourceLanguage), origin: 'vowel-hyp' });

  // Seed: fully random shuffle — complete restart, no assumptions
  const randomMap: Record<string, string> = {};
  const allLetters = [...(LANG_FREQ[sourceLanguage] ?? LANG_FREQ.latin)];
  const shuffled = [...allLetters].sort(() => Math.random() - 0.5);
  for (let i = 0; i < evaFreqs.length; i++) {
    randomMap[evaFreqs[i][0]] = shuffled[Math.min(i, shuffled.length - 1)];
  }
  const randomDecoded = applyMap(evaText, randomMap);
  seeds.push({ map: randomMap, decoded: randomDecoded, score: hillClimbScore(randomDecoded, sourceLanguage), origin: 'random' });

  // Seed: historical Newbold-style — EVA 'o'→'a', 'a'→'e', 'i'→'i', 'd'→'d', etc.
  // (phonetic similarity hypothesis — EVA chars look like the Latin letters they encode)
  const phonMap: Record<string, string> = {
    o: 'a', a: 'e', i: 'i', d: 'd', n: 'n', e: 'e', y: 'y', r: 'r', s: 's',
    k: 'c', l: 'l', t: 't', h: 'h', c: 'c', f: 'f', p: 'p', m: 'm', q: 'q',
    ch: 'k', sh: 'x', th: 'f', ct: 'st', ok: 'ac', qo: 'qu', ol: 'al', or: 'ar',
    ai: 'ae', ee: 'ii', dy: 'dy', ey: 'ey', ar: 'ar', ck: 'ck',
  };
  const phonDecoded = applyMap(evaText, phonMap);
  seeds.push({ map: phonMap, decoded: phonDecoded, score: hillClimbScore(phonDecoded, sourceLanguage), origin: 'phonetic' });

  // Fill remaining with perturbed consensus
  while (seeds.length < SEED_MAPS) {
    const perturbation = 0.3 + Math.random() * 0.4;  // 0.3-0.7 range — more aggressive
    const map = generateConsensusMap(evaFreqs, sourceLanguage, perturbation);
    const decoded = applyMap(evaText, map);
    seeds.push({ map, decoded, score: hillClimbScore(decoded, sourceLanguage), origin: 'perturbed' });
  }

  seeds.sort((a, b) => b.score - a.score);
  let bestMap = seeds[0].map;
  let bestDecoded = seeds[0].decoded;
  let bestScore = seeds[0].score;
  const seedOrigin = seeds[0].origin;

  console.log(`[theory-loop]   seeds=${seeds.length} best_seed=${bestScore.toFixed(3)} origin=${seedOrigin} dict=${dictionaryScore(bestDecoded, sourceLanguage).toFixed(3)} starting hill-climb...`);

  // Step 3: Hill-climb — balance focused vs wild mutations based on seed origin and mode.
  // Cold mode escapes the consensus basin: full wild mutation.
  // Exploration seeds get more wild mutations; exploitation seeds stay focused.
  const isExplorationSeed = ['reverse-freq', 'vowel-hyp', 'random', 'phonetic'].includes(seedOrigin);
  const wildRate = seedMode === 'cold' ? 0.6
    : isExplorationSeed ? 0.4
    : 0.15;

  let bestHillScore = hillClimbScore(bestDecoded, sourceLanguage);
  let improvements = 0;

  for (let step = 0; step < HILL_CLIMB_STEPS; step++) {
    const candidate = Math.random() < (1 - wildRate)
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
  const bigramFinal = langModelScore(bestDecoded, sourceLanguage);
  bestScore = bestHillScore;

  let keyword: string | undefined;
  if (cipherType === 'polyalphabetic') {
    const keywords = ['HERBA', 'FLORA', 'RADIX', 'FOLIA', 'SEMEN', 'VIRTUS', 'MEDICA', 'PLANTA'];
    keyword = keywords[Math.floor(Math.random() * keywords.length)];
  }

  // Add to elite pool for crossbreeding in future rounds
  addToElitePool(bestMap, bestHillScore, sourceLanguage);

  console.log(`[theory-loop]   hill-climb: ${improvements} improvements in ${HILL_CLIMB_STEPS} steps, dict=${dictScore.toFixed(3)} lm=${bigramFinal.toFixed(3)} combined=${bestHillScore.toFixed(3)} elites=${elitePool.length}`);

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
    const testEva = stripPreprocess ? stripNulls(testFolio.eva_sample) : testFolio.eva_sample;
    const decoded = applyMap(testEva, effectiveMap);

    const expectedTerms = expectedTermsFor(testFolio, sourceLanguage);

    const matchScore = broadGrounding(decoded, sourceLanguage, expectedTerms);

    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: matchScore,
    });
  }

  // Step 5: Broad grounding — plant terms + dictionary + bigrams
  const primaryTerms = expectedTermsFor(targetFolio, sourceLanguage);
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

const VERBOSE_SA_STEPS = 8000;

/**
 * Verbose-cipher theory: many-to-many EVA→Latin map searched via simulated
 * annealing. In `elite` seed mode, seeds the singles from a top-3 substitution
 * elite for the language — this gives SA a hill-climbed starting point so it
 * can spend its budget refining the multi-char extensions (bigrams, scribal
 * abbreviations) rather than rediscovering the basic function-word mapping.
 */
async function proposeVerboseTheory(
  theoryId: string,
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  seedMode: SeedMode = 'cold',
): Promise<Theory> {
  const evaText = targetFolio.eva_sample;
  await loadElitePool();

  let seed = generateVerboseSeed(sourceLanguage);
  let seedSource = 'fresh';
  if (seedMode === 'elite') {
    const elites = elitePool.filter((e) => e.language === sourceLanguage).slice(0, 3);
    if (elites.length > 0) {
      const pick = elites[Math.floor(Math.random() * elites.length)];
      // Overlay elite single-glyph mappings; keep verbose-specific multi-char
      // entries (bigrams + scribal abbrevs) from the fresh seed.
      const merged = { ...seed };
      for (const [k, v] of Object.entries(pick.map)) {
        if (k.length === 1) merged[k] = v;
      }
      seed = merged;
      seedSource = `elite(score=${pick.score.toFixed(3)})`;
    }
  }
  const seedDecoded = applyMap(evaText, seed);
  console.log(`[theory-loop]   verbose seed: ${Object.keys(seed).length} entries, source=${seedSource}, dict=${dictionaryScore(seedDecoded, sourceLanguage).toFixed(3)} starting SA...`);

  const sa = simulatedAnnealVerbose(evaText, seed, sourceLanguage, VERBOSE_SA_STEPS);
  const dictFinal = dictionaryScore(sa.decoded, sourceLanguage);
  const bigramFinal = langModelScore(sa.decoded, sourceLanguage);
  console.log(`[theory-loop]   verbose SA: ${sa.improvements} improvements in ${VERBOSE_SA_STEPS} steps, dict=${dictFinal.toFixed(3)} lm=${bigramFinal.toFixed(3)} combined=${sa.score.toFixed(3)}`);

  // Cross-folio consistency
  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 10);
  for (const testFolio of testFolios) {
    const decoded = applyMap(testFolio.eva_sample, sa.map);
    const expectedTerms = expectedTermsFor(testFolio, sourceLanguage);
    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: broadGrounding(decoded, sourceLanguage, expectedTerms),
    });
  }

  const primaryTerms = expectedTermsFor(targetFolio, sourceLanguage);
  const primaryGrounding = broadGrounding(sa.decoded, sourceLanguage, primaryTerms);
  const consistencyScore = crossFolioResults.length > 0
    ? crossFolioResults.reduce((sum, r) => sum + r.grounding_score, 0) / crossFolioResults.length
    : 0;

  return {
    id: theoryId,
    proposed_at: new Date().toISOString(),
    source_language: sourceLanguage,
    cipher_type: 'verbose',
    target_folio: targetFolio.folio_id,
    target_plant: targetFolio.plant_name,
    symbol_map: sa.map,
    decoded_text: sa.decoded,
    grounding_score: primaryGrounding,
    consistency_score: consistencyScore,
    cross_folio_results: crossFolioResults,
  };
}

// ---------------------------------------------------------------------------
// Positional cipher — same glyph maps to different output by word position.
//
// Voynich's most distinctive structural anomaly: glyphs cluster strongly by
// position within a word. Gallows letters (k, t, p, f) and `q` skew toward
// word-initial; `y`, `n`, `m` skew toward word-final. A position-conditional
// substitution treats word-initial / middle / final positions as separate
// alphabets. Maps are stored flat with i:/m:/f: prefixes so they fit in
// the existing symbol_map: Record<string, string> field.
// ---------------------------------------------------------------------------

const POSITIONAL_SA_STEPS = 8000;

function applyPositionalMap(evaText: string, posMap: Record<string, string>): string {
  const text = evaText.replace(/\./g, ' ');
  const tokens = text.split(/(\s+)/);
  let result = '';
  for (const tok of tokens) {
    if (tok.length === 0) continue;
    if (/^\s+$/.test(tok)) { result += tok; continue; }
    if (tok.length === 1) {
      result += posMap[`i:${tok}`] ?? tok;
      continue;
    }
    result += posMap[`i:${tok[0]}`] ?? tok[0];
    for (let i = 1; i < tok.length - 1; i++) {
      result += posMap[`m:${tok[i]}`] ?? tok[i];
    }
    result += posMap[`f:${tok[tok.length - 1]}`] ?? tok[tok.length - 1];
  }
  return result;
}

/**
 * Build a positional seed map. Three buckets (initial / middle / final),
 * each with a randomized 1:1 mapping over the most common EVA glyphs.
 * If a substitution elite is provided, it seeds all three buckets identically;
 * SA mutations then break the symmetry to find position-specific mappings.
 */
function generatePositionalSeed(
  language: string,
  substitutionElite?: Record<string, string>,
): Record<string, string> {
  const evaGlyphs = ['o','a','e','i','y','c','h','d','k','l','n','r','s','t','q','p','f','m','g','x'];
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;
  const seed: Record<string, string> = {};
  if (substitutionElite) {
    // Seed all three buckets with the elite substitution mapping (where
    // the elite has single-char keys). SA will diverge them as it explores.
    for (const g of evaGlyphs) {
      const mapped = substitutionElite[g];
      const v = mapped && mapped.length === 1 ? mapped : langLetters[Math.floor(Math.random() * langLetters.length)];
      seed[`i:${g}`] = v;
      seed[`m:${g}`] = v;
      seed[`f:${g}`] = v;
    }
  } else {
    const shuffleAlpha = () => [...langLetters].sort(() => Math.random() - 0.5);
    const aI = shuffleAlpha();
    const aM = shuffleAlpha();
    const aF = shuffleAlpha();
    for (let i = 0; i < evaGlyphs.length; i++) {
      seed[`i:${evaGlyphs[i]}`] = aI[i % aI.length];
      seed[`m:${evaGlyphs[i]}`] = aM[i % aM.length];
      seed[`f:${evaGlyphs[i]}`] = aF[i % aF.length];
    }
  }
  return seed;
}

function mutatePositionalMap(posMap: Record<string, string>, language: string): Record<string, string> {
  const result = { ...posMap };
  const keys = Object.keys(result);
  if (keys.length === 0) return result;
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;
  const r = Math.random();
  if (r < 0.5) {
    // Swap two values within the same position bucket
    const k1 = keys[Math.floor(Math.random() * keys.length)];
    const bucket = k1.slice(0, 2);
    const sameBucketKeys = keys.filter((k) => k.startsWith(bucket));
    const k2 = sameBucketKeys[Math.floor(Math.random() * sameBucketKeys.length)];
    if (k1 !== k2) {
      const tmp = result[k1];
      result[k1] = result[k2];
      result[k2] = tmp;
    }
  } else {
    // Replace a single mapping with a fresh random letter
    const k = keys[Math.floor(Math.random() * keys.length)];
    result[k] = langLetters[Math.floor(Math.random() * langLetters.length)];
  }
  return result;
}

async function proposePositionalTheory(
  theoryId: string,
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  seedMode: SeedMode = 'cold',
): Promise<Theory> {
  const evaText = targetFolio.eva_sample;
  await loadElitePool();

  let substitutionElite: Record<string, string> | undefined;
  let seedSource = 'fresh';
  if (seedMode === 'elite') {
    const elites = elitePool.filter((e) => e.language === sourceLanguage).slice(0, 3);
    if (elites.length > 0) {
      const pick = elites[Math.floor(Math.random() * elites.length)];
      substitutionElite = pick.map;
      seedSource = `elite(score=${pick.score.toFixed(3)})`;
    }
  }
  const seed = generatePositionalSeed(sourceLanguage, substitutionElite);
  const seedDecoded = applyPositionalMap(evaText, seed);
  console.log(`[theory-loop]   positional seed: ${Object.keys(seed).length} entries, source=${seedSource}, dict=${dictionaryScore(seedDecoded, sourceLanguage).toFixed(3)} starting SA...`);

  let curMap = seed;
  let curScore = hillClimbScore(applyPositionalMap(evaText, curMap), sourceLanguage);
  let bestMap = curMap;
  let bestDecoded = applyPositionalMap(evaText, curMap);
  let bestScore = curScore;
  let improvements = 0;
  const T_START = 0.08;
  const T_END = 0.005;

  for (let step = 0; step < POSITIONAL_SA_STEPS; step++) {
    const t = T_START * Math.pow(T_END / T_START, step / POSITIONAL_SA_STEPS);
    const cand = mutatePositionalMap(curMap, sourceLanguage);
    const decoded = applyPositionalMap(evaText, cand);
    const score = hillClimbScore(decoded, sourceLanguage);
    const dScore = score - curScore;
    if (dScore > 0 || Math.random() < Math.exp(dScore / Math.max(t, 1e-6))) {
      curMap = cand;
      curScore = score;
      if (score > bestScore) {
        bestMap = cand;
        bestDecoded = decoded;
        bestScore = score;
        improvements++;
      }
    }
  }

  const dictFinal = dictionaryScore(bestDecoded, sourceLanguage);
  const lmFinal = langModelScore(bestDecoded, sourceLanguage);
  console.log(`[theory-loop]   positional SA: ${improvements} improvements in ${POSITIONAL_SA_STEPS} steps, dict=${dictFinal.toFixed(3)} lm=${lmFinal.toFixed(3)} combined=${bestScore.toFixed(3)}`);

  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 10);
  for (const testFolio of testFolios) {
    const decoded = applyPositionalMap(testFolio.eva_sample, bestMap);
    const expectedTerms = expectedTermsFor(testFolio, sourceLanguage);
    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: broadGrounding(decoded, sourceLanguage, expectedTerms),
    });
  }

  const primaryTerms = expectedTermsFor(targetFolio, sourceLanguage);
  const primaryGrounding = broadGrounding(bestDecoded, sourceLanguage, primaryTerms);
  const consistencyScore = crossFolioResults.length > 0
    ? crossFolioResults.reduce((sum, r) => sum + r.grounding_score, 0) / crossFolioResults.length
    : 0;

  return {
    id: theoryId,
    proposed_at: new Date().toISOString(),
    source_language: sourceLanguage,
    cipher_type: 'positional',
    target_folio: targetFolio.folio_id,
    target_plant: targetFolio.plant_name,
    symbol_map: bestMap,
    decoded_text: bestDecoded,
    grounding_score: primaryGrounding,
    consistency_score: consistencyScore,
    cross_folio_results: crossFolioResults,
  };
}

// ---------------------------------------------------------------------------
// Homophonic cipher — Naibbe-style verbose homophonic substitution.
//
// Inspired by Greshko et al. (Cryptologia 2025): each plaintext letter encodes
// to one of MANY distinct multi-glyph Voynichese strings. Decryption inverts
// this — many glyph-strings collapse to the same plaintext letter. This is
// the inverse cardinality of our verbose cipher (which mapped one glyph to a
// multi-char string). It explicitly attacks the central pathology Karl K
// identified: a 1:1 substitution can't put Voynich's structural weirdness
// anywhere except the plaintext, so it produces gibberish. Homophonic gives
// the cipher somewhere to hide the structural variety.
// ---------------------------------------------------------------------------

const HOMOPHONIC_SA_STEPS = 8000;

/**
 * Curated EVA tokens used as homophone alphabet. Mix of single chars and
 * common digraph/trigraph/word-fragment patterns from EVA literature.
 * The hill-climber assigns each token a single plaintext letter; multiple
 * tokens can share the same letter (homophonic). applyMap's longest-first
 * matching greedily tokenizes Voynich text against this dictionary.
 */
const HOMOPHONIC_TOKENS: string[] = [
  // Singles (most common EVA glyphs)
  'o', 'a', 'e', 'i', 'y', 'c', 'h', 'd', 'k', 'l', 'n', 'r', 's', 't', 'q', 'p', 'f', 'm', 'g',
  // Common digraphs
  'ch', 'sh', 'ee', 'qo', 'dy', 'ar', 'or', 'ol', 'ed', 'ai', 'in', 'ck', 'th', 'oe', 'ho', 'ke', 'te', 'al',
  // Common trigraphs / common word-fragments
  'chy', 'shy', 'qok', 'qot', 'eed', 'edy', 'ody', 'oky', 'ain', 'ched', 'shed', 'okai', 'otai',
  // Common 4+ grams (these are full Voynich words or near-words)
  'aiin', 'chedy', 'shedy', 'daiin', 'qokai', 'qotai', 'okeedy', 'qokeedy',
];

/**
 * Generate a homophonic seed map. Each token is assigned a single plaintext
 * letter from the language alphabet, frequency-biased so that high-frequency
 * letters (e, a, i, t, n in Latin/Italian) get more homophones — matching
 * the encoding direction where common letters need more cipher options to
 * avoid one-glyph-fits-all attacks.
 */
function generateHomophonicSeed(
  language: string,
  substitutionElite?: Record<string, string>,
): Record<string, string> {
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;
  // Top letters get sampled with higher probability — Zipf-like.
  const weighted: string[] = [];
  for (let i = 0; i < langLetters.length; i++) {
    const w = Math.max(1, Math.round(8 / (i + 1)));
    for (let j = 0; j < w; j++) weighted.push(langLetters[i]);
  }
  const pickLetter = () => weighted[Math.floor(Math.random() * weighted.length)];

  const seed: Record<string, string> = {};
  for (const tok of HOMOPHONIC_TOKENS) {
    if (substitutionElite && tok.length === 1 && substitutionElite[tok] && substitutionElite[tok].length === 1) {
      // Inherit single-char mappings from substitution elite — gives the search
      // a head start on glyphs that already had a strong baseline.
      seed[tok] = substitutionElite[tok];
    } else {
      seed[tok] = pickLetter();
    }
  }
  return seed;
}

function mutateHomophonicMap(map: Record<string, string>, language: string): Record<string, string> {
  const result = { ...map };
  const keys = Object.keys(result);
  if (keys.length === 0) return result;
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;
  const r = Math.random();
  if (r < 0.7) {
    // Replace a single token's letter
    const k = keys[Math.floor(Math.random() * keys.length)];
    result[k] = langLetters[Math.floor(Math.random() * langLetters.length)];
  } else {
    // Swap letters between two tokens (can keep homophone counts balanced)
    const k1 = keys[Math.floor(Math.random() * keys.length)];
    const k2 = keys[Math.floor(Math.random() * keys.length)];
    if (k1 !== k2) {
      const tmp = result[k1];
      result[k1] = result[k2];
      result[k2] = tmp;
    }
  }
  return result;
}

async function proposeHomophonicTheory(
  theoryId: string,
  targetFolio: FolioInfo,
  allFolios: FolioInfo[],
  sourceLanguage: string,
  seedMode: SeedMode = 'cold',
): Promise<Theory> {
  const evaText = targetFolio.eva_sample;
  await loadElitePool();

  let substitutionElite: Record<string, string> | undefined;
  let seedSource = 'fresh';
  if (seedMode === 'elite') {
    const elites = elitePool.filter((e) => e.language === sourceLanguage).slice(0, 3);
    if (elites.length > 0) {
      const pick = elites[Math.floor(Math.random() * elites.length)];
      substitutionElite = pick.map;
      seedSource = `elite(score=${pick.score.toFixed(3)})`;
    }
  }
  const seed = generateHomophonicSeed(sourceLanguage, substitutionElite);
  const seedDecoded = applyMap(evaText, seed);
  console.log(`[theory-loop]   homophonic seed: ${Object.keys(seed).length} tokens, source=${seedSource}, dict=${dictionaryScore(seedDecoded, sourceLanguage).toFixed(3)} starting SA...`);

  let curMap = seed;
  let curScore = hillClimbScore(applyMap(evaText, curMap), sourceLanguage);
  let bestMap = curMap;
  let bestDecoded = applyMap(evaText, curMap);
  let bestScore = curScore;
  let improvements = 0;
  const T_START = 0.08;
  const T_END = 0.005;

  for (let step = 0; step < HOMOPHONIC_SA_STEPS; step++) {
    const t = T_START * Math.pow(T_END / T_START, step / HOMOPHONIC_SA_STEPS);
    const cand = mutateHomophonicMap(curMap, sourceLanguage);
    const decoded = applyMap(evaText, cand);
    const score = hillClimbScore(decoded, sourceLanguage);
    const dScore = score - curScore;
    if (dScore > 0 || Math.random() < Math.exp(dScore / Math.max(t, 1e-6))) {
      curMap = cand;
      curScore = score;
      if (score > bestScore) {
        bestMap = cand;
        bestDecoded = decoded;
        bestScore = score;
        improvements++;
      }
    }
  }

  const dictFinal = dictionaryScore(bestDecoded, sourceLanguage);
  const lmFinal = langModelScore(bestDecoded, sourceLanguage);
  console.log(`[theory-loop]   homophonic SA: ${improvements} improvements in ${HOMOPHONIC_SA_STEPS} steps, dict=${dictFinal.toFixed(3)} lm=${lmFinal.toFixed(3)} combined=${bestScore.toFixed(3)}`);

  const crossFolioResults: Theory['cross_folio_results'] = [];
  const testFolios = allFolios
    .filter((f) => f.folio_id !== targetFolio.folio_id && f.confidence >= 0.4)
    .slice(0, 10);
  for (const testFolio of testFolios) {
    const decoded = applyMap(testFolio.eva_sample, bestMap);
    const expectedTerms = expectedTermsFor(testFolio, sourceLanguage);
    crossFolioResults.push({
      folio_id: testFolio.folio_id,
      plant_expected: testFolio.plant_name,
      decoded_text: decoded.slice(0, 50),
      grounding_score: broadGrounding(decoded, sourceLanguage, expectedTerms),
    });
  }

  const primaryTerms = expectedTermsFor(targetFolio, sourceLanguage);
  const primaryGrounding = broadGrounding(bestDecoded, sourceLanguage, primaryTerms);
  const consistencyScore = crossFolioResults.length > 0
    ? crossFolioResults.reduce((sum, r) => sum + r.grounding_score, 0) / crossFolioResults.length
    : 0;

  return {
    id: theoryId,
    proposed_at: new Date().toISOString(),
    source_language: sourceLanguage,
    cipher_type: 'homophonic',
    target_folio: targetFolio.folio_id,
    target_plant: targetFolio.plant_name,
    symbol_map: bestMap,
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
 * Reeds-style null preprocessor: strip word-initial `q` and word-final `y`.
 * These two glyphs together account for ~25% of the EVA corpus and cluster
 * suspiciously by word position — common candidates for scribal markers /
 * filler rather than content. Stripping them before substitution gives the
 * hill-climber a smaller, less constrained alphabet to fit.
 */
function stripNulls(evaText: string): string {
  return evaText.split(/(\s+|\.+)/).map((tok) => {
    if (/^[\s.]+$/.test(tok)) return tok;
    let w = tok;
    if (w.startsWith('q')) w = w.slice(1);
    if (w.endsWith('y')) w = w.slice(0, -1);
    return w;
  }).join('');
}

// ---------------------------------------------------------------------------
// Verbose cipher — many-to-many EVA→Latin map searched via simulated annealing
// ---------------------------------------------------------------------------

/**
 * Build a verbose seed map. Keys are EVA singles, bigrams, or trigrams; values
 * are 0–3 plaintext characters. Covers scribal abbreviations as a special case
 * (`y` → `us`, `dy` → `tio`).
 */
function generateVerboseSeed(language: string): Record<string, string> {
  const map: Record<string, string> = {};
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;

  // Single glyphs — frequency-aligned to language baseline
  const singles = ['o','a','i','n','s','e','l','r','c','d','t','k','f','p','m','h'];
  singles.forEach((s, i) => { map[s] = langLetters[i % langLetters.length]; });

  // EVA bigrams that appear unusually often → likely encode common 2-letter Latin sequences
  const evaBigrams = ['qo','ch','sh','th','ct','ee','ar','or','ol','ai','ey'];
  const latinSeqs = language === 'latin'
    ? ['qu','ch','st','tu','ct','ee','ar','or','al','ae','er']
    : ['qu','ch','sc','tt','tt','ee','ar','or','al','ai','er'];
  evaBigrams.forEach((b, i) => { map[b] = latinSeqs[i] ?? langLetters[i % langLetters.length]; });

  // Scribal abbreviation candidates — single EVA glyph or digraph → multi-char suffix
  if (language === 'latin') {
    map.y = 'us';
    map.dy = 'tio';
    map.aiin = 'ium';
    map.iin = 'um';
    map.q = '';
  } else {
    map.y = 'i';
    map.dy = 'zione';
    map.aiin = 'ione';
    map.iin = 'one';
    map.q = '';
  }
  return map;
}

/**
 * Mutate a verbose map. One of: swap output values, change a char in an
 * output, lengthen/shorten an output by 1, or add/remove a bigram entry.
 */
function mutateVerboseMap(map: Record<string, string>, language: string): Record<string, string> {
  const result = { ...map };
  const keys = Object.keys(result);
  if (keys.length < 2) return result;
  const langLetters = LANG_FREQ[language] ?? LANG_FREQ.latin;
  const op = Math.random();

  if (op < 0.4) {
    // Swap output values between two entries
    const i = Math.floor(Math.random() * keys.length);
    let j = Math.floor(Math.random() * keys.length);
    if (i === j) j = (j + 1) % keys.length;
    [result[keys[i]], result[keys[j]]] = [result[keys[j]], result[keys[i]]];
  } else if (op < 0.7) {
    // Replace one character in an output
    const k = keys[Math.floor(Math.random() * keys.length)];
    const v = result[k];
    if (v.length > 0) {
      const ci = Math.floor(Math.random() * v.length);
      const newChar = langLetters[Math.floor(Math.random() * langLetters.length)];
      result[k] = v.slice(0, ci) + newChar + v.slice(ci + 1);
    } else {
      result[k] = langLetters[Math.floor(Math.random() * langLetters.length)];
    }
  } else if (op < 0.85) {
    // Lengthen or shorten an output
    const k = keys[Math.floor(Math.random() * keys.length)];
    const v = result[k];
    if (v.length > 0 && Math.random() < 0.5) {
      result[k] = v.slice(0, -1);
    } else if (v.length < 4) {
      result[k] = v + langLetters[Math.floor(Math.random() * langLetters.length)];
    }
  } else {
    // Add or remove an EVA bigram entry
    const evaCandidates = ['qok','dy','ey','ar','or','ol','aiin','iin','chol','ched','shed','okeedy','chedy','shedy','qokeedy','qokedy','okain'];
    const ec = evaCandidates[Math.floor(Math.random() * evaCandidates.length)];
    if (ec in result && Math.random() < 0.5) {
      delete result[ec];
    } else {
      const len = 1 + Math.floor(Math.random() * 3);
      let newVal = '';
      for (let c = 0; c < len; c++) {
        newVal += langLetters[Math.floor(Math.random() * langLetters.length)];
      }
      result[ec] = newVal;
    }
  }

  return result;
}

/**
 * Simulated annealing search over verbose maps. Greedy hill-climbing fails
 * here because the search space is much larger and full of plateaus —
 * accepting occasional worsening moves (with cooling temperature) escapes
 * local optima.
 */
function simulatedAnnealVerbose(
  evaText: string,
  initialMap: Record<string, string>,
  language: string,
  steps: number,
): { map: Record<string, string>; decoded: string; score: number; improvements: number } {
  let curMap = initialMap;
  let curDecoded = applyMap(evaText, curMap);
  let curScore = hillClimbScore(curDecoded, language);
  let bestMap = curMap;
  let bestDecoded = curDecoded;
  let bestScore = curScore;
  let improvements = 0;

  const T_START = 0.08;
  const T_END = 0.005;

  for (let step = 0; step < steps; step++) {
    const t = T_START * Math.pow(T_END / T_START, step / steps);
    const cand = mutateVerboseMap(curMap, language);
    const decoded = applyMap(evaText, cand);
    const score = hillClimbScore(decoded, language);
    const dScore = score - curScore;

    if (dScore > 0 || Math.random() < Math.exp(dScore / Math.max(t, 1e-6))) {
      curMap = cand;
      curDecoded = decoded;
      curScore = score;
      if (score > bestScore) {
        bestMap = cand;
        bestDecoded = decoded;
        bestScore = score;
        improvements++;
      }
    }
  }
  return { map: bestMap, decoded: bestDecoded, score: bestScore, improvements };
}

/**
 * Score how well decoded text matches expected plant terms.
 * Returns 0-1 based on exact matches, substring matches, and prefix matches.
 * Saturating denominator: 5 strong matches = full score. Critical when
 * term lists vary in size — dividing by terms.length would penalize the
 * richer image-derived term lists vs the thin baseline metadata.
 */
function scoreTermOverlap(text: string, terms: string[]): number {
  if (terms.length === 0) return 0;
  const decoded = text.toLowerCase().replace(/[^a-z\s]/g, ' ');
  const tokens = decoded.split(/\s+/).filter((t) => t.length > 2);
  if (tokens.length === 0) return 0;

  // Each unique decoded token can be credited at most once across all terms.
  // Without this, a single repeated decoded word (e.g. `vino` x12) collects
  // hits via prefix/substring matches against many similar terms in the list,
  // faking a high score from low diversity.
  const creditedTokens = new Set<string>();
  let score = 0;
  for (const term of terms) {
    const tl = term.toLowerCase();
    if (tokens.includes(tl) && !creditedTokens.has(tl)) {
      score += 1.0;
      creditedTokens.add(tl);
      continue;
    }
    if (decoded.includes(tl)) {
      const matchTok = tokens.find((t) => t.includes(tl) && !creditedTokens.has(t));
      if (matchTok) {
        score += 0.7;
        creditedTokens.add(matchTok);
      }
      continue;
    }
    if (tl.length >= 4) {
      const matchTok = tokens.find((t) => t.startsWith(tl.slice(0, 5)) && !creditedTokens.has(t));
      if (matchTok) {
        score += 0.4;
        creditedTokens.add(matchTok);
      }
    }
  }
  return Math.min(1.0, score / 5.0);
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
  const lmScore = langModelScore(text, language);
  const quality = dictionaryQuality(text, language);

  return termScore * 0.3 + (dictScore_ * quality) * 0.4 + lmScore * 0.3;
}

/**
 * Build the term list for grounding a folio. Combines the thin metadata
 * (plant name, Latin binomial, structured features) with the rich image-derived
 * terms backfilled into folio_vision_analysis.expected_terms by an LLM that
 * read the visual_description. Typically yields 50-60 terms per language.
 */
function expectedTermsFor(folio: FolioInfo, language: string): string[] {
  const imageTerms = folio.expected_terms[language] ?? [];
  return [
    folio.plant_name.toLowerCase(),
    folio.plant_latin.toLowerCase(),
    ...folio.botanical_features.map((f) => f.toLowerCase()),
    ...imageTerms,
  ].filter(Boolean);
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

/** Optional callback for live activity reporting to the dashboard. */
type TheoryResultCallback = (entry: {
  round: number; batch: number; folio: string; plant: string; lang: string;
  cipher: string; grounding: number; consistency: number; combined: number;
  dictScore: number; improvements: number; seedOrigin: string; decoded: string;
}) => void;

let _onTheoryResult: TheoryResultCallback | null = null;
export function setOnTheoryResult(cb: TheoryResultCallback) { _onTheoryResult = cb; }

async function loadStrategyStats(): Promise<void> {
  if (strategyStatsLoaded) return;
  strategyStatsLoaded = true;
  try {
    await executeSql(`
      CREATE TABLE IF NOT EXISTS serverless_stable_qh44kx_catalog.voynich.strategy_stats (
        strategy_key STRING,
        attempts INT,
        best_score DOUBLE,
        last_attempted_at TIMESTAMP,
        exhausted BOOLEAN
      ) USING DELTA
    `);
    const rows = await executeSql(`
      SELECT strategy_key, attempts, best_score,
             CAST(last_attempted_at AS STRING) AS last_attempted_at,
             exhausted
      FROM serverless_stable_qh44kx_catalog.voynich.strategy_stats
    `);
    for (const r of rows) {
      const exhaustedRaw: unknown = r.exhausted;
      strategyStats.set(r.strategy_key, {
        attempts: parseInt(r.attempts),
        best_score: parseFloat(r.best_score),
        last_attempted_at: r.last_attempted_at,
        exhausted: exhaustedRaw === true || exhaustedRaw === 'true',
      });
    }
    if (strategyStats.size > 0) {
      console.log(`[strategy] Loaded ${strategyStats.size} strategy stats from Delta`);
    }
  } catch (err) {
    console.warn('[strategy] Failed to load strategy stats:', err);
  }
}

async function persistStrategyStat(key: string, stat: StrategyStat): Promise<void> {
  const ts = stat.last_attempted_at.replace('T', ' ').replace('Z', '');
  try {
    await executeSql(`
      MERGE INTO serverless_stable_qh44kx_catalog.voynich.strategy_stats AS t
      USING (SELECT
        '${key}' AS strategy_key,
        CAST(${stat.attempts} AS INT) AS attempts,
        CAST(${stat.best_score} AS DOUBLE) AS best_score,
        TIMESTAMP '${ts}' AS last_attempted_at,
        ${stat.exhausted} AS exhausted
      ) AS s
      ON t.strategy_key = s.strategy_key
      WHEN MATCHED THEN UPDATE SET
        attempts = s.attempts,
        best_score = s.best_score,
        last_attempted_at = s.last_attempted_at,
        exhausted = s.exhausted
      WHEN NOT MATCHED THEN INSERT (strategy_key, attempts, best_score, last_attempted_at, exhausted)
        VALUES (s.strategy_key, s.attempts, s.best_score, s.last_attempted_at, s.exhausted)
    `);
  } catch (err) {
    console.warn(`[strategy] Failed to persist stat for ${key}:`, err);
  }
}

function pickNextStrategy(): Strategy {
  // 1. Untried strategies first
  for (const s of STRATEGIES) {
    if (!strategyStats.has(strategyKey(s))) return s;
  }
  // 2. Non-exhausted, oldest last_attempted_at
  const live = STRATEGIES.filter((s) => !strategyStats.get(strategyKey(s))!.exhausted);
  if (live.length > 0) {
    live.sort((a, b) =>
      strategyStats.get(strategyKey(a))!.last_attempted_at.localeCompare(
        strategyStats.get(strategyKey(b))!.last_attempted_at
      ),
    );
    return live[0];
  }
  // 3. All exhausted — clear flags and pick weakest (give losers another shot
  //    against the now-richer elite pool).
  console.log('[strategy] All strategies exhausted — resetting and trying weakest');
  for (const s of STRATEGIES) strategyStats.get(strategyKey(s))!.exhausted = false;
  const sorted = [...STRATEGIES].sort((a, b) =>
    strategyStats.get(strategyKey(a))!.best_score - strategyStats.get(strategyKey(b))!.best_score
  );
  return sorted[0];
}

export async function runTheoryLoop(numBursts: number = 10, batch: number = 0): Promise<Theory[]> {
  const folios = await loadFolios();
  const highConfidence = folios.filter((f) => f.confidence >= 0.5);
  await loadStrategyStats();

  console.log(`[theory-loop] Starting ${numBursts} bursts × ${ROUNDS_PER_BURST} rounds with ${highConfidence.length} high-confidence folios`);

  const theories: Theory[] = [];

  for (let burst = 0; burst < numBursts; burst++) {
    const strategy = pickNextStrategy();
    const key = strategyKey(strategy);
    const prevStat = strategyStats.get(key);
    const prevBest = prevStat?.best_score ?? 0;
    const attempts = prevStat?.attempts ?? 0;

    console.log(`[strategy] BURST ${burst + 1}/${numBursts}: ${key} (prev_best=${prevBest.toFixed(3)}, attempts=${attempts})`);

    let burstBest = 0;
    for (let round = 0; round < ROUNDS_PER_BURST; round++) {
      const folio = highConfidence[Math.floor(Math.random() * highConfidence.length)];
      console.log(`[theory-loop] Burst ${burst + 1} Round ${round}: ${folio.folio_id} (${folio.plant_name}) [${strategy.cipherType}/${strategy.seedMode}]`);

      const traceLabel = `burst ${burst + 1}/round ${round} ${folio.folio_id} [${strategy.cipherType}/${strategy.language}/${strategy.seedMode}]`;
      try {
        const combined = await withRoundTrace('voynich-orchestrator', traceLabel, async () => {
          const theory = await proposeTheory(folio, folios, strategy.language, strategy.cipherType, strategy.seedMode);
          const c = theory.grounding_score + theory.consistency_score;

          console.log(`[theory-loop]   grounding=${theory.grounding_score.toFixed(3)} consistency=${theory.consistency_score.toFixed(3)} decoded="${theory.decoded_text.slice(0, 50)}"`);

          if (_onTheoryResult) {
            _onTheoryResult({
              round, batch: burst, folio: folio.folio_id, plant: folio.plant_name,
              lang: strategy.language, cipher: strategy.cipherType,
              grounding: theory.grounding_score, consistency: theory.consistency_score,
              combined: c,
              dictScore: dictionaryScore(theory.decoded_text, strategy.language),
              improvements: 0, seedOrigin: strategy.seedMode,
              decoded: theory.decoded_text,
            });
          }

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
          await persistTheory(theory, verdict);
          return c;
        });
        if (combined > burstBest) burstBest = combined;
      } catch (err) {
        console.error(`[theory-loop] Burst ${burst + 1} Round ${round} failed:`, err);
      }
    }

    // Evaluate strategy progress
    const delta = burstBest - prevBest;
    const exhausted = delta < PROGRESS_THRESHOLD;
    const newStat: StrategyStat = {
      attempts: attempts + 1,
      best_score: Math.max(prevBest, burstBest),
      last_attempted_at: new Date().toISOString(),
      exhausted,
    };
    strategyStats.set(key, newStat);
    await persistStrategyStat(key, newStat);

    const sign = delta >= 0 ? '+' : '';
    const flag = exhausted ? '⊘ exhausted' : '✓ progressing';
    console.log(`[strategy] BURST ${burst + 1} done: ${key} burst_best=${burstBest.toFixed(3)} (Δ=${sign}${delta.toFixed(3)}) ${flag}`);
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

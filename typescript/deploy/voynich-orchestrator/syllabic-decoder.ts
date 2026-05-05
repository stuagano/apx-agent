/**
 * Syllabic/morpheme codebook decoder.
 *
 * Hypothesis: each Voynich EVA word is a ciphertext unit that maps to one
 * Latin SYLLABLE (or morpheme). The combined stream of syllables forms
 * medieval Latin when read continuously.
 *
 * Approach:
 *   1. Count EVA word frequencies across the corpus → top N_VOCAB words
 *   2. Build a Latin syllable inventory from the training corpus
 *   3. Initialize codebook: frequency-rank match (most common EVA word →
 *      most common Latin syllable, etc.) with random perturbation
 *   4. SA hill-climbing: swap two EVA word → syllable assignments,
 *      keep if syllable-stream trigram score improves
 *   5. Decode a folio: replace each EVA word with its assigned syllable,
 *      concatenate with space boundaries matching EVA sentence structure
 *   6. Score with the existing langModelScore on the syllable stream
 *
 * Unlike substitution (glyph → letter), this is a word-level codebook.
 * The search space is N_VOCAB! but we only hill-climb swap moves.
 *
 * Key structural difference from substitution:
 *   - Output unit is a syllable, not a letter → much richer per-token information
 *   - If the hypothesis is right, the n-gram score should rise sharply
 *     because the model is seeing plausible Latin trigrams
 *   - If EVA words map to a real morpheme inventory, vocabulary diversity
 *     should be high (many distinct syllables in output)
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx syllabic-decoder.ts
 *
 * Tunables:
 *   N_VOCAB=200   number of top EVA words to include in codebook
 *   N_SYLLABLES=200  syllable inventory size (pulled from Latin corpus)
 *   SA_STEPS=5000 simulated annealing steps per restart
 *   N_RESTARTS=5  number of SA restarts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const N_VOCAB = parseInt(process.env.N_VOCAB ?? '200');
const N_SYLLABLES = parseInt(process.env.N_SYLLABLES ?? '200');
const SA_STEPS = parseInt(process.env.SA_STEPS ?? '5000');
const N_RESTARTS = parseInt(process.env.N_RESTARTS ?? '5');

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
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Latin syllable inventory — extracted from LATIN_CORPUS
// ---------------------------------------------------------------------------

const LATIN_CORPUS = `
mandragora habet folia similia brassicae sed maiora et latiora atque pilosa
radix alba et longa cortice nigro et interior est alba velut rapum
nascitur in locis cultis et hortulanis folia in terra prostrata iacent
herbarum omnium potentissima est et multas habet vires medicinales
recipe folium mentae et tere cum aceto et impone super tempora dolenti
cannabis nascitur in locis humidis et palustris folia habet multa et pinnata
folia viridissima et odorifera succum habent acutum et calefacientem
herba coquitur in aqua et decocto utimur contra venenum et morsus serpentis
radix contrita cum vino et melle bibitur contra tussim antiquam et duram
verbena herba sacra est et habet flores parvos et purpureos contra venenum
absinthium amarum est et tollit vermes et purgat hepar et lien et stomachum
artemisia mater herbarum dicitur et iuvat morbos matricis et menstrua provocat
melissa habet odorem suavem et confortat cor et oculos et virtutes vitales
recipe semen anethi et tere subtiliter et misce cum oleo et vino calido
folium hederae cum aceto bibitum tollit dolorem capitis et tussim purgat
plantago herba frigida et sicca est et confortat stomachum et sistit fluxum
betonica multarum virtutum herba est et sanat dolores capitis antiquos
salvia calida et sicca est et confortat nervos et iuvat memoriam
ruta acris est et calida et purgat visum et valet contra venenum
urtica calida est et purgat sanguinem et iuvat reumata articulorum
`;

/**
 * Extract syllable inventory from Latin text.
 * Strategy: split each word into overlapping 2-4 char substrings that look
 * like Latin syllables (consonant + vowel patterns). Then rank by frequency.
 */
function extractSyllableInventory(corpus: string, maxSyllables: number): string[] {
  const words = corpus.toLowerCase().replace(/[^a-z\s]/g, '').split(/\s+/).filter((w) => w.length >= 2);
  const syllableFreqs = new Map<string, number>();

  const vowels = new Set(['a', 'e', 'i', 'o', 'u']);

  for (const word of words) {
    // Extract 2-5 char substrings that contain at least one vowel
    for (let start = 0; start < word.length; start++) {
      for (let len = 2; len <= 5 && start + len <= word.length; len++) {
        const syl = word.slice(start, start + len);
        if ([...syl].some((c) => vowels.has(c))) {
          syllableFreqs.set(syl, (syllableFreqs.get(syl) ?? 0) + 1);
        }
      }
    }
    // Also include the whole word (words themselves are syllables if short)
    if (word.length <= 5) {
      syllableFreqs.set(word, (syllableFreqs.get(word) ?? 0) + 1);
    }
  }

  // Return top N by frequency (most common Latin fragments → most mappable)
  return [...syllableFreqs.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, maxSyllables)
    .map(([s]) => s);
}

// ---------------------------------------------------------------------------
// Trigram language model (same as theory-loop.ts)
// ---------------------------------------------------------------------------

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

const LATIN_MODEL = buildTrigramModel(LATIN_CORPUS);

/**
 * Score a text against the Latin trigram model.
 * Returns 0-1; calibration: real Latin ≈ 0.65, gibberish ≈ 0.15.
 */
function langModelScore(text: string): number {
  const clean = ` ${text.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  if (clean.length < 5) return 0;
  const { ngrams, total, vocabSize } = LATIN_MODEL;
  let logProbSum = 0;
  let count = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    const c = ngrams.get(tg) ?? 0;
    logProbSum += Math.log((c + 1) / (total + vocabSize));
    count++;
  }
  if (count === 0) return 0;
  const avgLogProb = logProbSum / count;
  const REF_BAD = -8.5;
  const REF_GOOD = -5.5;
  return Math.max(0, Math.min(1, (avgLogProb - REF_BAD) / (REF_GOOD - REF_BAD)));
}

/**
 * Vocabulary diversity bonus: reward output that has diverse distinct syllables.
 * Penalises decodings that map everything to one or two syllables.
 */
function diversityBonus(syllableStream: string[]): number {
  if (syllableStream.length === 0) return 0;
  const uniqueCount = new Set(syllableStream).size;
  return Math.min(1, uniqueCount / Math.max(1, syllableStream.length * 0.3));
}

// ---------------------------------------------------------------------------
// EVA word frequency analysis
// ---------------------------------------------------------------------------

function parseEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, '.')
    .split('.')
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length > 0 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Codebook decoder
// ---------------------------------------------------------------------------

/**
 * Decode EVA text using the codebook.
 * Returns a syllable-stream string: syllables joined by spaces, with line
 * breaks where the EVA text had sentence boundaries (groups of ~10 words).
 */
function decode(evaText: string, codebook: Map<string, string>, syllableVocab: string[]): string {
  const fallback = syllableVocab[0] ?? 'x';
  const words = parseEvaWords(evaText);
  const syllables = words.map((w) => codebook.get(w) ?? fallback);
  // Group syllables into "words" of 2-3 syllables separated by space,
  // then put a line break every ~8 such groups. This gives the scorer
  // word-shaped units to work with rather than one long blob.
  const chunks: string[] = [];
  for (let i = 0; i < syllables.length; i += 2) {
    chunks.push(syllables.slice(i, i + 2).join(''));
  }
  const lines: string[] = [];
  for (let i = 0; i < chunks.length; i += 8) {
    lines.push(chunks.slice(i, i + 8).join(' '));
  }
  return lines.join('\n');
}

function scoreDecode(decoded: string, syllableStream: string[]): number {
  const lm = langModelScore(decoded);
  const div = diversityBonus(syllableStream);
  // Weight: n-gram is the primary signal; diversity prevents collapse
  return lm * 0.80 + div * 0.20;
}

// ---------------------------------------------------------------------------
// Simulated annealing over codebook
// ---------------------------------------------------------------------------

function runSA(
  evaTexts: string[],
  topEvaWords: string[],
  syllableVocab: string[],
  saSteps: number,
): { codebook: Map<string, string>; score: number } {
  // Initialize codebook: frequency-rank match with noise
  const shuffledSyllables = [...syllableVocab].sort(() => Math.random() - 0.5);
  const codebook = new Map<string, string>();
  for (let i = 0; i < topEvaWords.length; i++) {
    codebook.set(topEvaWords[i], shuffledSyllables[i % shuffledSyllables.length]);
  }

  // Score on combined EVA texts (first 5 folios for speed)
  const sampleTexts = evaTexts.slice(0, 5).join(' . ');

  function currentScore(): { score: number; syllables: string[] } {
    const words = parseEvaWords(sampleTexts);
    const syllables = words.map((w) => codebook.get(w) ?? syllableVocab[0]);
    const decoded = decode(sampleTexts, codebook, syllableVocab);
    return { score: scoreDecode(decoded, syllables), syllables };
  }

  let { score: bestScore } = currentScore();
  let currentBest = new Map(codebook);
  let bestFinal = new Map(codebook);
  let bestFinalScore = bestScore;

  const vocabKeys = topEvaWords;

  for (let step = 0; step < saSteps; step++) {
    const temperature = 0.3 * Math.exp(-4 * step / saSteps);

    // Mutation: swap two EVA words' syllable assignments
    const i = Math.floor(Math.random() * vocabKeys.length);
    const j = Math.floor(Math.random() * vocabKeys.length);
    if (i === j) continue;

    const ki = vocabKeys[i];
    const kj = vocabKeys[j];
    const vi = codebook.get(ki) ?? syllableVocab[0];
    const vj = codebook.get(kj) ?? syllableVocab[0];

    codebook.set(ki, vj);
    codebook.set(kj, vi);

    const { score: newScore } = currentScore();
    const delta = newScore - bestScore;

    if (delta > 0 || Math.random() < Math.exp(delta / Math.max(temperature, 1e-6))) {
      bestScore = newScore;
      currentBest = new Map(codebook);
      if (bestScore > bestFinalScore) {
        bestFinalScore = bestScore;
        bestFinal = new Map(codebook);
      }
    } else {
      // Revert
      codebook.set(ki, vi);
      codebook.set(kj, vj);
    }
  }

  return { codebook: bestFinal, score: bestFinalScore };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('syllabic-decoder');
  console.log(`  N_VOCAB=${N_VOCAB}, N_SYLLABLES=${N_SYLLABLES}, SA_STEPS=${SA_STEPS}, N_RESTARTS=${N_RESTARTS}`);
  console.log('');

  // 1. Load EVA corpus (herbal section)
  console.log('Loading EVA corpus (herbal)...');
  const rows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
    ORDER BY folio_id
    LIMIT 50
  `);
  console.log(`Loaded ${rows.length} folios`);

  const folioTexts = rows
    .filter((r) => r.eva_text)
    .map((r) => r.eva_text as string);

  // 2. Count EVA word frequencies
  const wordFreqs = new Map<string, number>();
  for (const text of folioTexts) {
    for (const word of parseEvaWords(text)) {
      wordFreqs.set(word, (wordFreqs.get(word) ?? 0) + 1);
    }
  }
  const topEvaWords = [...wordFreqs.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, N_VOCAB)
    .map(([w]) => w);

  console.log(`Top EVA words (N_VOCAB=${N_VOCAB}): ${topEvaWords.slice(0, 10).join(', ')} ...`);
  console.log(`  unique EVA words in corpus: ${wordFreqs.size}`);

  // 3. Build syllable inventory
  const syllableVocab = extractSyllableInventory(LATIN_CORPUS, N_SYLLABLES);
  console.log(`Latin syllable inventory (top ${syllableVocab.length}): ${syllableVocab.slice(0, 15).join(', ')} ...`);
  console.log('');

  // 4. SA multi-restart
  let overallBestScore = -Infinity;
  let overallBestCodebook: Map<string, string> = new Map();

  const restartScores: number[] = [];

  for (let restart = 0; restart < N_RESTARTS; restart++) {
    const start = Date.now();
    const { codebook, score } = runSA(folioTexts, topEvaWords, syllableVocab, SA_STEPS);
    const elapsed = ((Date.now() - start) / 1000).toFixed(1);
    restartScores.push(score);
    console.log(`  Restart ${restart + 1}/${N_RESTARTS}: score=${score.toFixed(4)} (${elapsed}s)`);
    if (score > overallBestScore) {
      overallBestScore = score;
      overallBestCodebook = codebook;
    }
  }

  const restartSpread = Math.max(...restartScores) - Math.min(...restartScores);
  console.log(`\nRestart spread: ${restartSpread.toFixed(4)} (low = no exploitable gradient)`);
  console.log(`Best score: ${overallBestScore.toFixed(4)}`);
  console.log('');

  // 5. Decode a sample folio with the best codebook
  console.log('=== Sample decoded output (first 3 folios) ===');
  for (let i = 0; i < Math.min(3, folioTexts.length); i++) {
    const folio = rows[i].folio_id ?? `folio-${i}`;
    const decoded = decode(folioTexts[i], overallBestCodebook, syllableVocab);
    const score = langModelScore(decoded);
    console.log(`\nFolio ${folio} (lm_score=${score.toFixed(3)}):`);
    // Print first 200 chars of decoded
    console.log('  ' + decoded.slice(0, 200).replace(/\n/g, '\n  '));
  }

  // 6. Codebook inspection — top 20 most frequent mappings
  console.log('\n=== Codebook (top 20 EVA words by frequency) ===');
  for (const word of topEvaWords.slice(0, 20)) {
    const syllable = overallBestCodebook.get(word) ?? '?';
    const freq = wordFreqs.get(word) ?? 0;
    console.log(`  ${word.padEnd(15)} → ${syllable.padEnd(8)} (freq=${freq})`);
  }

  // 7. Interpretation
  console.log('\n=== Interpretation ===');
  console.log(`LM score calibration: real Latin ≈ 0.65, gibberish ≈ 0.15`);
  console.log(`Best score achieved : ${overallBestScore.toFixed(4)}`);
  if (overallBestScore > 0.45) {
    console.log('Result: PROMISING — syllabic mapping produces Latin-like n-gram sequences.');
    console.log('Recommend: send best decode to critic agent for LLM judge evaluation.');
  } else if (overallBestScore > 0.30) {
    console.log('Result: MARGINAL — some n-gram signal but below the critic gate threshold.');
    console.log('Consider: increase N_VOCAB, try phoneme-level mapping, or per-section analysis.');
  } else {
    console.log('Result: FLAT — no n-gram gradient found. Syllabic codebook hypothesis not supported.');
    console.log('Consistent with null hypothesis (Rugg/Timm-Schinner): no codebook in this family.');
  }
}

main().catch((err) => {
  console.error('[syllabic-decoder] fatal:', err);
  process.exit(1);
});

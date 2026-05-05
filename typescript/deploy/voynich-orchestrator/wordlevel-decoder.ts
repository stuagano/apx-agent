/**
 * Word-level codebook decoder.
 *
 * Tests whether each Voynich EVA word type maps to a whole Latin word
 * (not a letter, glyph, or syllable). This is the codebook cipher hypothesis:
 *   EVA word → Latin word
 *
 * Two phases:
 *
 *   CALIBRATION: Take LATIN_CORPUS sentences, replace each unique word with
 *     an opaque integer token, then run SA to recover the original mapping.
 *     Reports whether the search converges and what restart spread looks like
 *     when a ground truth optimum EXISTS. If SA can't recover a known mapping,
 *     the EVA results are uninterpretable.
 *
 *   EVA PHASE: Map top-N EVA word types → N Latin words from LATIN_DICT
 *     via SA hill-climb. Scores decoded text with char-trigram LM.
 *     Reports restart spread as the primary diagnostic.
 *
 * Circular bias mitigation:
 *   Target vocabulary: LATIN_DICT (not words extracted from LATIN_CORPUS)
 *   LM scorer: char trigrams trained on LATIN_CORPUS
 *   These are largely disjoint: the LM learns from sentence-level patterns;
 *   LATIN_DICT adds plant names and medical terms not in the training sentences.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx wordlevel-decoder.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

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
// Language model (char trigrams — same approach as theory-loop.ts)
// ---------------------------------------------------------------------------

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

const LM = buildTrigramModel(LATIN_CORPUS);

function langModelScore(text: string): number {
  const clean = ` ${text.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  if (clean.length < 5) return 0;

  const logSmoothed = Math.log((1) / (LM.total + LM.vocabSize));  // laplace smoothing floor
  let logProbSum = 0;
  let count = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    const freq = LM.ngrams.get(tg) ?? 0;
    logProbSum += freq > 0 ? Math.log((freq + 1) / (LM.total + LM.vocabSize)) : logSmoothed;
    count++;
  }
  if (count === 0) return 0;
  const avgLogProb = logProbSum / count;

  // Calibrated from theory-loop.ts: real Latin ≈ -4.0, random ≈ -7.5
  const REAL_ANCHOR = -4.0;
  const RAND_ANCHOR = -7.5;
  return Math.max(0, Math.min(1, (avgLogProb - RAND_ANCHOR) / (REAL_ANCHOR - RAND_ANCHOR)));
}

// ---------------------------------------------------------------------------
// Latin vocabulary for codebook target
// Deliberately separate from LATIN_CORPUS sentences to break circular bias.
// These are the candidate Latin words a codebook would map EVA tokens to.
// ---------------------------------------------------------------------------

const CODEBOOK_VOCAB: string[] = [
  // Function words
  'et','in','de','ad','cum','per','est','non','ex','ut','ab','hoc','quod','qui','que',
  'sed','aut','vel','si','sunt','nam','enim','ita','nec','ac','pro','sub','ante','post',
  'inter','contra','super','autem','ergo','igitur',
  // Botanical — plant parts
  'herba','radix','folia','folium','flos','flores','semen','semina','cortex','ramus',
  'planta','fructus','succus','oleum','gummi','resina','spina','caulis','bacca','bulbus',
  // Botanical — descriptors
  'viridis','alba','nigra','rubra','flava','purpurea','amara','dulcis','acris',
  'longa','brevis','lata','rotunda','acuta','pilosa','glabra','spinosa','ramosa',
  // Medical — body parts
  'corpus','caput','oculus','oculi','manus','pes','pedes','stomachus','hepar','vulnus',
  'venter','pectus','cutis','sanguis','os','nervus','vena','cor','dens','gula','lingua',
  // Medical — conditions
  'morbus','dolor','febris','tussis','tumor','ulcus','venenum','pestis','lepra',
  'vertigo','paralysis','fluxus',
  // Medical — preparations
  'remedium','potio','emplastrum','pulvis','unguentum','medicina','decoctum','infusum',
  'electuarium','syrupus','pilula','dosis','manipulus',
  // Properties
  'calor','natura','forma','color','odor','sapor','virtus','vis','qualitas',
  'calida','frigida','humida','sicca','temperata',
  // Elements / substances
  'aqua','ignis','terra','sal','mel','vinum','acetum','lac','cera','piper',
  'crocus','cinnamomum',
  // Verbs (infinitive / base)
  'facit','valet','sanat','tollit','purgat','iuvat','crescit','datur','bibitur',
  'nascitur','dicitur','potest','curat','confortat','miscetur','lavatur','sumitur',
  'habet','bibat','sumat','ponat','accipiat','teritur','conteritur',
  // Plant names
  'mandragora','cannabis','papaver','rosa','lilium','salvia','urtica','absinthium',
  'artemisia','plantago','verbena','malva','betonica','ruta','melissa','hedera',
  'mentha','anethum','foeniculum','apium','coriandrum','origanum','thymus',
  'rosmarinus','sambucus','hypericum','gentiana','valeriana','symphytum',
  'cicuta','helleborus','atropa','hyoscyamus','solanum','aloe','myrrha',
  // Numbers / measures
  'unum','duo','tres','quatuor','quinque','sex','libra','uncia','drachma','granum',
  'partem','dimidium',
  // Additional herbal vocab
  'recipe','pista','misce','tere','impone','coque','sume','pone','accipe',
  'primo','secundo','tertio','quarto','gradu','modo','tempore','loco','aquam',
  'calidam','frigidam','partem','partes','mensuram',
];

// ---------------------------------------------------------------------------
// Simulated annealing
// ---------------------------------------------------------------------------

function scoreMapping(codeSeq: number[], mapping: string[]): number {
  const words = codeSeq.map((c) => mapping[c] ?? '???');
  return langModelScore(words.join(' '));
}

interface RestartResult {
  score: number;
  mapping: string[];
}

function saRestart(
  codeSeq: number[],
  vocabSize: number,
  targetWords: string[],
  steps: number,
  seed: number,
): RestartResult {
  // Initial mapping: random permutation of targetWords for each code
  const mapping = [...targetWords];
  // Seeded shuffle (LCG)
  let rng = seed;
  const rand = () => { rng = (rng * 1664525 + 1013904223) & 0xffffffff; return (rng >>> 0) / 0x100000000; };
  for (let i = mapping.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [mapping[i], mapping[j]] = [mapping[j], mapping[i]];
  }

  let currentScore = scoreMapping(codeSeq, mapping);
  let bestScore = currentScore;
  let bestMapping = [...mapping];

  const T0 = 2.0;
  for (let step = 0; step < steps; step++) {
    const T = T0 * (1 - step / steps);
    // Swap two random positions in mapping
    const i = Math.floor(rand() * vocabSize);
    const j = Math.floor(rand() * vocabSize);
    if (i === j) continue;
    [mapping[i], mapping[j]] = [mapping[j], mapping[i]];
    const newScore = scoreMapping(codeSeq, mapping);
    const delta = newScore - currentScore;
    if (delta > 0 || rand() < Math.exp(delta / Math.max(T, 1e-6))) {
      currentScore = newScore;
      if (currentScore > bestScore) { bestScore = currentScore; bestMapping = [...mapping]; }
    } else {
      [mapping[i], mapping[j]] = [mapping[j], mapping[i]];  // revert
    }
  }

  return { score: bestScore, mapping: bestMapping };
}

// ---------------------------------------------------------------------------
// CALIBRATION PHASE
// Tests that SA can recover a known word-level mapping.
// Encodes LATIN_CORPUS by replacing each unique word with an integer token,
// then runs SA to find the reverse mapping.
// ---------------------------------------------------------------------------

function runCalibration(nRestarts: number, steps: number): void {
  console.log('\n=== CALIBRATION PHASE ===');
  console.log('Testing: can SA recover a known Latin word→code mapping?');
  console.log('');

  // Build vocabulary from LATIN_CORPUS
  const corpusWords = LATIN_CORPUS.toLowerCase()
    .replace(/[^a-z\s]/g, ' ')
    .split(/\s+/)
    .filter(Boolean);
  const vocab = [...new Set(corpusWords)].sort();
  const wordToCode = new Map<string, number>(vocab.map((w, i) => [w, i]));
  const codeToWord = vocab;

  // Encode corpus as a sequence of integer codes
  const codeSeq = corpusWords.map((w) => wordToCode.get(w) ?? 0);

  console.log(`  corpus: ${corpusWords.length} tokens, ${vocab.size ?? vocab.length} unique types`);

  // Ground-truth score (original mapping)
  const groundTruthScore = scoreMapping(codeSeq, codeToWord);
  console.log(`  ground-truth score (original mapping): ${groundTruthScore.toFixed(4)}`);

  // Run N restarts
  const results: RestartResult[] = [];
  for (let r = 0; r < nRestarts; r++) {
    const res = saRestart(codeSeq, vocab.length, [...codeToWord], steps, 42 + r * 31337);
    results.push(res);
    process.stdout.write(`  restart ${r + 1}/${nRestarts}: score=${res.score.toFixed(4)}\n`);
  }

  const scores = results.map((r) => r.score);
  const bestScore = Math.max(...scores);
  const worstScore = Math.min(...scores);
  const spread = bestScore - worstScore;

  console.log('');
  console.log(`  Best restart score : ${bestScore.toFixed(4)}`);
  console.log(`  Ground-truth score : ${groundTruthScore.toFixed(4)}`);
  console.log(`  Restart spread     : ${spread.toFixed(4)}  (0.02+ = gradient exists; <0.01 = flat landscape)`);
  console.log('');

  const recovered = bestScore >= groundTruthScore * 0.95;
  if (recovered) {
    console.log('  CALIBRATION PASS: SA approaches ground-truth score. LM gradient is exploitable at word level.');
    console.log('  EVA phase results are interpretable.');
  } else {
    console.log('  CALIBRATION WARN: SA cannot approach ground-truth. Scoring function may not discriminate at word level.');
    console.log('  EVA phase results would be uninterpretable even if the hypothesis is correct.');
  }

  // Show best-found mapping for spot-check
  const best = results.reduce((a, b) => (a.score > b.score ? a : b));
  console.log('');
  console.log('  Spot-check (first 10 code → word assignments in best solution):');
  for (let i = 0; i < Math.min(10, vocab.length); i++) {
    const original = codeToWord[i];
    const found = best.mapping[i];
    const match = original === found ? '✓' : '✗';
    console.log(`    code_${i.toString().padStart(3, '0')}: original=${original.padEnd(12)} found=${found.padEnd(12)} ${match}`);
  }
}

// ---------------------------------------------------------------------------
// EVA PHASE
// Maps top-N EVA word types → Latin words from CODEBOOK_VOCAB via SA.
// ---------------------------------------------------------------------------

async function runEvaPhase(nRestarts: number, steps: number): Promise<void> {
  console.log('\n=== EVA PHASE ===');
  console.log('Hypothesis: each EVA word type maps to one Latin word from CODEBOOK_VOCAB');
  console.log('');

  // Load EVA corpus — top-N word types by frequency
  console.log('Loading EVA corpus...');
  const rows = await executeSql(`
    SELECT eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
    LIMIT 500
  `);

  // Parse EVA words (single-char tokenization to avoid multi-glyph compression)
  const allEvaWords: string[] = [];
  for (const r of rows) {
    if (!r.eva_text) continue;
    const words = r.eva_text
      .replace(/[\r\n]+/g, ' ')
      .split(/[\s.]+/)
      .map((w) => w.trim().toLowerCase())
      .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
    allEvaWords.push(...words);
  }

  // Frequency rank the EVA word types
  const evaFreq = new Map<string, number>();
  for (const w of allEvaWords) evaFreq.set(w, (evaFreq.get(w) ?? 0) + 1);
  const topEvaWords = [...evaFreq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, Math.min(CODEBOOK_VOCAB.length, 200))
    .map(([w]) => w);

  const N = topEvaWords.length;
  const evaToCode = new Map<string, number>(topEvaWords.map((w, i) => [w, i]));

  console.log(`  EVA corpus: ${allEvaWords.length} tokens, ${evaFreq.size} unique types`);
  console.log(`  Mapping top-${N} EVA types → ${CODEBOOK_VOCAB.slice(0, N).length} Latin words`);
  console.log(`  Top-10 EVA words by freq: ${topEvaWords.slice(0, 10).join(', ')}`);
  console.log('');

  // Encode EVA corpus as code sequence
  const codeSeq = allEvaWords
    .map((w) => evaToCode.get(w))
    .filter((c): c is number => c !== undefined);

  // Candidate Latin words: first N from CODEBOOK_VOCAB
  const candidateLatinWords = CODEBOOK_VOCAB.slice(0, N);

  console.log(`  Encoded sequence length: ${codeSeq.length} tokens`);
  console.log(`  Running ${nRestarts} SA restarts × ${steps} steps each...`);
  console.log('');

  const results: RestartResult[] = [];
  for (let r = 0; r < nRestarts; r++) {
    const res = saRestart(codeSeq, N, [...candidateLatinWords], steps, 12345 + r * 99991);
    results.push(res);
    process.stdout.write(`  restart ${r + 1}/${nRestarts}: score=${res.score.toFixed(4)}\n`);
  }

  const scores = results.map((r) => r.score);
  const bestScore = Math.max(...scores);
  const worstScore = Math.min(...scores);
  const spread = bestScore - worstScore;
  const avgScore = scores.reduce((s, x) => s + x, 0) / scores.length;

  console.log('');
  console.log(`  Best score   : ${bestScore.toFixed(4)}  (calibration: real Latin ≈ 0.65, random ≈ 0.10)`);
  console.log(`  Avg score    : ${avgScore.toFixed(4)}`);
  console.log(`  Worst score  : ${worstScore.toFixed(4)}`);
  console.log(`  Restart spread: ${spread.toFixed(4)}  (0.02+ = gradient; <0.01 = flat landscape = no optimum)`);
  console.log('');

  // Decode a sample of the EVA text with the best mapping
  const best = results.reduce((a, b) => (a.score > b.score ? a : b));
  const sampleEvaWords = allEvaWords.slice(0, 40);
  const decodedSample = sampleEvaWords.map((w) => {
    const code = evaToCode.get(w);
    return code !== undefined ? (best.mapping[code] ?? '?') : w;
  });

  console.log('  Sample decoded text (first 40 EVA tokens → Latin words):');
  console.log(`  EVA    : ${sampleEvaWords.slice(0, 20).join(' ')}`);
  console.log(`  Decoded: ${decodedSample.slice(0, 20).join(' ')}`);
  console.log(`  EVA    : ${sampleEvaWords.slice(20, 40).join(' ')}`);
  console.log(`  Decoded: ${decodedSample.slice(20, 40).join(' ')}`);
  console.log('');

  // Show top-20 EVA→Latin assignments by EVA frequency
  console.log('  Top-20 EVA word assignments (by EVA frequency):');
  console.log('  ' + 'EVA word'.padEnd(16) + 'freq'.padStart(6) + '  →  ' + 'Latin word');
  console.log('  ' + '-'.repeat(50));
  for (let i = 0; i < Math.min(20, topEvaWords.length); i++) {
    const evaWord = topEvaWords[i];
    const freq = evaFreq.get(evaWord) ?? 0;
    const latinWord = best.mapping[evaToCode.get(evaWord) ?? 0] ?? '?';
    console.log(`  ${evaWord.padEnd(16)}${freq.toString().padStart(6)}  →  ${latinWord}`);
  }
  console.log('');

  // Verdict
  console.log('=== VERDICT ===');
  if (spread < 0.005) {
    console.log('  Restart spread < 0.005: FLAT LANDSCAPE. No exploitable gradient in EVA data.');
    console.log('  Same result as transposition (0.001) and syllabic (0.007) decoders.');
    console.log('  Word-level codebook hypothesis: no evidence of structure at this level.');
  } else if (spread < 0.02) {
    console.log('  Restart spread 0.005–0.02: WEAK GRADIENT. Marginal structure; not dispositive.');
  } else {
    console.log('  Restart spread ≥ 0.02: GRADIENT DETECTED. SA is finding non-trivial optima.');
    console.log('  Investigate whether best mapping produces interpretable Latin.');
  }

  if (bestScore > 0.55) {
    console.log(`  Best score ${bestScore.toFixed(3)} > 0.55: high. But check if circular bias inflates this.`);
    console.log('  Compare against shuffled-EVA control to verify the score is real structure.');
  } else if (bestScore > 0.40) {
    console.log(`  Best score ${bestScore.toFixed(3)} in 0.40–0.55: moderate. Decoder found Latin-shaped output.`);
    console.log('  LLM judge needed to determine if decoded text is coherent (not just Latin-shaped).');
  } else {
    console.log(`  Best score ${bestScore.toFixed(3)} < 0.40: below threshold. Decoded text is not Latin-shaped.`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const N_RESTARTS = parseInt(process.env.N_RESTARTS ?? '5');
const SA_STEPS   = parseInt(process.env.SA_STEPS ?? '3000');

async function main(): Promise<void> {
  console.log('wordlevel-decoder — EVA word → Latin word codebook hypothesis');
  console.log(`Config: N_RESTARTS=${N_RESTARTS}, SA_STEPS=${SA_STEPS}`);
  console.log(`LM: ${LM.total} trigrams, ${LM.vocabSize} unique`);
  console.log('');

  runCalibration(N_RESTARTS, SA_STEPS);

  await runEvaPhase(N_RESTARTS, SA_STEPS);
}

main().catch((err) => {
  console.error('[wordlevel-decoder] fatal:', err);
  process.exit(1);
});

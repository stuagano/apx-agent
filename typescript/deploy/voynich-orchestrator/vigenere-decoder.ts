/**
 * Vigenère decoder with key recovery.
 *
 * Tests the hypothesis that the Voynich EVA text is a Vigenère cipher of Latin:
 * each EVA character is shifted by a key character that cycles with period L.
 *
 * Two diagnostic layers:
 *
 *   IC ANALYSIS: Index of Coincidence on each stride-L stream. Real Vigenère
 *     of natural language has IC ≈ 0.065 per stream at the correct key length;
 *     random / Rugg-style text has IC ≈ 0.038 regardless of L. This is a
 *     pre-SA structural test that doesn't require decoding anything.
 *
 *   KEY RECOVERY: For the best key length(s), recover each Caesar shift via
 *     frequency cross-correlation against Latin char frequencies, then refine
 *     the full key with SA. Score decoded text with char-trigram LM. Report
 *     restart spread as the primary diagnostic.
 *
 * EVA alphabet: single-char EVA glyphs mapped to a 21-char alphabet
 * (a-y excluding j,u,v,w,z — characters not present in EVA).
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   npx tsx vigenere-decoder.ts
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
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Alphabets
// ---------------------------------------------------------------------------

// EVA single-char alphabet (no j, u, v, w, z in EVA text)
const EVA_ALPHA = 'abcdefghiklmnopqrstxy';
const ALPHA_SIZE = EVA_ALPHA.length;  // 21

// Latin character frequencies (Beutelspacher 1994, classical Latin)
const LATIN_CHAR_FREQ: Record<string, number> = {
  a:0.074, b:0.009, c:0.045, d:0.038, e:0.124, f:0.007, g:0.015, h:0.009,
  i:0.110, k:0.001, l:0.044, m:0.031, n:0.065, o:0.057, p:0.015, q:0.003,
  r:0.063, s:0.062, t:0.063, x:0.003, y:0.002,
};
// Normalize to the EVA_ALPHA set
const LATIN_FREQ_VEC: number[] = EVA_ALPHA.split('').map((c) => LATIN_CHAR_FREQ[c] ?? 0.001);
const latinTotal = LATIN_FREQ_VEC.reduce((s, x) => s + x, 0);
for (let i = 0; i < LATIN_FREQ_VEC.length; i++) LATIN_FREQ_VEC[i] /= latinTotal;

// ---------------------------------------------------------------------------
// Language model (char trigrams)
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
verbena herba sacra est et habet flores parvos et purpureos contra venenum
absinthium amarum est et tollit vermes et purgat hepar et lien et stomachum
artemisia mater herbarum dicitur et iuvat morbos matricis et menstrua provocat
melissa habet odorem suavem et confortat cor et oculos et virtutes vitales
recipe semen anethi et tere subtiliter et misce cum oleo et vino calido
`;

function buildTrigramModel(corpus: string): { ngrams: Map<string, number>; total: number; vocabSize: number } {
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
  const floor = Math.log(1 / (LM.total + LM.vocabSize));
  let sum = 0; let count = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    const freq = LM.ngrams.get(tg) ?? 0;
    sum += freq > 0 ? Math.log((freq + 1) / (LM.total + LM.vocabSize)) : floor;
    count++;
  }
  if (count === 0) return 0;
  const avg = sum / count;
  return Math.max(0, Math.min(1, (avg - (-7.5)) / (-4.0 - (-7.5))));
}

// ---------------------------------------------------------------------------
// EVA text extraction (single-char)
// ---------------------------------------------------------------------------

function evaToIndices(text: string): number[] {
  return text.toLowerCase().split('').map((c) => EVA_ALPHA.indexOf(c)).filter((i) => i >= 0);
}

function vigenereDecodeIndices(indices: number[], key: number[]): string {
  const L = key.length;
  return indices.map((idx, pos) => EVA_ALPHA[(((idx - key[pos % L]) % ALPHA_SIZE) + ALPHA_SIZE) % ALPHA_SIZE]).join('');
}

// ---------------------------------------------------------------------------
// Index of Coincidence
// ---------------------------------------------------------------------------

function indexOfCoincidence(stream: number[]): number {
  const N = stream.length;
  if (N < 2) return 0;
  const freq = new Array<number>(ALPHA_SIZE).fill(0);
  for (const c of stream) freq[c]++;
  const numer = freq.reduce((s, f) => s + f * (f - 1), 0);
  return numer / (N * (N - 1));
}

function meanIcForKeyLength(indices: number[], L: number): number {
  const streams: number[][] = Array.from({ length: L }, () => []);
  for (let i = 0; i < indices.length; i++) streams[i % L].push(indices[i]);
  const ics = streams.filter((s) => s.length >= 2).map(indexOfCoincidence);
  return ics.reduce((s, x) => s + x, 0) / ics.length;
}

// ---------------------------------------------------------------------------
// Key recovery via frequency cross-correlation
// ---------------------------------------------------------------------------

function frequencyKeyGuess(indices: number[], L: number): number[] {
  const key: number[] = [];
  for (let pos = 0; pos < L; pos++) {
    const stream = indices.filter((_, i) => i % L === pos);
    if (stream.length === 0) { key.push(0); continue; }
    // Build frequency vector for this stream
    const freq = new Array<number>(ALPHA_SIZE).fill(0);
    for (const c of stream) freq[c]++;
    const total = stream.length;
    // Find shift k that maximizes correlation with Latin freq vector
    let bestShift = 0;
    let bestCorr = -Infinity;
    for (let k = 0; k < ALPHA_SIZE; k++) {
      let corr = 0;
      for (let c = 0; c < ALPHA_SIZE; c++) {
        corr += (freq[c] / total) * LATIN_FREQ_VEC[(c - k + ALPHA_SIZE) % ALPHA_SIZE];
      }
      if (corr > bestCorr) { bestCorr = corr; bestShift = k; }
    }
    key.push(bestShift);
  }
  return key;
}

// ---------------------------------------------------------------------------
// SA refinement of key
// ---------------------------------------------------------------------------

function saKeyRefine(indices: number[], initialKey: number[], steps: number, seed: number): { key: number[]; score: number } {
  const L = initialKey.length;
  let key = [...initialKey];
  let score = langModelScore(vigenereDecodeIndices(indices.slice(0, 2000), key));
  let bestKey = [...key];
  let bestScore = score;

  let rng = seed;
  const rand = () => { rng = (rng * 1664525 + 1013904223) & 0xffffffff; return (rng >>> 0) / 0x100000000; };

  const T0 = 1.5;
  for (let step = 0; step < steps; step++) {
    const T = T0 * (1 - step / steps);
    // Mutation: nudge one key position by ±1 or ±2
    const pos = Math.floor(rand() * L);
    const delta = Math.floor(rand() * 5) - 2;  // -2..+2
    if (delta === 0) continue;
    const oldVal = key[pos];
    key[pos] = ((oldVal + delta) % ALPHA_SIZE + ALPHA_SIZE) % ALPHA_SIZE;

    const newScore = langModelScore(vigenereDecodeIndices(indices.slice(0, 2000), key));
    const dScore = newScore - score;
    if (dScore > 0 || rand() < Math.exp(dScore / Math.max(T, 1e-6))) {
      score = newScore;
      if (score > bestScore) { bestScore = score; bestKey = [...key]; }
    } else {
      key[pos] = oldVal;  // revert
    }
  }
  return { key: bestKey, score: bestScore };
}

// ---------------------------------------------------------------------------
// Critic harness caller
// ---------------------------------------------------------------------------

async function callCriticTool(criticUrl: string, toolName: string, params: Record<string, unknown>): Promise<unknown | null> {
  const url = `${criticUrl.replace(/\/$/, '')}/api/agent/tools/${toolName}`;
  let token = '';
  try { token = await resolveToken(); } catch { /* ok */ }
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60_000);
    let res: globalThis.Response;
    try {
      res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(params), signal: controller.signal });
    } finally { clearTimeout(timer); }
    if (!res.ok) { console.warn(`  [critic/${toolName}] http ${res.status}`); return null; }
    return await res.json();
  } catch (err) {
    console.warn(`  [critic/${toolName}] threw: ${(err as Error).message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const N_RESTARTS = parseInt(process.env.N_RESTARTS ?? '5');
const SA_STEPS   = parseInt(process.env.SA_STEPS ?? '2000');
const MAX_KEY_LEN = parseInt(process.env.MAX_KEY_LEN ?? '12');

async function main(): Promise<void> {
  const criticUrl = process.env.CRITIC_AGENT_URL ?? '';
  console.log('vigenere-decoder — keyed substitution cipher test');
  console.log(`Config: N_RESTARTS=${N_RESTARTS}, SA_STEPS=${SA_STEPS}, MAX_KEY_LEN=${MAX_KEY_LEN}`);
  console.log(`EVA alphabet: "${EVA_ALPHA}" (${ALPHA_SIZE} chars)`);
  console.log('');

  // Load EVA corpus
  console.log('Loading EVA corpus (herbal section)...');
  const rows = await executeSql(`
    SELECT eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal' LIMIT 500
  `);
  const rawText = rows.map((r) => r.eva_text ?? '').join(' ').toLowerCase().replace(/[\r\n.]+/g, ' ');
  const evaIndices = evaToIndices(rawText);
  console.log(`  ${evaIndices.length} EVA character indices extracted`);
  console.log('');

  // ---------------------------------------------------------------------------
  // Phase 1: IC analysis across key lengths 2..MAX_KEY_LEN
  // ---------------------------------------------------------------------------
  console.log('=== PHASE 1: Index of Coincidence key-length search ===');
  console.log('  (IC_natural ≈ 0.065, IC_random ≈ 0.038)');
  console.log('');
  console.log('  ' + 'key_len'.padEnd(10) + 'mean_IC'.padStart(10) + '  signal');

  let bestL = 2;
  let bestIC = -Infinity;
  const icByLen: Array<{ L: number; ic: number }> = [];

  for (let L = 2; L <= MAX_KEY_LEN; L++) {
    const ic = meanIcForKeyLength(evaIndices, L);
    icByLen.push({ L, ic });
    if (ic > bestIC) { bestIC = ic; bestL = L; }
    const bar = '█'.repeat(Math.round((ic - 0.035) / 0.003));
    const signal = ic > 0.055 ? 'STRONG' : ic > 0.045 ? 'moderate' : 'weak';
    console.log(`  L=${L.toString().padEnd(9)} ${ic.toFixed(5).padStart(10)}  ${bar} ${signal}`);
  }

  console.log('');
  console.log(`  Best key length by IC: L=${bestL} (IC=${bestIC.toFixed(5)})`);

  const IC_RANDOM = 1 / ALPHA_SIZE;  // ≈ 0.0476 for 21-char alphabet
  const IC_NATURAL = 0.065;
  if (bestIC < IC_RANDOM + 0.005) {
    console.log('  IC VERDICT: No key-length signal. EVA IC is consistent with random text at all L.');
    console.log('  Vigenère of natural language should show IC ≥ 0.050 at the correct L.');
  } else if (bestIC < IC_NATURAL - 0.010) {
    console.log('  IC VERDICT: Weak signal. IC elevated above random but below natural language.');
    console.log('  Could indicate partial structure or wrong alphabet size assumption.');
  } else {
    console.log('  IC VERDICT: STRONG signal. IC at this key length is consistent with Vigenère of natural language.');
  }

  // Also check calibration: known Latin text should show high IC at L=1
  const latinIndices = evaToIndices(LATIN_CORPUS.replace(/[^a-z]/g, ''));
  const latinIC = indexOfCoincidence(latinIndices);
  console.log(`\n  Calibration: IC of LATIN_CORPUS chars = ${latinIC.toFixed(5)} (expect ≈ 0.065)`);

  // ---------------------------------------------------------------------------
  // Phase 2: Key recovery and SA refinement at best L (and runner-up)
  // ---------------------------------------------------------------------------
  const candidateLens = icByLen.sort((a, b) => b.ic - a.ic).slice(0, 3).map((x) => x.L);
  console.log(`\n=== PHASE 2: Key recovery at L=${candidateLens.join(', ')} ===`);

  interface LenResult { L: number; scores: number[]; bestKey: number[]; bestScore: number }
  const lenResults: LenResult[] = [];

  for (const L of candidateLens) {
    console.log(`\n  Key length L=${L}:`);
    // Frequency-analysis initial guess
    const freqKey = frequencyKeyGuess(evaIndices, L);
    const freqScore = langModelScore(vigenereDecodeIndices(evaIndices.slice(0, 2000), freqKey));
    console.log(`    freq-analysis key: [${freqKey.join(',')}]`);
    console.log(`    freq-analysis score: ${freqScore.toFixed(4)}`);

    // N restarts with SA refinement — each starting from freq key + random perturbation
    const scores: number[] = [];
    let overallBestKey = [...freqKey];
    let overallBestScore = freqScore;

    for (let r = 0; r < N_RESTARTS; r++) {
      // Perturb the freq key for each restart (except restart 0)
      let startKey = [...freqKey];
      if (r > 0) {
        let rng = 77777 + r * 131071;
        const rand = () => { rng = (rng * 1664525 + 1013904223) & 0xffffffff; return (rng >>> 0) / 0x100000000; };
        startKey = startKey.map((k) => ((k + Math.floor(rand() * ALPHA_SIZE)) % ALPHA_SIZE));
      }
      const { key, score } = saKeyRefine(evaIndices, startKey, SA_STEPS, 42 + r * 13337 + L * 777);
      scores.push(score);
      if (score > overallBestScore) { overallBestScore = score; overallBestKey = [...key]; }
      process.stdout.write(`    restart ${r + 1}/${N_RESTARTS}: score=${score.toFixed(4)}\n`);
    }

    const spread = Math.max(...scores) - Math.min(...scores);
    console.log(`    spread: ${spread.toFixed(4)}  avg: ${(scores.reduce((s,x) => s+x,0)/scores.length).toFixed(4)}  best: ${overallBestScore.toFixed(4)}`);
    lenResults.push({ L, scores, bestKey: overallBestKey, bestScore: overallBestScore });
  }

  // ---------------------------------------------------------------------------
  // Phase 3: Decode with best overall key and show sample
  // ---------------------------------------------------------------------------
  const globalBest = lenResults.reduce((a, b) => a.bestScore > b.bestScore ? a : b);
  console.log(`\n=== PHASE 3: Best decoding (L=${globalBest.L}, score=${globalBest.bestScore.toFixed(4)}) ===`);
  console.log(`  Key: [${globalBest.bestKey.join(',')}]  →  "${globalBest.bestKey.map((k) => EVA_ALPHA[k]).join('')}"`);

  const fullDecoded = vigenereDecodeIndices(evaIndices.slice(0, 500), globalBest.bestKey);
  const decodedWithSpaces = fullDecoded.match(/.{1,6}/g)?.join(' ') ?? fullDecoded;  // rough word segmentation
  console.log(`\n  Decoded sample (first 120 chars, 6-char segments):`);
  console.log(`  ${decodedWithSpaces.slice(0, 240)}`);

  // ---------------------------------------------------------------------------
  // Restart spread summary
  // ---------------------------------------------------------------------------
  console.log('\n=== RESTART SPREAD SUMMARY ===');
  console.log('  (Reference: substitution=0.02-0.08, transposition=0.001, syllabic=0.007)');
  for (const r of lenResults) {
    const spread = Math.max(...r.scores) - Math.min(...r.scores);
    console.log(`  L=${r.L}: spread=${spread.toFixed(4)}, best=${r.bestScore.toFixed(4)}`);
  }

  // ---------------------------------------------------------------------------
  // Critic harness (if best score is at all meaningful)
  // ---------------------------------------------------------------------------
  if (criticUrl && globalBest.bestScore >= 0.25) {
    console.log('\n=== CRITIC HARNESS ===');
    // Use full decoded text with space insertion at original EVA word boundaries
    // (re-decode with word boundaries preserved from original)
    const rawWords = rawText.split(/\s+/).filter(Boolean);
    let charOffset = 0;
    const decodedWords: string[] = [];
    for (const w of rawWords.slice(0, 300)) {
      const wIndices = evaToIndices(w);
      if (wIndices.length > 0) {
        const decoded = vigenereDecodeIndices(wIndices.map((_, i) => evaIndices[charOffset + i] ?? 0), globalBest.bestKey.map((k, i) => globalBest.bestKey[(charOffset + i) % globalBest.L]));
        decodedWords.push(decoded);
        charOffset += wIndices.length;
      }
    }
    const decodedText = decodedWords.join(' ');

    const [likRaw, nullRaw] = await Promise.all([
      callCriticTool(criticUrl, 'score_latin_likelihood', { decoded_text: decodedText, source_language: 'latin' }),
      callCriticTool(criticUrl, 'null_baseline_test', { decoded_text: decodedText, source_language: 'latin' }),
    ]);
    const lik = (likRaw as { likelihood?: number } | null)?.likelihood ?? null;
    const nullDist = (nullRaw as { distinguishable?: boolean } | null)?.distinguishable ?? null;
    console.log(`  likelihood=${lik?.toFixed(3) ?? 'null'}  null_dist=${nullDist}`);

    if ((lik ?? 0) >= 0.3 && nullDist === true) {
      console.log('  Heuristic gate cleared — running llm_judge...');
      const judgeRaw = await callCriticTool(criticUrl, 'llm_judge', { decoded_text: decodedText, source_language: 'latin' });
      const judge = judgeRaw as { verdict?: string; reason?: string } | null;
      console.log(`  judge verdict: ${judge?.verdict ?? 'null'}`);
      if (judge?.reason) console.log(`  judge reason: ${judge.reason.slice(0, 200)}`);
    } else {
      console.log('  Below heuristic gate — llm_judge skipped.');
    }
  } else if (!criticUrl) {
    console.log('\n(Set CRITIC_AGENT_URL to run full harness on best decoded text.)');
  }

  // ---------------------------------------------------------------------------
  // Final verdict
  // ---------------------------------------------------------------------------
  console.log('\n=== VERDICT ===');

  // Check whether IC is effectively constant across all key lengths
  const icValues = icByLen.map((x) => x.ic);
  const icMin = Math.min(...icValues);
  const icMax = Math.max(...icValues);
  const icRange = icMax - icMin;

  if (icRange < 0.002) {
    console.log(`  IC FLAT across all L (range ${icRange.toFixed(5)}): this is NOT a Vigenère signal.`);
    console.log('');
    console.log('  In a real Vigenère cipher, IC is HIGH at the correct key length (each stride-L');
    console.log('  stream is a plain Caesar shift → IC ≈ natural language ≈ 0.065) and LOWER at');
    console.log('  wrong lengths (mixed streams → IC ≈ random ≈ 0.048). Flat IC means no period.');
    console.log('');
    console.log(`  EVA IC = ${bestIC.toFixed(5)} > IC_random (${IC_RANDOM.toFixed(5)}) and > IC_Latin (≈0.065).`);
    console.log('  EVA is MORE concentrated than natural language at every stride — a property');
    console.log('  consistent with generation from a constrained glyph table (Rugg cardan grille),');
    console.log('  not Vigenère encryption of real language.');
  } else if (bestIC < IC_RANDOM + 0.005) {
    console.log('  IC is at random level across all L. No key-length signal. Not Vigenère.');
  } else {
    console.log(`  IC peak at L=${bestL} (range ${icRange.toFixed(5)}). Possible Vigenère signal.`);
    console.log('  Check decoded text quality to determine if the peak is meaningful.');
  }

  console.log('');
  if (globalBest.bestScore < 0.35) {
    console.log(`  Decoded text score ${globalBest.bestScore.toFixed(3)} < 0.35: not Latin-shaped.`);
    console.log('  Vigenère shifts within EVA_ALPHA do not produce Latin trigram sequences.');
    console.log('  FALSIFIED: Vigenère of Latin (any key length 2–12) is not supported.');
  } else if (globalBest.bestScore < 0.55) {
    console.log(`  Decoded text score ${globalBest.bestScore.toFixed(3)}: moderate. Check LLM judge.');
  } else {
    console.log(`  Decoded text score ${globalBest.bestScore.toFixed(3)}: HIGH. Warrants close inspection.`);
  }
}

main().catch((err) => {
  console.error('[vigenere-decoder] fatal:', err);
  process.exit(1);
});

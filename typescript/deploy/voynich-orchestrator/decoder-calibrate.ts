/**
 * Decoder calibration test — does the hill-climb actually work on a known-easy
 * substitution problem?
 *
 * Pipeline:
 *   1. Take a Latin sentence from LATIN_CORPUS.
 *   2. Encode it with a random a-z permutation (pure substitution cipher).
 *   3. Hill-climb back from a DIFFERENT random permutation, scoring with the
 *      orchestrator's actual hillClimbScore('latin').
 *   4. Report: did the search recover real Latin, and how close did its score
 *      get to the ceiling (original Latin's hillClimbScore)?
 *
 * Interpretation:
 *   - char_accuracy > 0.85, score_ratio > 0.85 → search works on easy
 *     instances. Pivot focus to alternative cipher hypotheses.
 *   - score_ratio > 0.85 but char_accuracy < 0.5 → scorer has degenerate
 *     local optima; search reaches "good" scores via wrong maps.
 *   - score_ratio < 0.5 → search itself is broken; fix optimizer before
 *     adding strategies.
 *
 * Run from this dir:
 *   export DATABRICKS_TOKEN=... DATABRICKS_HOST=... DATABRICKS_WAREHOUSE_ID=...
 *   npx tsx decoder-calibrate.ts
 *
 * (DATABRICKS_* aren't strictly used here, but theory-loop.ts's module-level
 * SQL helper boots and would fail without them. Cheap workaround: dummy values.)
 */

import { hillClimbScore } from './theory-loop.js';

// ---------------------------------------------------------------------------
// Sample
// ---------------------------------------------------------------------------

const LATIN_SAMPLES = [
  'herba haec nascitur in locis humidis et umbrosis radix eius est longa et alba',
  'recipe radicem mandragorae et folia eius pista et misce cum aqua frigida',
  'folium est latum et pilosum flores sunt purpurei semen est nigrum et acutum',
  'herba haec sanat morbos stomachi et iuvat digestionem cum bibitur in vino',
  'calida et sicca est in primo gradu valet contra dolorem capitis et oculorum',
];

// ---------------------------------------------------------------------------
// Cipher: random a-z permutation
// ---------------------------------------------------------------------------

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz';

function shuffleString(s: string): string {
  const arr = s.split('');
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr.join('');
}

/** Map letter (a-z) → cipher letter (also a-z). */
function randomPermutationMap(): Record<string, string> {
  const shuffled = shuffleString(ALPHABET);
  const map: Record<string, string> = {};
  for (let i = 0; i < ALPHABET.length; i++) {
    map[ALPHABET[i]] = shuffled[i];
  }
  return map;
}

function applyMap(text: string, map: Record<string, string>): string {
  let out = '';
  for (const ch of text.toLowerCase()) {
    out += map[ch] ?? ch;
  }
  return out;
}

function invertMap(m: Record<string, string>): Record<string, string> {
  const inv: Record<string, string> = {};
  for (const [k, v] of Object.entries(m)) inv[v] = k;
  return inv;
}

// ---------------------------------------------------------------------------
// Hill-climb (simulated annealing, swap-two mutation)
// ---------------------------------------------------------------------------

interface HillClimbResult {
  bestMap: Record<string, string>;
  bestScore: number;
  bestDecoded: string;
  improvements: number;
}

function hillClimb(
  encoded: string,
  steps: number,
  language: string,
): HillClimbResult {
  // Start from a random map (cipher letter → plaintext letter)
  let current = randomPermutationMap();
  let currentDecoded = applyMap(encoded, current);
  let currentScore = hillClimbScore(currentDecoded, language);
  let best = current;
  let bestScore = currentScore;
  let bestDecoded = currentDecoded;
  let improvements = 0;

  for (let step = 0; step < steps; step++) {
    // Mutate: swap two random char mappings
    const candidate = { ...current };
    const keys = Object.keys(candidate);
    const i = Math.floor(Math.random() * keys.length);
    let j = Math.floor(Math.random() * keys.length);
    while (j === i) j = Math.floor(Math.random() * keys.length);
    const a = keys[i], b = keys[j];
    [candidate[a], candidate[b]] = [candidate[b], candidate[a]];

    const decoded = applyMap(encoded, candidate);
    const score = hillClimbScore(decoded, language);

    // SA: cooling temp from 0.05 → 0.001
    const t = 0.05 - (0.049 * step) / steps;
    const delta = score - currentScore;
    const accept = delta > 0 || Math.random() < Math.exp(delta / Math.max(t, 1e-6));

    if (accept) {
      current = candidate;
      currentScore = score;
      currentDecoded = decoded;
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
        bestDecoded = decoded;
        improvements++;
      }
    }
  }

  return { bestMap: best, bestScore, bestDecoded, improvements };
}

// ---------------------------------------------------------------------------
// Recovery metrics
// ---------------------------------------------------------------------------

function charAccuracy(recovered: string, original: string): number {
  const len = Math.min(recovered.length, original.length);
  if (len === 0) return 0;
  let match = 0;
  for (let i = 0; i < len; i++) {
    if (recovered[i] === original[i]) match++;
  }
  return match / len;
}

function wordOverlap(recovered: string, original: string): number {
  const origWords = new Set(original.split(/\s+/).filter((w) => w.length >= 3));
  const recWords = recovered.split(/\s+/).filter((w) => w.length >= 3);
  if (origWords.size === 0 || recWords.length === 0) return 0;
  const matches = recWords.filter((w) => origWords.has(w)).length;
  return matches / Math.max(recWords.length, origWords.size);
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

const STEPS = 8000;       // matches the orchestrator's verbose SA budget
const N_TRIALS = 3;       // 3 runs per sample to average out RNG noise

console.log(`Decoder calibration — substitution-cipher recovery test`);
console.log(`steps=${STEPS} per run, ${N_TRIALS} trials per sample, ${LATIN_SAMPLES.length} samples`);
console.log('');

const allResults: Array<{ sample: number; trial: number; score_ratio: number; char_acc: number; word_overlap: number }> = [];

for (let s = 0; s < LATIN_SAMPLES.length; s++) {
  const original = LATIN_SAMPLES[s];
  const ceiling = hillClimbScore(original, 'latin');

  for (let t = 0; t < N_TRIALS; t++) {
    const encodeMap = randomPermutationMap();
    const encoded = applyMap(original, encodeMap);

    const start = Date.now();
    const result = hillClimb(encoded, STEPS, 'latin');
    const elapsed = (Date.now() - start) / 1000;

    const scoreRatio = ceiling > 0 ? result.bestScore / ceiling : 0;
    const charAcc = charAccuracy(result.bestDecoded, original);
    const wordOlap = wordOverlap(result.bestDecoded, original);

    allResults.push({ sample: s, trial: t, score_ratio: scoreRatio, char_acc: charAcc, word_overlap: wordOlap });

    console.log(`sample ${s} trial ${t} (${elapsed.toFixed(1)}s, ${result.improvements} improvements):`);
    console.log(`  ceiling=${ceiling.toFixed(3)} found=${result.bestScore.toFixed(3)} ratio=${scoreRatio.toFixed(2)}`);
    console.log(`  char_acc=${charAcc.toFixed(2)} word_overlap=${wordOlap.toFixed(2)}`);
    console.log(`  recovered: ${result.bestDecoded.slice(0, 80)}`);
    console.log(`  original : ${original.slice(0, 80)}`);
    console.log('');
  }
}

// ---------------------------------------------------------------------------
// Verdict — bimodal-aware: count per-trial successes, not averages.
// A bimodal distribution (some trials nail it, others fail) means the
// optimizer has the right gradient but a narrow basin of attraction;
// "average ratio" hides this entirely.
// ---------------------------------------------------------------------------

const SUCCESS_RATIO = 0.85;
const SUCCESS_CHAR = 0.85;

const successes = allResults.filter(
  (r) => r.score_ratio >= SUCCESS_RATIO && r.char_acc >= SUCCESS_CHAR,
).length;
const successRate = successes / allResults.length;

const samplesWithAtLeastOneSuccess = new Set(
  allResults.filter((r) => r.score_ratio >= SUCCESS_RATIO && r.char_acc >= SUCCESS_CHAR).map((r) => r.sample),
).size;

const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;

console.log('=== Decoder calibration summary ===');
console.log(`per-trial success rate: ${(successRate * 100).toFixed(0)}% (${successes}/${allResults.length} trials cleared score_ratio >= ${SUCCESS_RATIO} AND char_acc >= ${SUCCESS_CHAR})`);
console.log(`samples with >=1 success: ${samplesWithAtLeastOneSuccess}/${LATIN_SAMPLES.length}`);
console.log(`(also reporting averages, but they hide the bimodal distribution:)`);
console.log(`  avg score_ratio: ${avg(allResults.map((r) => r.score_ratio)).toFixed(2)}`);
console.log(`  avg char_acc:    ${avg(allResults.map((r) => r.char_acc)).toFixed(2)}`);
console.log('');

// Implied N-restart success: 1 - (1 - p)^N
const p = successRate;
console.log('Multi-restart projection (assumes per-trial successes are independent):');
for (const n of [1, 3, 5, 10, 20]) {
  const composite = 1 - Math.pow(1 - p, n);
  console.log(`  N=${n}: ${(composite * 100).toFixed(0)}% chance of >=1 success per round`);
}
console.log('');

if (successRate >= 0.5) {
  console.log('VERDICT: search reliably finds the global optimum from cold starts.');
  console.log('  Implication: EVA-as-substitution-of-Latin is probably wrong if no real EVA decoding has surfaced.');
  console.log('  Next step: alternative cipher families or tokenization.');
} else if (successRate > 0) {
  console.log(`VERDICT: bimodal — search finds the global optimum on ${(successRate * 100).toFixed(0)}% of cold starts; the rest get stuck.`);
  console.log('  Implication: the optimizer has the right gradient but a narrow basin of attraction.');
  console.log('  Next step: multi-restart (N=5-10) per round in the orchestrator. Cheap multiplier on success rate.');
} else {
  console.log('VERDICT: search never reaches the global optimum on any trial.');
  console.log('  Implication: scorer is the wrong gradient, or SA is misconfigured.');
  console.log('  Next step: investigate scorer landscape before adding compute.');
}

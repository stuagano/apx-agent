/**
 * Calibration check for the critic's composite likelihood + null-baseline.
 *
 * Runs three samples through compositeLikelihood:
 *   1. Real medieval Latin (botanical, drawn from theory-loop's LATIN_CORPUS)
 *   2. Scrambled Latin (same chars, redistributed across word boundaries)
 *   3. Random EVA-style gibberish (high vowel density, no Latin structure)
 *
 * Asserts that the composite likelihood is monotone:
 *   real_latin > scrambled_latin > random_eva
 *
 * If this ordering breaks, the scoring is theater — fail loudly so the next
 * change to the scorer surfaces a regression. Run with:
 *
 *   cd typescript && npx tsx examples/voynich/critic/calibrate.ts
 *
 * Used as a manual sanity check before merging changes to the critic, and as
 * a cheap regression guard. NOT exercised in the framework's CI test suite
 * because the random shuffle introduces small variance — see the loop count
 * note below for how we keep the assertion stable.
 */

import { compositeLikelihood } from './scoring.js';

// ---------------------------------------------------------------------------
// Samples
// ---------------------------------------------------------------------------

const REAL_LATIN = [
  'herba haec nascitur in locis humidis et umbrosis radix eius est longa et alba',
  'recipe radicem mandragorae et folia eius pista et misce cum aqua frigida',
  'folium est latum et pilosum flores sunt purpurei semen est nigrum et acutum',
  'herba haec sanat morbos stomachi et iuvat digestionem cum bibitur in vino',
  'calida et sicca est in primo gradu valet contra dolorem capitis et oculorum',
].join(' ');

// Scrambled: same characters as REAL_LATIN, but pooled and redistributed at
// the original word boundaries — destroys per-word structure while preserving
// length distribution and total character frequencies.
function scrambleAcrossText(text: string): string {
  const words = text.toLowerCase().replace(/[^a-z\s]/g, '').split(/\s+/).filter(Boolean);
  const allChars = words.join('').split('');
  for (let i = allChars.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [allChars[i], allChars[j]] = [allChars[j], allChars[i]];
  }
  const out: string[] = [];
  let cursor = 0;
  for (const w of words) {
    out.push(allChars.slice(cursor, cursor + w.length).join(''));
    cursor += w.length;
  }
  return out.join(' ');
}

// Random EVA-shaped text: high vowel density, short tokens (3-5 chars),
// avoids Latin endings by construction.
function randomEvaLike(targetWords: number): string {
  const consonants = 'bcdfghjklmnpqrstvwxyz';
  const vowels = 'aeiouy';
  const out: string[] = [];
  for (let i = 0; i < targetWords; i++) {
    const len = 3 + Math.floor(Math.random() * 3);
    let w = '';
    for (let j = 0; j < len; j++) {
      w += j % 2 === 0
        ? consonants[Math.floor(Math.random() * consonants.length)]
        : vowels[Math.floor(Math.random() * vowels.length)];
    }
    out.push(w);
  }
  return out.join(' ');
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

// Stabilize against shuffle variance: average over a small number of trials
// for the random samples. Real Latin is deterministic.
const N_TRIALS = 5;

function avgComposite(textGen: () => string): number {
  let sum = 0;
  for (let i = 0; i < N_TRIALS; i++) {
    sum += compositeLikelihood(textGen()).likelihood;
  }
  return sum / N_TRIALS;
}

const realScore = compositeLikelihood(REAL_LATIN).likelihood;
const realBreakdown = compositeLikelihood(REAL_LATIN);
const scrambledScore = avgComposite(() => scrambleAcrossText(REAL_LATIN));
const randomScore = avgComposite(() => randomEvaLike(40));

// ---------------------------------------------------------------------------
// Report + assertions
// ---------------------------------------------------------------------------

function fmt(n: number): string {
  return n.toFixed(3).padStart(6);
}

console.log('Critic calibration — composite likelihood across signal/noise samples');
console.log('======================================================================');
console.log(`  REAL Latin      : ${fmt(realScore)}  (bigram=${fmt(realBreakdown.bigram_similarity)}, morph=${fmt(realBreakdown.morphological_score)}, wordLen=${fmt(realBreakdown.word_length_score)})`);
console.log(`  Scrambled Latin : ${fmt(scrambledScore)}  (avg of ${N_TRIALS} trials)`);
console.log(`  Random EVA-like : ${fmt(randomScore)}  (avg of ${N_TRIALS} trials)`);
console.log('');

const orderingOK = realScore > scrambledScore && scrambledScore > randomScore;
const margin1 = realScore - scrambledScore;
const margin2 = scrambledScore - randomScore;

console.log(`  real - scrambled = ${fmt(margin1)}`);
console.log(`  scrambled - random = ${fmt(margin2)}`);
console.log('');

if (!orderingOK) {
  console.error('FAIL: ordering invariant broken — real_latin > scrambled_latin > random_eva');
  console.error('      The composite likelihood is not separating signal from noise.');
  process.exit(1);
}

// Require at least 0.05 separation between adjacent classes to call this useful
const MIN_MARGIN = 0.05;
if (margin1 < MIN_MARGIN || margin2 < MIN_MARGIN) {
  console.error(`FAIL: ordering holds but margin < ${MIN_MARGIN} — too noisy to act on`);
  process.exit(1);
}

console.log('PASS: composite likelihood separates signal from noise with margin >= 0.05');

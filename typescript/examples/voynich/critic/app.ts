/**
 * Voynich Critic — adversarial falsifier agent.
 *
 * Exposes a single `find_contradictions` tool that attempts to falsify decoded
 * Voynich manuscript text using three independent checks:
 *
 *   1. Antonym proximity  — contradictory word pairs found within 15 words
 *   2. Anachronism check  — POST_RENAISSANCE_CONCEPTS present in the text
 *   3. Character frequency — any single character accounting for > 25% of text
 *
 * Verdict: FALSIFIED if adversarial score < 0.5, otherwise SURVIVED.
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   npx tsx app.ts
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
} from '../../../src/index.js';
import { POST_RENAISSANCE_CONCEPTS } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Latin n-gram reference frequencies (top bigrams from medieval Latin corpus)
// Used to score whether decoded text has Latin-like character distribution.
// ---------------------------------------------------------------------------

const LATIN_BIGRAM_FREQ: Record<string, number> = {
  'um': 0.032, 'us': 0.029, 'is': 0.027, 'er': 0.026, 'in': 0.025,
  'it': 0.024, 'es': 0.023, 'am': 0.022, 'em': 0.021, 'at': 0.020,
  'en': 0.019, 'an': 0.019, 're': 0.018, 'nt': 0.018, 'ti': 0.017,
  'ra': 0.016, 'ur': 0.016, 'tu': 0.015, 'ta': 0.015, 'ae': 0.014,
  'et': 0.014, 'ar': 0.013, 'al': 0.013, 'de': 0.013, 'te': 0.012,
  'or': 0.012, 'ri': 0.012, 'li': 0.011, 'ro': 0.011, 'ni': 0.011,
  'co': 0.010, 'on': 0.010, 'ab': 0.010, 'la': 0.010, 'di': 0.010,
};

// Common Latin word endings for morphological check
const LATIN_ENDINGS = [
  'us', 'um', 'is', 'am', 'em', 'as', 'es', 'os', 'ae', 'arum',
  'orum', 'ibus', 'orum', 'ium', 'ens', 'ans', 'unt', 'unt',
  'tur', 'atur', 'ere', 'ire', 'are', 'alis', 'ilis', 'inus',
  'atus', 'itus', 'osis', 'onis', 'tio', 'tas', 'men', 'ment',
];

// ---------------------------------------------------------------------------
// Antonym pairs — if both terms appear within 15 words of each other the text
// is internally contradictory.
// ---------------------------------------------------------------------------

const ANTONYM_PAIRS: [string, string][] = [
  ['hot', 'cold'],
  ['dry', 'wet'],
  ['bitter', 'sweet'],
  ['cure', 'cause'],
  ['poison', 'remedy'],
  ['visible', 'invisible'],
];

// ---------------------------------------------------------------------------
// Helper: tokenise text into lowercase words, preserving index position
// ---------------------------------------------------------------------------

function tokenise(text: string): string[] {
  return text.toLowerCase().match(/[a-z]+/g) ?? [];
}

// ---------------------------------------------------------------------------
// Check 1: antonym proximity
// ---------------------------------------------------------------------------

interface Contradiction {
  type: 'antonym_proximity' | 'anachronism' | 'character_distribution';
  detail: string;
  confidence: number;
}

function checkAntonymProximity(words: string[]): Contradiction[] {
  const results: Contradiction[] = [];

  for (const [a, b] of ANTONYM_PAIRS) {
    const indicesA = words.reduce<number[]>((acc, w, i) => (w === a ? [...acc, i] : acc), []);
    const indicesB = words.reduce<number[]>((acc, w, i) => (w === b ? [...acc, i] : acc), []);

    for (const ia of indicesA) {
      for (const ib of indicesB) {
        if (Math.abs(ia - ib) <= 15) {
          results.push({
            type: 'antonym_proximity',
            detail: `"${a}" (pos ${ia}) and "${b}" (pos ${ib}) appear within ${Math.abs(ia - ib)} words`,
            confidence: 0.75,
          });
          // One hit per pair is sufficient
          break;
        }
      }
      if (results.some((r) => r.detail.startsWith(`"${a}"`))) break;
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Check 2: anachronisms
// ---------------------------------------------------------------------------

function checkAnachronisms(text: string): Contradiction[] {
  const lower = text.toLowerCase();
  const results: Contradiction[] = [];

  for (const concept of POST_RENAISSANCE_CONCEPTS) {
    if (lower.includes(concept.toLowerCase())) {
      results.push({
        type: 'anachronism',
        detail: `Post-Renaissance concept "${concept}" found in decoded text`,
        confidence: 0.9,
      });
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Check 3: character frequency anomaly
// ---------------------------------------------------------------------------

function checkCharDistribution(text: string): Contradiction[] {
  const chars = text.toLowerCase().replace(/[^a-z]/g, '');
  if (chars.length === 0) return [];

  const freq: Record<string, number> = {};
  for (const ch of chars) {
    freq[ch] = (freq[ch] ?? 0) + 1;
  }

  const results: Contradiction[] = [];
  for (const [ch, count] of Object.entries(freq)) {
    const pct = count / chars.length;
    if (pct > 0.25) {
      results.push({
        type: 'character_distribution',
        detail: `Character "${ch}" appears ${(pct * 100).toFixed(1)}% of the time (threshold: 25%)`,
        confidence: 0.65,
      });
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Tool definition
// ---------------------------------------------------------------------------

const findContradictions = defineTool({
  name: 'find_contradictions',
  description:
    'Adversarially analyse decoded Voynich text for internal contradictions, anachronisms, and statistical anomalies. Returns an adversarial score and a SURVIVED / FALSIFIED verdict.',
  parameters: z.object({
    decoded_text: z.string().describe('The decoded/translated manuscript text to analyse'),
    section: z
      .string()
      .describe('Voynich manuscript section name (e.g. herbal, astronomical, biological)'),
  }),
  handler: async ({ decoded_text, section }) => {
    const words = tokenise(decoded_text);

    const contradictions: Contradiction[] = [
      ...checkAntonymProximity(words),
      ...checkAnachronisms(decoded_text),
      ...checkCharDistribution(decoded_text),
    ];

    // Score: start at 0.8, subtract 0.15 per contradiction, floor at 0
    const score = Math.max(0, 0.8 - contradictions.length * 0.15);

    const verdict: 'SURVIVED' | 'FALSIFIED' = score >= 0.5 ? 'SURVIVED' : 'FALSIFIED';

    return {
      section,
      adversarial: score,
      contradictions,
      verdict,
    };
  },
});

// ---------------------------------------------------------------------------
// Tool: score_latin_likelihood — n-gram + morphological analysis
// ---------------------------------------------------------------------------

const scoreLatinLikelihood = defineTool({
  name: 'score_latin_likelihood',
  description:
    'Score how likely the decoded text is to be actual Latin (or the target language), ' +
    'using character bigram frequencies and morphological ending analysis. ' +
    'Returns a likelihood score from 0 (not Latin-like at all) to 1 (statistically consistent with Latin).',
  parameters: z.object({
    decoded_text: z.string().describe('The decoded text to analyse'),
    source_language: z.string().default('latin').describe('Expected source language'),
  }),
  handler: async ({ decoded_text, source_language }) => {
    const text = decoded_text.toLowerCase().replace(/[^a-z\s]/g, '');
    const chars = text.replace(/\s/g, '');

    if (chars.length < 10) {
      return { likelihood: 0, reason: 'Text too short for analysis', details: {} };
    }

    // --- Bigram frequency similarity ---
    // Count bigrams in decoded text
    const textBigrams: Record<string, number> = {};
    let totalBigrams = 0;
    for (let i = 0; i < chars.length - 1; i++) {
      const bg = chars.slice(i, i + 2);
      textBigrams[bg] = (textBigrams[bg] ?? 0) + 1;
      totalBigrams++;
    }

    // Compute cosine similarity with Latin bigram reference
    let dotProduct = 0;
    let normText = 0;
    let normRef = 0;
    const allBigrams = new Set([...Object.keys(textBigrams), ...Object.keys(LATIN_BIGRAM_FREQ)]);
    for (const bg of allBigrams) {
      const textFreq = (textBigrams[bg] ?? 0) / totalBigrams;
      const refFreq = LATIN_BIGRAM_FREQ[bg] ?? 0;
      dotProduct += textFreq * refFreq;
      normText += textFreq * textFreq;
      normRef += refFreq * refFreq;
    }
    const bigramSimilarity = normText > 0 && normRef > 0
      ? dotProduct / (Math.sqrt(normText) * Math.sqrt(normRef))
      : 0;

    // --- Morphological ending check ---
    // What fraction of words end with common Latin suffixes?
    const words = text.split(/\s+/).filter((w) => w.length >= 3);
    const wordsWithLatinEnding = words.filter((w) =>
      LATIN_ENDINGS.some((ending) => w.endsWith(ending))
    );
    const morphScore = words.length > 0 ? wordsWithLatinEnding.length / words.length : 0;

    // --- Word length distribution ---
    // Latin words average 5-7 characters; gibberish from EVA substitution tends to 3-5
    const avgWordLen = words.length > 0
      ? words.reduce((s, w) => s + w.length, 0) / words.length
      : 0;
    const wordLenScore = avgWordLen >= 4 && avgWordLen <= 8 ? 1.0 : avgWordLen >= 3 ? 0.5 : 0.2;

    // --- Composite ---
    const likelihood = Math.round(
      (bigramSimilarity * 0.4 + morphScore * 0.35 + wordLenScore * 0.25) * 1000
    ) / 1000;

    return {
      likelihood,
      bigram_similarity: Math.round(bigramSimilarity * 1000) / 1000,
      morphological_score: Math.round(morphScore * 1000) / 1000,
      word_length_score: Math.round(wordLenScore * 1000) / 1000,
      avg_word_length: Math.round(avgWordLen * 10) / 10,
      words_with_latin_endings: wordsWithLatinEnding.length,
      total_words: words.length,
      source_language,
    };
  },
});

// ---------------------------------------------------------------------------
// Tool: null_baseline_test — shuffled text comparison
// ---------------------------------------------------------------------------

const nullBaselineTest = defineTool({
  name: 'null_baseline_test',
  description:
    'Test whether the decoded text scores significantly better than a null hypothesis ' +
    '(randomly shuffled version of the same text). If the real score is not distinguishable ' +
    'from the shuffled version, the decoding is likely meaningless.',
  parameters: z.object({
    decoded_text: z.string().describe('The decoded text to test'),
    source_language: z.string().default('latin').describe('Expected source language'),
  }),
  handler: async ({ decoded_text, source_language: _source_language }) => {
    const text = decoded_text.toLowerCase().replace(/[^a-z\s]/g, '');
    const chars = text.replace(/\s/g, '');

    if (chars.length < 20) {
      return { distinguishable: false, reason: 'Text too short', p_value: 1.0 };
    }

    // Score the real text
    const realWords = text.split(/\s+/).filter((w) => w.length >= 3);
    const realMorphScore = realWords.length > 0
      ? realWords.filter((w) => LATIN_ENDINGS.some((e) => w.endsWith(e))).length / realWords.length
      : 0;

    // Generate N shuffled versions and score each
    const N_SHUFFLES = 20;
    const shuffleScores: number[] = [];

    for (let i = 0; i < N_SHUFFLES; i++) {
      // Shuffle characters within each word (preserving word boundaries)
      const shuffledWords = realWords.map((w) => {
        const arr = w.split('');
        for (let j = arr.length - 1; j > 0; j--) {
          const k = Math.floor(Math.random() * (j + 1));
          [arr[j], arr[k]] = [arr[k], arr[j]];
        }
        return arr.join('');
      });
      const shuffleMorphScore = shuffledWords.length > 0
        ? shuffledWords.filter((w) => LATIN_ENDINGS.some((e) => w.endsWith(e))).length / shuffledWords.length
        : 0;
      shuffleScores.push(shuffleMorphScore);
    }

    // How many shuffled versions score >= real?
    const countBetter = shuffleScores.filter((s) => s >= realMorphScore).length;
    const pValue = Math.round((countBetter / N_SHUFFLES) * 1000) / 1000;

    // If >50% of shuffled texts score as well as real text, it's not distinguishable
    const distinguishable = pValue < 0.1;
    const shuffleMean = Math.round(
      (shuffleScores.reduce((a, b) => a + b, 0) / N_SHUFFLES) * 1000
    ) / 1000;

    return {
      distinguishable,
      p_value: pValue,
      real_score: Math.round(realMorphScore * 1000) / 1000,
      shuffle_mean: shuffleMean,
      shuffle_scores_above_real: countBetter,
      total_shuffles: N_SHUFFLES,
      verdict: distinguishable
        ? 'Text is statistically distinguishable from random — passes null test'
        : 'Text is NOT distinguishable from shuffled version — likely meaningless',
    };
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Critic — an adversarial falsifier. Your job is to determine',
    'whether decoded manuscript text is real language or meaningless gibberish.',
    '',
    'For EVERY hypothesis you receive, run ALL THREE tools in order:',
    '1. find_contradictions — checks for anachronisms, antonym proximity, character anomalies',
    '2. score_latin_likelihood — checks bigram frequencies and morphological endings against Latin',
    '3. null_baseline_test — compares the text against shuffled versions (null hypothesis)',
    '',
    'A decoding is only credible if it:',
    '  - Passes find_contradictions (adversarial >= 0.5)',
    '  - Has Latin-like structure (likelihood >= 0.3)',
    '  - Is distinguishable from shuffled text (distinguishable = true)',
    '',
    'Respond with ONLY a JSON object:',
    '  { "adversarial": <0-1>, "likelihood": <0-1>, "null_test_passed": <true/false> }',
    '',
    'Do NOT include scores you did not compute with a tool.',
  ].join('\n'),
  tools: [findContradictions, scoreLatinLikelihood, nullBaselineTest],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  { name: 'voynich-critic', description: 'Adversarial falsifier for decoded Voynich manuscript text' },
  agentExports,
);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(console.error);

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8003');
app.listen(port, () => {
  console.log(`Voynich Critic running at http://localhost:${port}`);
  console.log(`  POST /responses               — agent endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  GET  /mcp                     — MCP server`);
  console.log(`  GET  /_apx/agent              — dev chat UI`);
  console.log(`  GET  /_apx/tools              — tool inspector`);
});

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
  resolveToken,
  resolveHost,
} from '../../../src/index.js';
import { POST_RENAISSANCE_CONCEPTS } from '../voynich-config.js';
import { compositeLikelihood, LATIN_ENDINGS } from './scoring.js';

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
// Tool: score_latin_likelihood
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
    const breakdown = compositeLikelihood(decoded_text);
    if (breakdown.likelihood === 0 && breakdown.total_words === 0) {
      return { likelihood: 0, reason: 'Text too short for analysis', source_language };
    }
    return { ...breakdown, source_language };
  },
});

// ---------------------------------------------------------------------------
// Tool: null_baseline_test — shuffled text comparison
// ---------------------------------------------------------------------------

/**
 * Two shuffle strategies for the null-hypothesis test.
 *
 * within-word: shuffles characters inside each word, preserving word boundaries
 *   and per-word character bag. Preserves length distribution exactly. Easiest
 *   null to beat — Latin endings like -us, -um, -is are so frequent that
 *   within-word shuffles will land on them by chance.
 *
 * across-text: pools all characters, shuffles them, then re-splits at the
 *   ORIGINAL word boundaries. Preserves length distribution but completely
 *   destroys per-word character composition. Much harder null — text must
 *   beat both to be credible.
 */
type ShuffleMode = 'within-word' | 'across-text';

function shuffleArray<T>(arr: T[]): T[] {
  const out = [...arr];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function shuffleText(text: string, mode: ShuffleMode): string {
  const lowered = text.toLowerCase().replace(/[^a-z\s]/g, '');
  const words = lowered.split(/\s+/).filter(Boolean);

  if (mode === 'within-word') {
    return words.map((w) => shuffleArray(w.split('')).join('')).join(' ');
  }
  // across-text: pool chars, shuffle, redistribute at original word lengths
  const allChars = words.join('').split('');
  const pool = shuffleArray(allChars);
  const out: string[] = [];
  let cursor = 0;
  for (const w of words) {
    out.push(pool.slice(cursor, cursor + w.length).join(''));
    cursor += w.length;
  }
  return out.join(' ');
}

const N_SHUFFLES = 50;
const P_VALUE_THRESHOLD = 0.1;

const nullBaselineTest = defineTool({
  name: 'null_baseline_test',
  description:
    'Test whether the decoded text scores significantly better than a null hypothesis. ' +
    'Runs TWO shuffles: within-word (preserves per-word char bag) and across-text (pools ' +
    'all chars, redistributes at original word lengths). Both p-values must be < 0.1 for ' +
    'the text to be credibly distinguishable from random. Scores use the full composite ' +
    'likelihood (bigram + morph + word-length), not just morphology.',
  parameters: z.object({
    decoded_text: z.string().describe('The decoded text to test'),
    source_language: z.string().default('latin').describe('Expected source language'),
  }),
  handler: async ({ decoded_text, source_language: _source_language }) => {
    const lowered = decoded_text.toLowerCase().replace(/[^a-z\s]/g, '');
    const chars = lowered.replace(/\s/g, '');

    if (chars.length < 20) {
      return {
        distinguishable: false,
        reason: 'Text too short',
        p_value_within: 1.0,
        p_value_across: 1.0,
      };
    }

    const realScore = compositeLikelihood(decoded_text).likelihood;

    function pValueFor(mode: ShuffleMode): { p: number; mean: number; n_above: number } {
      const scores: number[] = [];
      for (let i = 0; i < N_SHUFFLES; i++) {
        scores.push(compositeLikelihood(shuffleText(decoded_text, mode)).likelihood);
      }
      const nAbove = scores.filter((s) => s >= realScore).length;
      const mean = scores.reduce((a, b) => a + b, 0) / N_SHUFFLES;
      return {
        p: Math.round((nAbove / N_SHUFFLES) * 1000) / 1000,
        mean: Math.round(mean * 1000) / 1000,
        n_above: nAbove,
      };
    }

    const within = pValueFor('within-word');
    const across = pValueFor('across-text');

    const distinguishableWithin = within.p < P_VALUE_THRESHOLD;
    const distinguishableAcross = across.p < P_VALUE_THRESHOLD;
    const distinguishable = distinguishableWithin && distinguishableAcross;

    return {
      distinguishable,
      real_score: Math.round(realScore * 1000) / 1000,
      within_word: {
        p_value: within.p,
        shuffle_mean: within.mean,
        shuffles_above_real: within.n_above,
        distinguishable: distinguishableWithin,
      },
      across_text: {
        p_value: across.p,
        shuffle_mean: across.mean,
        shuffles_above_real: across.n_above,
        distinguishable: distinguishableAcross,
      },
      total_shuffles: N_SHUFFLES,
      verdict: distinguishable
        ? 'Text is statistically distinguishable from BOTH null shuffles — passes null test'
        : `Text is NOT distinguishable from ${distinguishableWithin ? 'across-text' : distinguishableAcross ? 'within-word' : 'either'} shuffle — likely meaningless`,
    };
  },
});

// ---------------------------------------------------------------------------
// Tool: llm_judge — strict PASS/FAIL on grammatical coherence
// ---------------------------------------------------------------------------
//
// Mirrors the pattern from python apx_agent _dev.py /_apx/eval/judge (PR #22):
// strict criterion-based PASS/FAIL with one-sentence reason. Costs one LLM
// call per invocation — orchestrator should gate this on a heuristic threshold
// (e.g. only call when composite likelihood > 0.3) to keep per-batch cost
// bounded.
//
// Subsumes falsification-roadmap item 2 (syntactic well-formedness): a strict
// LLM judge catches "Latin-shaped but ungrammatical" cases that POS heuristics
// would miss.

const JUDGE_MODEL = process.env.JUDGE_MODEL ?? 'databricks-claude-sonnet-4-6';

function parseJudgeOutput(text: string): { verdict: 'PASS' | 'FAIL'; reason: string } {
  const verdictMatch = text.match(/VERDICT:\s*(PASS|FAIL)/i);
  const reasonMatch = text.match(/REASON:\s*(.+?)(?:\n|$)/i);

  if (verdictMatch) {
    return {
      verdict: verdictMatch[1].toUpperCase() as 'PASS' | 'FAIL',
      reason: reasonMatch?.[1].trim() ?? '',
    };
  }
  // No labeled verdict — infer from content. Default to FAIL on ambiguity
  // (conservative: unclear judge ≠ green).
  const upper = text.toUpperCase();
  if (upper.includes('FAIL') && !upper.includes('PASS')) {
    return { verdict: 'FAIL', reason: text.trim().slice(0, 200) };
  }
  if (upper.includes('PASS') && !upper.includes('FAIL')) {
    return { verdict: 'PASS', reason: text.trim().slice(0, 200) };
  }
  return { verdict: 'FAIL', reason: 'Judge output ambiguous; defaulting to FAIL' };
}

const llmJudge = defineTool({
  name: 'llm_judge',
  description:
    'Strict LLM judge for grammatical coherence. Asks an LLM whether the decoded text ' +
    'parses as grammatically coherent medieval Latin (or the target language) with valid ' +
    'noun-verb agreement and recognizable sentence structure. Returns PASS only if the ' +
    'judge is unambiguous; partial / fragmentary / Latin-shaped-but-incoherent text → FAIL. ' +
    'Costs one LLM call per invocation; gate on heuristic threshold to bound batch cost.',
  parameters: z.object({
    decoded_text: z.string().describe('The decoded text to judge'),
    source_language: z.string().default('latin').describe('Expected source language'),
  }),
  handler: async ({ decoded_text, source_language }) => {
    if (decoded_text.trim().length < 20) {
      return {
        verdict: 'FAIL',
        reason: 'Text too short for grammatical judgment',
        duration_ms: 0,
        model: JUDGE_MODEL,
      };
    }

    const prompt = [
      'You are evaluating a candidate decoding of a medieval manuscript fragment.',
      'Reply on two lines in this exact format:',
      'VERDICT: PASS|FAIL',
      'REASON: <one sentence>',
      '',
      `Source language: ${source_language}`,
      `Decoded text: ${decoded_text.slice(0, 1000)}`,
      '',
      `Strict pass: clearly grammatical ${source_language} prose with valid inflection,`,
      'recognizable sentence structure, and coherent semantic content (e.g. botanical /',
      'medical / herbal subject matter consistent with the manuscript).',
      `Partial / fragmentary / ${source_language}-shaped-but-incoherent / random word salad: FAIL.`,
      'When in doubt, FAIL.',
    ].join('\n');

    const t0 = Date.now();
    try {
      const host = resolveHost();
      const token = await resolveToken();
      const res = await fetch(`${host}/serving-endpoints/${JUDGE_MODEL}/invocations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          model: JUDGE_MODEL,
          messages: [{ role: 'user', content: prompt }],
          max_tokens: 200,
        }),
      });

      if (!res.ok) {
        return {
          verdict: 'FAIL',
          reason: `Judge request failed (${res.status})`,
          duration_ms: Date.now() - t0,
          model: JUDGE_MODEL,
          error: await res.text().then((t) => t.slice(0, 200)),
        };
      }

      const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
      const text = data.choices?.[0]?.message?.content ?? '';
      const { verdict, reason } = parseJudgeOutput(text);

      return {
        verdict,
        reason,
        duration_ms: Date.now() - t0,
        model: JUDGE_MODEL,
      };
    } catch (err) {
      return {
        verdict: 'FAIL',
        reason: 'Judge call errored',
        duration_ms: Date.now() - t0,
        model: JUDGE_MODEL,
        error: (err as Error).message,
      };
    }
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
    'For EVERY hypothesis you receive, run the cheap heuristic tools first:',
    '1. find_contradictions — checks for anachronisms, antonym proximity, character anomalies',
    '2. score_latin_likelihood — checks bigram frequencies, morphological endings, word length',
    '3. null_baseline_test — compares the text against TWO shuffled nulls (within-word + across-text)',
    '',
    'Then, ONLY if likelihood >= 0.3 AND null_baseline distinguishable = true,',
    'call the expensive llm_judge tool for grammatical coherence. Skip it on cheap rejects.',
    '4. llm_judge — strict PASS/FAIL on whether the text parses as coherent Latin prose',
    '',
    'A decoding is only credible if it:',
    '  - Passes find_contradictions (adversarial >= 0.5)',
    '  - Has Latin-like structure (likelihood >= 0.3)',
    '  - Is distinguishable from BOTH null shuffles (distinguishable = true)',
    '  - Passes llm_judge (verdict = PASS) when the judge was run',
    '',
    'Respond with ONLY a JSON object:',
    '  { "adversarial": <0-1>, "likelihood": <0-1>, "null_test_passed": <true/false>,',
    '    "judge_verdict": "PASS"|"FAIL"|"SKIPPED" }',
    '',
    'Do NOT include scores you did not compute with a tool.',
  ].join('\n'),
  tools: [findContradictions, scoreLatinLikelihood, nullBaselineTest, llmJudge],
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

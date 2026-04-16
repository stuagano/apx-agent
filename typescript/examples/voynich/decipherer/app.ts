/**
 * Voynich Decipherer — mutation agent (AppKit example).
 *
 * Responsibilities:
 *  - mutate_hypothesis  : produce a child hypothesis by swapping two symbol mappings
 *  - apply_cipher       : apply a symbol map + null-char filter to EVA transcription text
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   npx tsx app.ts
 */

import express from 'express';
import { z } from 'zod';
import {
  defineTool,
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
} from '../../../src/index.js';
import {
  VOYNICH_CIPHER_TYPES,
  VOYNICH_SOURCE_LANGUAGES,
  EVA_COMMON_CHARS,
} from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Tool: mutate_hypothesis
// ---------------------------------------------------------------------------

const mutateHypothesis = defineTool({
  name: 'mutate_hypothesis',
  description:
    'Produce a child hypothesis by applying a deterministic mutation to a parent: ' +
    'swap two randomly-chosen symbol mappings in the symbol map.',
  parameters: z.object({
    parent_id: z.string().describe('ID of the parent hypothesis being mutated'),
    cipher_type: z
      .enum(VOYNICH_CIPHER_TYPES as [string, ...string[]])
      .describe('Cipher family for this hypothesis'),
    source_language: z
      .enum(VOYNICH_SOURCE_LANGUAGES as [string, ...string[]])
      .describe('Proposed source language'),
    symbol_map: z
      .record(z.string(), z.string())
      .describe('Current symbol → plaintext character mapping'),
    null_chars: z
      .array(z.string())
      .describe('EVA symbols treated as null / meaningless fillers'),
    mutation_hint: z
      .string()
      .optional()
      .describe('Optional free-text hint guiding which symbols to swap'),
  }),
  handler: async ({ parent_id, cipher_type, source_language, symbol_map, null_chars, mutation_hint }) => {
    const keys = Object.keys(symbol_map);

    if (keys.length < 2) {
      return {
        error: 'symbol_map must contain at least two entries to perform a swap mutation',
        parent_id,
      };
    }

    // Deterministic selection: use a simple hash of parent_id + hint for
    // reproducibility without relying on a seeded PRNG.
    const seed = hashString(`${parent_id}${mutation_hint ?? ''}`);
    const idxA = seed % keys.length;
    const idxB = (seed + 1) % keys.length === idxA
      ? (seed + 2) % keys.length
      : (seed + 1) % keys.length;

    const keyA = keys[idxA];
    const keyB = keys[idxB];

    const mutatedMap = { ...symbol_map, [keyA]: symbol_map[keyB], [keyB]: symbol_map[keyA] };

    return {
      parent_id,
      cipher_type,
      source_language,
      symbol_map: mutatedMap,
      null_chars,
      mutation: {
        type: 'swap',
        swapped: [keyA, keyB],
        hint_applied: mutation_hint ?? null,
      },
    };
  },
});

// ---------------------------------------------------------------------------
// Tool: apply_cipher
// ---------------------------------------------------------------------------

const applyCipher = defineTool({
  name: 'apply_cipher',
  description:
    'Apply a symbol map to EVA transcription text, filtering out null characters first. ' +
    'Returns the decoded text alongside original and decoded lengths.',
  parameters: z.object({
    eva_text: z
      .string()
      .describe('Raw EVA-transcribed Voynich text (space-separated symbols)'),
    symbol_map: z
      .record(z.string(), z.string())
      .describe('Symbol → plaintext character mapping'),
    null_chars: z
      .array(z.string())
      .describe('EVA symbols to discard before mapping'),
  }),
  handler: async ({ eva_text, symbol_map, null_chars }) => {
    const nullSet = new Set(null_chars);

    // Tokenise on whitespace; each token is one EVA symbol
    const tokens = eva_text.split(/\s+/).filter(Boolean);
    const original_length = tokens.length;

    const meaningful = tokens.filter((t) => !nullSet.has(t));
    const decoded_text = meaningful
      .map((t) => symbol_map[t] ?? t)   // unknown symbols pass through unchanged
      .join('');

    return {
      decoded_text,
      original_length,
      decoded_length: decoded_text.length,
    };
  },
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Simple non-cryptographic hash → unsigned 32-bit integer. */
function hashString(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (Math.imul(h, 0x01000193) >>> 0);
  }
  return h;
}

// ---------------------------------------------------------------------------
// AppKit plugins
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Decipherer — a mutation agent responsible for evolving cipher hypotheses.',
    `You understand the EVA transliteration alphabet. Common EVA characters include: ${EVA_COMMON_CHARS.join(', ')}.`,
    `Supported cipher families: ${VOYNICH_CIPHER_TYPES.join(', ')}.`,
    `Candidate source languages: ${VOYNICH_SOURCE_LANGUAGES.join(', ')}.`,
    'Use mutate_hypothesis to produce a child from a parent hypothesis, and apply_cipher to test a mapping against a text snippet.',
  ].join(' '),
  tools: [mutateHypothesis, applyCipher],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-decipherer',
    description: 'Mutation agent for evolving Voynich cipher hypotheses',
  },
  agentExports,
);
discoveryPlugin.setup();

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8001');
app.listen(port, () => {
  console.log(`Voynich Decipherer running at http://localhost:${port}`);
  console.log(`  POST /responses               — agent endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  GET  /_apx/agent              — dev chat UI`);
  console.log(`  GET  /_apx/tools              — tool inspector`);
});

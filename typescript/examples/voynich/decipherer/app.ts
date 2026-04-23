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
// EVA reference data for grounding-guided mutation
// ---------------------------------------------------------------------------

/**
 * Common EVA word sequences from the herbal section.
 * Used to reverse-engineer symbol maps from target plaintext.
 */
const HERBAL_EVA_WORDS = [
  'daiin', 'chedy', 'qokeedy', 'shedy', 'otedy', 'qokain',
  'chol', 'chor', 'shol', 'shory', 'dar', 'aly', 'okeey',
  'chey', 'cthor', 'oteey', 'ykeedy', 'qokedy', 'lchedy',
  'cheol', 'otaiin', 'dain', 'aiin', 'ol', 'or', 'ar',
];

/**
 * Target botanical terms per candidate language, keyed by language.
 * These are common plant-related words that the grounder expects.
 */
const BOTANICAL_TARGETS: Record<string, string[]> = {
  latin: [
    'mandragora', 'radix', 'herba', 'folium', 'flos', 'semen',
    'cannabis', 'hedera', 'eryngium', 'carduus', 'campanula',
    'geranium', 'planta', 'cortex', 'spina', 'medicinalis',
  ],
  italian: [
    'mandragola', 'radice', 'erba', 'foglia', 'fiore', 'seme',
    'canapa', 'edera', 'cardo', 'pianta', 'corteccia', 'spina',
  ],
  greek: ['mandragoras', 'rhiza', 'botanē', 'phyllon', 'anthos'],
  hebrew: ['dudaim', 'shoresh', 'esev', 'aleh', 'perach'],
  arabic: ['yabruh', 'jidr', 'ushb', 'waraqa', 'zahrah'],
};

/**
 * Tokenize an EVA word into individual characters, handling digraphs.
 */
function tokenizeEva(word: string): string[] {
  const tokens: string[] = [];
  let i = 0;
  while (i < word.length) {
    // Check for digraphs (ch, sh, th)
    if (i + 1 < word.length) {
      const pair = word.substring(i, i + 2);
      if (pair === 'ch' || pair === 'sh' || pair === 'th') {
        tokens.push(pair);
        i += 2;
        continue;
      }
    }
    tokens.push(word[i]);
    i++;
  }
  return tokens;
}

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
// Tool: reverse_engineer_mapping
// ---------------------------------------------------------------------------

const reverseEngineerMapping = defineTool({
  name: 'reverse_engineer_mapping',
  description:
    'Reverse-engineer a symbol map by aligning EVA characters with target plaintext characters. ' +
    'Given a target word (e.g., "mandragora") and a parent symbol map, produces a new map where ' +
    'common EVA sequences will decode to fragments of the target word. Use this when mutating ' +
    'hypotheses to bias symbol maps toward producing botanical terms.',
  parameters: z.object({
    target_word: z
      .string()
      .describe('The target plaintext word to reverse-engineer toward (e.g., "mandragora").'),
    source_language: z
      .string()
      .describe('The candidate source language.'),
    parent_map: z
      .any()
      .optional()
      .describe('Existing parent symbol map to merge new mappings into. If omitted, starts fresh.'),
  }),
  handler: async ({
    target_word,
    source_language,
    parent_map,
  }: {
    target_word: string;
    source_language: string;
    parent_map?: Record<string, string>;
  }) => {
    const target = target_word.toLowerCase();
    const baseMap: Record<string, string> = parent_map ? { ...parent_map } : {};

    // Pick several EVA words and align their characters with the target
    const evaWords = HERBAL_EVA_WORDS.slice(0, 6); // use first 6 common words
    let targetIdx = 0;

    for (const evaWord of evaWords) {
      const evaTokens = tokenizeEva(evaWord);
      for (const token of evaTokens) {
        if (targetIdx < target.length) {
          baseMap[token] = target[targetIdx];
          targetIdx++;
          if (targetIdx >= target.length) targetIdx = 0; // cycle
        }
      }
    }

    // Apply the map to all EVA samples to produce decoded text
    const decodedSample = HERBAL_EVA_WORDS
      .map((w) => {
        const tokens = tokenizeEva(w);
        return tokens.map((t) => baseMap[t] || t).join('');
      })
      .join(' ');

    // Get additional botanical targets for this language
    const targets = BOTANICAL_TARGETS[source_language] || BOTANICAL_TARGETS['latin'] || [];
    const randomTarget = targets[Math.floor(Math.random() * targets.length)] || target_word;

    return {
      symbol_map: baseMap,
      decoded_sample: decodedSample,
      target_word: target,
      secondary_target: randomTarget,
      source_language,
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
    'You are the Voynich Decipherer, a mutation agent for evolving cipher hypotheses.',
    '',
    'WHEN YOU RECEIVE A MESSAGE:',
    '1. Parse the JSON input. It contains: parents (array), generation (number), batch_size (number).',
    '',
    '2. IF parents array is EMPTY (seeding):',
    '   - For each hypothesis up to batch_size:',
    '     a. Pick a random language from: latin, italian, greek, hebrew, arabic',
    '     b. Call reverse_engineer_mapping with a random botanical target word and that language',
    '     c. Build a hypothesis object from the result',
    '',
    '3. IF parents array is NOT EMPTY (mutation):',
    '   - For each parent (up to batch_size, cycling if needed):',
    '     a. Extract parent.metadata.source_language and parent.metadata.symbol_map',
    '     b. Call reverse_engineer_mapping with a random botanical target word,',
    '        the parent source_language, and the parent symbol_map as parent_map',
    '     c. The tool returns a new symbol_map and decoded_sample',
    '     d. Build a child hypothesis with the new symbol_map and decoded_sample',
    '',
    '4. Build each hypothesis object as:',
    '   {',
    '     "id": "<8 random hex chars>",',
    '     "generation": <generation from input>,',
    '     "parent_id": "<parent.id or empty string for seeds>",',
    '     "fitness": {},',
    '     "metadata": {',
    '       "cipher_type": "substitution",',
    '       "source_language": "<from tool result>",',
    '       "symbol_map": <from tool result>,',
    '       "null_chars": [],',
    '       "decoded_sample": "<from tool result>"',
    '     },',
    '     "flagged_for_review": false',
    '   }',
    '',
    '5. Return ONLY the JSON array of hypothesis objects. No markdown, no explanation.',
    '',
    'CRITICAL: Always call reverse_engineer_mapping for each mutation.',
    'The decoded_sample field is essential — the grounder uses it for scoring.',
  ].join('\n'),
  tools: [mutateHypothesis, applyCipher, reverseEngineerMapping],
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

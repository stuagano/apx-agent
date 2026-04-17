/**
 * Voynich Historian — RAG fitness scorer for the Voynich Manuscript.
 *
 * Evaluates decoded text for historical plausibility by detecting anachronisms
 * and measuring lexical diversity.
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
import { POST_RENAISSANCE_CONCEPTS, SECTION_TO_INDEX } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Tool: score_historical_plausibility
// ---------------------------------------------------------------------------

const scoreHistoricalPlausibility = defineTool({
  name: 'score_historical_plausibility',
  description:
    'Score the historical plausibility of a decoded Voynich Manuscript passage. ' +
    'Checks for post-Renaissance anachronisms and computes lexical diversity. ' +
    'Returns a semantic score (0–1), list of detected anachronisms, word count, ' +
    'lexical diversity ratio, and the corpus index for the given section.',
  parameters: z.object({
    decoded_text: z
      .string()
      .describe('The decoded/translated text passage to evaluate.'),
    section: z
      .string()
      .describe(
        'The manuscript section this text belongs to ' +
          '(herbal, astronomical, biological, cosmological, pharmaceutical, recipes).',
      ),
  }),
  handler: async ({
    decoded_text,
    section,
  }: {
    decoded_text: string;
    section: string;
  }) => {
    // Tokenise to lowercase words, strip punctuation
    const words = decoded_text
      .toLowerCase()
      .replace(/[^a-z\s]/g, ' ')
      .split(/\s+/)
      .filter((w) => w.length > 0);

    const wordCount = words.length;

    // Lexical diversity = unique tokens / total tokens
    const uniqueWords = new Set(words);
    const lexicalDiversity = wordCount > 0 ? uniqueWords.size / wordCount : 0;

    // Anachronism detection — check for any POST_RENAISSANCE_CONCEPTS phrase
    const textLower = decoded_text.toLowerCase();
    const anachronisms = POST_RENAISSANCE_CONCEPTS.filter((concept) =>
      textLower.includes(concept),
    );

    // Scoring
    let score = 0.5;

    // Subtract 0.1 per anachronism
    score -= anachronisms.length * 0.1;

    // Bonus for natural lexical diversity range (not too repetitive, not too random)
    if (lexicalDiversity >= 0.3 && lexicalDiversity <= 0.8) {
      score += 0.2;
    }

    // Clamp to [0, 1]
    score = Math.min(1, Math.max(0, score));

    // Resolve corpus index for the section (default -1 if unknown)
    const corpus =
      SECTION_TO_INDEX[section.toLowerCase() as keyof typeof SECTION_TO_INDEX] ??
      -1;

    return {
      semantic: Math.round(score * 1000) / 1000,
      anachronisms,
      word_count: wordCount,
      lexical_diversity: Math.round(lexicalDiversity * 1000) / 1000,
      corpus,
    };
  },
});

// ---------------------------------------------------------------------------
// Create plugins
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Historian, a specialist in medieval manuscript analysis. ' +
    'Use the score_historical_plausibility tool to evaluate decoded text passages ' +
    'for anachronisms and lexical authenticity. Report findings clearly.',
  tools: [scoreHistoricalPlausibility],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Wire up Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-historian',
    description: 'RAG fitness scorer for Voynich Manuscript decoded text',
  },
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

const port = parseInt(process.env.PORT ?? '8002');
app.listen(port, () => {
  console.log(`Voynich Historian running at http://localhost:${port}`);
  console.log(`  /responses               — agent endpoint (Responses API)`);
  console.log(`  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  /mcp                     — MCP server`);
  console.log(`  /_apx/agent              — dev chat UI`);
  console.log(`  /_apx/tools              — tool inspector`);
});

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
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Critic — an adversarial falsifier whose job is to challenge decoded',
    'manuscript text. Use the find_contradictions tool on any text the user provides.',
    'Report your findings clearly, emphasising the most damaging contradictions.',
    'If the text SURVIVED, explain why it is not (yet) falsifiable. If FALSIFIED, explain',
    'what brought it down.',
  ].join(' '),
  tools: [findContradictions],
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

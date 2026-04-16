/**
 * Example: Voynich Judge — agent eval for reasoning quality.
 *
 * Scores the reasoning quality of Historian and Critic agents
 * based on structural features of their JSON outputs, not their
 * final conclusions. This is the key novel contribution: eval
 * correctness is decoupled from domain knowledge.
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

// ---------------------------------------------------------------------------
// Scoring helpers
// ---------------------------------------------------------------------------

/** Clamp a number to [0, 1]. */
function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

/**
 * Score the output from a Historian agent.
 *
 * Rubric (starts at 0.5):
 *   +0.1  corpus field is present
 *   +0.1  no anachronisms (anachronisms field is absent, null, or empty array)
 *   +0.1  word_count > 10
 *   +0.1  lexical_diversity > 0.3
 *   -0.2  semantic > 0.9 but corpus field is absent (hallucination signal)
 *
 * Returns 0.3 on JSON parse failure.
 */
function scoreHistorian(raw: string): number {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return 0.3;
  }

  let score = 0.5;

  // +0.1 if corpus field present
  if ('corpus' in parsed && parsed.corpus !== null && parsed.corpus !== undefined) {
    score += 0.1;
  }

  // +0.1 if no anachronisms
  const anachronisms = parsed.anachronisms;
  const hasNoAnachronisms =
    anachronisms === null ||
    anachronisms === undefined ||
    (Array.isArray(anachronisms) && anachronisms.length === 0);
  if (hasNoAnachronisms) {
    score += 0.1;
  }

  // +0.1 if word_count > 10
  if (typeof parsed.word_count === 'number' && parsed.word_count > 10) {
    score += 0.1;
  }

  // +0.1 if lexical_diversity > 0.3
  if (typeof parsed.lexical_diversity === 'number' && parsed.lexical_diversity > 0.3) {
    score += 0.1;
  }

  // -0.2 if semantic > 0.9 without corpus (overfitting / hallucination signal)
  const hasCorpus = 'corpus' in parsed && parsed.corpus !== null && parsed.corpus !== undefined;
  if (typeof parsed.semantic === 'number' && parsed.semantic > 0.9 && !hasCorpus) {
    score -= 0.2;
  }

  return clamp(score);
}

/**
 * Score the output from a Critic agent.
 *
 * Rubric (starts at 0.5):
 *   +0.05  per high-confidence contradiction (confidence > 0.7)
 *   -0.03  per low-confidence contradiction (confidence < 0.4)
 *   +0.1   if verdict === 'SURVIVED'
 *
 * Expects a JSON object with optional fields:
 *   contradictions: Array<{ confidence: number; ... }>
 *   verdict: string
 *
 * Returns 0.3 on JSON parse failure.
 */
function scoreCritic(raw: string): number {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return 0.3;
  }

  let score = 0.5;

  const contradictions = parsed.contradictions;
  if (Array.isArray(contradictions)) {
    for (const item of contradictions) {
      if (item !== null && typeof item === 'object') {
        const c = (item as Record<string, unknown>).confidence;
        if (typeof c === 'number') {
          if (c > 0.7) {
            score += 0.05;
          } else if (c < 0.4) {
            score -= 0.03;
          }
        }
      }
    }
  }

  if (parsed.verdict === 'SURVIVED') {
    score += 0.1;
  }

  return clamp(score);
}

// ---------------------------------------------------------------------------
// Tool definition
// ---------------------------------------------------------------------------

const scoreReasoningQuality = defineTool({
  name: 'score_reasoning_quality',
  description:
    'Score the reasoning quality of Historian and/or Critic agent outputs for a given hypothesis. ' +
    'Scores are based on structural features of the JSON outputs (corpus presence, contradiction confidence, etc.), ' +
    'not on domain correctness. Returns a score in [0, 1] for each provided output.',
  parameters: z.object({
    hypothesis_id: z.string().describe('Unique identifier for the hypothesis being evaluated.'),
    historian_output: z
      .string()
      .optional()
      .describe(
        'JSON string output from the Historian agent. Expected fields: corpus, anachronisms, ' +
          'word_count (number), lexical_diversity (number), semantic (number).',
      ),
    critic_output: z
      .string()
      .optional()
      .describe(
        'JSON string output from the Critic agent. Expected fields: ' +
          'contradictions (array of objects with a confidence field), verdict (string).',
      ),
  }),
  handler: async ({ hypothesis_id, historian_output, critic_output }) => {
    const result: {
      hypothesis_id: string;
      historian?: number;
      critic?: number;
    } = { hypothesis_id };

    if (historian_output !== undefined) {
      result.historian = scoreHistorian(historian_output);
    }

    if (critic_output !== undefined) {
      result.critic = scoreCritic(critic_output);
    }

    return result;
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Judge. Your sole purpose is to evaluate the reasoning quality of ' +
    'Historian and Critic agents via the score_reasoning_quality tool. You do not interpret ' +
    'the Voynich manuscript yourself — you score the structural rigor of agent outputs.',
  tools: [scoreReasoningQuality],
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
    name: 'voynich-judge',
    description: 'Agent eval: scores reasoning quality of Historian and Critic agents',
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

const port = parseInt(process.env.PORT ?? '8004');
app.listen(port, () => {
  console.log(`Voynich Judge running at http://localhost:${port}`);
  console.log(`  POST /responses              — judge endpoint (Responses API)`);
  console.log(`  GET  /.well-known/agent.json — A2A discovery card`);
  console.log(`  GET  /mcp                    — MCP server`);
  console.log(`  GET  /_apx/agent             — dev chat UI`);
  console.log(`  GET  /_apx/tools             — tool inspector`);
});

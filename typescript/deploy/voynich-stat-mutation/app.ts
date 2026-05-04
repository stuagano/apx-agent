// typescript/deploy/voynich-stat-mutation/app.ts

import express from 'express';
import { z } from 'zod';
import {
  defineTool,
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  resolveHost,
  resolveToken,
} from './appkit-agent/index.mjs';
import { type HypothesisSpec, FINDINGS_SUMMARY } from './stat-types.ts';

const JUDGE_MODEL = process.env.JUDGE_MODEL ?? 'databricks-claude-sonnet-4-6';

// ---------------------------------------------------------------------------
// LLM helper — same pattern as voynich-critic llmJudge
// ---------------------------------------------------------------------------

async function callLlm(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await resolveToken();
  const res = await fetch(`${host}/serving-endpoints/${JUDGE_MODEL}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      model: JUDGE_MODEL,
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 600,
    }),
  });
  if (!res.ok) throw new Error(`LLM call failed: ${res.status}`);
  const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
  return data.choices?.[0]?.message?.content ?? '';
}

// ---------------------------------------------------------------------------
// Tool: generate_hypothesis
// ---------------------------------------------------------------------------

const TopFindingSchema = z.object({
  feature: z.string(),
  method: z.string(),
  family_a: z.string(),
  family_b: z.string(),
  effect_size: z.number(),
  p_value: z.number(),
  critic_score: z.number(),
  critic_feedback: z.string(),
});

const generateHypothesis = defineTool({
  name: 'generate_hypothesis',
  description:
    'Generate a new statistical hypothesis to test about Voynich EVA vocabulary. ' +
    'Receives the current top findings and their critic feedback, returns a novel HypothesisSpec ' +
    'that extends or diversifies the existing evidence.',
  parameters: z.object({
    top_findings: z.array(TopFindingSchema).describe('Top-scored findings from the current population'),
    generation: z.number().describe('Current EA generation number'),
  }),
  handler: async ({ top_findings, generation }) => {
    const findingsSummary = top_findings.length > 0
      ? top_findings.map((f, i) =>
          `${i + 1}. feature="${f.feature}", method="${f.method}", ${f.family_a} vs ${f.family_b}, r=${f.effect_size}, p=${f.p_value}, critic_score=${f.critic_score}\n   feedback: ${f.critic_feedback}`
        ).join('\n')
      : 'No findings yet — this is generation 1.';

    const KNOWN_FEATURES = [
      'qo-prefix rate', '-chy suffix rate', '-dy suffix rate',
      'ch-init rate', 'short word rate',
    ];
    const ALL_FAMILIES = [
      'solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
      'mint-family', 'ranunculaceae', 'apiaceae', 'verbena',
      'artemisia', 'brassicaceae', 'lily-family',
    ];
    const NEW_FEATURES = [
      'unique word ratio', 'word entropy', 'oq-prefix rate',
      '-ol suffix rate', '-ain suffix rate',
    ];

    const prompt = [
      'You are generating a new statistical hypothesis about EVA vocabulary in the Voynich manuscript.',
      '',
      'ESTABLISHED RESULTS (do NOT reproduce these — propose something new):',
      FINDINGS_SUMMARY,
      '',
      `CURRENT GENERATION: ${generation}`,
      '',
      'TOP FINDINGS SO FAR (highest critic scores — build on these or explore gaps):',
      findingsSummary,
      '',
      'YOUR TASK: Propose ONE new HypothesisSpec that either:',
      '  A) Extends an existing result to a new family (e.g., run morpho permutation test on thistle or plantago)',
      '  B) Tests a new feature not yet explored (e.g., unique word ratio, word entropy, oq-prefix)',
      '  C) Tests a cross-family comparison (e.g., thistle vs plantago instead of vs all-botanical)',
      '  D) Uses a stronger null model (e.g., permutation-test instead of rank-biserial)',
      '',
      `Available features: ${[...KNOWN_FEATURES, ...NEW_FEATURES].join(', ')}`,
      `Available families: ${ALL_FAMILIES.join(', ')} or "all-botanical"`,
      `Available methods: rank-biserial, permutation-test`,
      '',
      'Reply with ONLY a JSON object on a single line (no markdown fences):',
      '{"feature":"...","method":"...","family_a":"...","family_b":"...","rationale":"..."}',
    ].join('\n');

    const raw = await callLlm(prompt);

    // Extract JSON from the response
    const jsonMatch = raw.match(/\{[^{}]+\}/);
    if (!jsonMatch) {
      throw new Error(`LLM did not return valid JSON. Raw response: ${raw.slice(0, 300)}`);
    }

    const spec = JSON.parse(jsonMatch[0]) as HypothesisSpec;

    // Ensure required fields are present
    if (!spec.feature || !spec.method || !spec.family_a || !spec.family_b || !spec.rationale) {
      throw new Error(`Incomplete HypothesisSpec: ${JSON.stringify(spec)}`);
    }

    return spec;
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Statistical Mutation agent.',
    'When asked to generate a hypothesis, call generate_hypothesis with the current top findings.',
    'Return the HypothesisSpec JSON directly.',
  ].join('\n'),
  tools: [generateHypothesis],
});

const agentExports = () => agentPlugin.exports();

const app = express();
app.use(express.json());
agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  { name: 'voynich-stat-mutation', description: 'Generates new statistical hypotheses about Voynich EVA vocabulary' },
  agentExports,
);
discoveryPlugin.setup();

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8006');
app.listen(port, () => {
  console.log(`Voynich Stat Mutation running at http://localhost:${port}`);
});

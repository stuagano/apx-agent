// typescript/deploy/voynich-stat-mutation/app.ts

import express from 'express';
import { z } from 'zod';
import {
  defineTool,
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  resolveHost,
} from './appkit-agent/index.mjs';
import { type HypothesisSpec, FINDINGS_SUMMARY } from './stat-types.ts';

const JUDGE_MODEL = process.env.JUDGE_MODEL ?? process.env.MODEL ?? 'databricks-claude-sonnet-4-6';

// ---------------------------------------------------------------------------
// M2M token cache — bypasses OBO token (limited scopes) for serving endpoint calls
// ---------------------------------------------------------------------------

let cachedM2mToken: string | null = null;
let cachedM2mExpiry = 0;

async function getM2mToken(): Promise<string> {
  if (cachedM2mToken && Date.now() < cachedM2mExpiry) return cachedM2mToken;
  const host = resolveHost();
  const clientId = process.env.DATABRICKS_CLIENT_ID;
  const clientSecret = process.env.DATABRICKS_CLIENT_SECRET;
  if (!clientId || !clientSecret) throw new Error('DATABRICKS_CLIENT_ID/SECRET not set');
  const res = await fetch(`${host}/oidc/v1/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'client_credentials', client_id: clientId, client_secret: clientSecret, scope: 'all-apis' }).toString(),
  });
  if (!res.ok) throw new Error(`M2M token failed: ${res.status}`);
  const data = await res.json() as { access_token: string; expires_in?: number };
  cachedM2mToken = data.access_token;
  cachedM2mExpiry = Date.now() + ((data.expires_in ?? 3600) - 60) * 1000;
  return cachedM2mToken;
}

// ---------------------------------------------------------------------------
// LLM helper
// ---------------------------------------------------------------------------

async function callLlm(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await getM2mToken();
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
    batch_tested: z.array(z.string()).optional().describe('Combinations already tested this batch — do not repeat'),
  }),
  handler: async ({ top_findings, generation, batch_tested = [] }) => {
    const findingsSummary = top_findings.length > 0
      ? top_findings.map((f, i) =>
          `${i + 1}. feature="${f.feature}", ${f.family_a} vs ${f.family_b}, r=${f.effect_size}, p=${f.p_value}, score=${f.critic_score} — ${f.critic_feedback}`
        ).join('\n')
      : 'No findings yet — this is generation 1.';

    // Build exclusion list: DB top findings + within-batch already tested
    const alreadyTested = [
      ...top_findings.map((f) => `"${f.feature}" + ${f.family_a} vs ${f.family_b}`),
      ...batch_tested,
    ].filter((v, i, a) => a.indexOf(v) === i).join('; ');

    const KNOWN_FEATURES = [
      'qo-prefix rate', '-chy suffix rate', '-dy suffix rate',
      'ch-init rate', 'short word rate',
    ];
    // Only families with ≥3 herbal folios in the corpus (queried 2026-05-04)
    const ALL_FAMILIES = [
      'solanaceae', // n=33
      'thistle',    // n=30
      'plantago',   // n=10
      'poppy',      // n=7
      'lily-family', // n=4
      'mint-family', // n=3
      'artemisia',  // n=3
    ];
    const NEW_FEATURES = [
      'unique word ratio', 'word entropy', 'oq-prefix rate',
      '-ol suffix rate', '-ain suffix rate',
    ];

    const prompt = [
      'You are generating a new statistical hypothesis about EVA vocabulary in the Voynich manuscript.',
      '',
      'ESTABLISHED RESULTS (do NOT reproduce):',
      FINDINGS_SUMMARY,
      '',
      `CURRENT GENERATION: ${generation}`,
      '',
      'ALREADY TESTED THIS SESSION (do NOT reproduce any of these exact feature+family combinations):',
      alreadyTested || 'none yet',
      '',
      'TOP FINDINGS SO FAR (for context — use them to guide what to explore NEXT, not repeat):',
      findingsSummary,
      '',
      'YOUR TASK: Propose ONE novel HypothesisSpec not in either exclusion list above. Options:',
      '  A) Take a SIGNIFICANT result (score > 0.3) and test it in a new family',
      '  B) Take an UNTESTED feature and test it in any botanical family',
      '  C) Test a family-vs-family comparison (e.g., thistle vs plantago)',
      '  D) Test a new feature+family pair not in any list above',
      '  IMPORTANT: vary both the feature AND the family from round to round.',
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

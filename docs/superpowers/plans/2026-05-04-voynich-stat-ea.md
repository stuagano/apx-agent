# Voynich Statistical EA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third route to the voynich-orchestrator that runs an EA loop generating, executing, and evolving statistical hypotheses about EVA vocabulary structure, with critic-based fitness scoring.

**Architecture:** `StatEvolutionaryAgent` in the orchestrator calls two new agent apps (`voynich-stat-mutation`, `voynich-stat-executor`) and reuses the existing critic with a new `score_statistical_finding` tool. Results persist to a new `voynich.stat_findings` Delta table. Route 3 in `RouterAgent` dispatches `analyze`/`hypothesis`/`statistical` keywords to the stat agent.

**Tech Stack:** TypeScript, tsx, express, zod, appkit-agent bundle, Databricks SQL REST API

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `typescript/deploy/voynich-orchestrator/stat-types.ts` | Shared interfaces, pure utilities, FINDINGS_SUMMARY |
| Create | `typescript/deploy/voynich-stat-executor/app.ts` | `execute_stat_test` tool — SQL + statistics |
| Create | `typescript/deploy/voynich-stat-executor/package.json` | Dependencies |
| Create | `typescript/deploy/voynich-stat-executor/app.yaml` | Databricks app config |
| Create | `typescript/deploy/voynich-stat-executor/appkit-agent/` | Copy bundle from voynich-critic |
| Create | `typescript/deploy/voynich-stat-mutation/app.ts` | `generate_hypothesis` tool — LLM call |
| Create | `typescript/deploy/voynich-stat-mutation/package.json` | Dependencies |
| Create | `typescript/deploy/voynich-stat-mutation/app.yaml` | Databricks app config |
| Create | `typescript/deploy/voynich-stat-mutation/appkit-agent/` | Copy bundle from voynich-critic |
| Modify | `typescript/deploy/voynich-critic/app.ts` | Add `score_statistical_finding` tool |
| Create | `typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts` | Loop logic + Delta writes + tools |
| Modify | `typescript/deploy/voynich-orchestrator/app.ts` | Route 3, env vars, tool list |
| Create | `typescript/deploy/voynich-orchestrator/stat-calibrate.ts` | Calibration + smoke tests |

---

## Task 1: Shared Types and Utilities

**Files:**
- Create: `typescript/deploy/voynich-orchestrator/stat-types.ts`

- [ ] **Step 1.1: Create stat-types.ts**

```typescript
// typescript/deploy/voynich-orchestrator/stat-types.ts

export interface HypothesisSpec {
  feature: string;        // e.g. "qo-prefix rate", "word entropy", "-dy suffix rate"
  method: string;         // "rank-biserial" | "permutation-test"
  family_a: string;       // e.g. "solanaceae"
  family_b: string;       // e.g. "all-botanical" | "thistle" | "plantago"
  folio_filter?: string;
  null_model?: string;    // "label-shuffle" | "within-quire-shuffle"
  rationale: string;
}

export interface StatFinding {
  spec: HypothesisSpec;
  effect_size: number;
  p_value: number;
  n_samples: number;
  result_table?: string;
  interpretation: string;
}

export const BOTANICAL_FAMILIES = new Set([
  'solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
  'mint-family', 'ranunculaceae', 'apiaceae', 'verbena',
  'artemisia', 'brassicaceae', 'lily-family',
]);

export const FINDINGS_SUMMARY = `
Prior work established four lines of evidence (FINDINGS.md, 2026-05-04):

1. Jaccard clustering: within-family lift = 1.10× overall (flat across distance bins, r=-0.003).
2. Morphological fingerprint (solanaceae only — permutation-tested):
   - qo-prefix: r=+0.532, p<0.001 (permutation max=0.415, N=1000)
   - -chy suffix: r=-0.435, p<0.001
   - -dy suffix: r=+0.367, p<0.001
   - ch-init: r=-0.249, p=0.017
   - short (≤3): r=-0.300, p=0.003
3. Two-tier label/text structure: 13 significant label words (once_rate≥0.70, p<0.05).
   5/13 confirmed botanical-specific (cross-section test): kchy, ckhey, qockhol, ty, oldaiin.
4. NPMI semantic coherence: qualitative only (p=0.212 for permutation test — method invalid).
   Control B: 1/30 cross-family vs 15/15 same-family NPMI associations.

What has NOT been permutation-tested: thistle and plantago morphological fingerprints.
What has NOT been tested: word entropy, unique-word ratio, bigram diversity per family.
What failed: NPMI threshold-count permutation test — needs a different statistical approach.
`.trim();

export function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

export function classifyPlantFamily(subjectCandidates: string): string {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return 'unknown'; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return 'uncertain';
  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();
  if (/papaver|poppy/.test(latin + name)) return 'poppy';
  if (/rosa|rose/.test(latin + name)) return 'rose';
  if (/mentha|mint|melissa|salvia|sage|thymus|thyme|origanum|oregano|rosmarinus|lavandula/.test(latin + name)) return 'mint-family';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name)) return 'thistle';
  if (/helleborus|aconitum|ranunculus|clematis|anemone/.test(latin + name)) return 'ranunculaceae';
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name)) return 'solanaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/brassica|sinapis|mustard|cabbage/.test(latin + name)) return 'brassicaceae';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';
  return 'other-botanical';
}

/** Rank-biserial correlation — non-parametric effect size for two-group comparison. */
export function rankBiserial(groupA: number[], groupB: number[]): number {
  const nA = groupA.length, nB = groupB.length;
  if (nA === 0 || nB === 0) return 0;
  let u = 0;
  for (const a of groupA) {
    for (const b of groupB) {
      if (a > b) u++;
      else if (a === b) u += 0.5;
    }
  }
  return (2 * u) / (nA * nB) - 1;
}

export function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** Compute a named EVA feature rate for a list of words. Returns null for unknown features. */
export function computeFeature(words: string[], feature: string): number | null {
  const n = words.length;
  if (n === 0) return null;
  switch (feature) {
    case 'qo-prefix rate':    return words.filter(w => w.startsWith('qo')).length / n;
    case '-chy suffix rate':  return words.filter(w => w.endsWith('chy')).length / n;
    case '-dy suffix rate':   return words.filter(w => w.endsWith('dy')).length / n;
    case 'ch-init rate':      return words.filter(w => w.startsWith('ch')).length / n;
    case 'short word rate':   return words.filter(w => w.length <= 3).length / n;
    case 'unique word ratio': return new Set(words).size / n;
    case 'word entropy': {
      const freq = new Map<string, number>();
      for (const w of words) freq.set(w, (freq.get(w) ?? 0) + 1);
      let entropy = 0;
      for (const c of freq.values()) {
        const p = c / n;
        entropy -= p * Math.log2(p);
      }
      return entropy;
    }
    case 'oq-prefix rate':    return words.filter(w => w.startsWith('oq')).length / n;
    case '-ol suffix rate':   return words.filter(w => w.endsWith('ol')).length / n;
    case '-ain suffix rate':  return words.filter(w => w.endsWith('ain')).length / n;
    default: return null;
  }
}

/** Normal approximation p-value for rank-biserial r (two-tailed). */
export function approxPValue(r: number, nA: number, nB: number): number {
  const variance = (nA + nB + 1) / (3 * nA * nB);
  const z = Math.abs(r) / Math.sqrt(variance);
  // Two-tailed normal p-value via complementary error function approximation
  const t = 1 / (1 + 0.2316419 * z);
  const poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  const pdf = Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI);
  const tailP = pdf * poly;
  return Math.min(1, 2 * tailP);
}
```

- [ ] **Step 1.2: Verify types compile**

```bash
cd typescript/deploy/voynich-orchestrator
npx tsx --no-cache -e "import { rankBiserial, computeFeature, approxPValue } from './stat-types.ts'; const r = rankBiserial([0.1,0.2,0.3],[0.05,0.08]); const p = computeFeature(['qokeedy','daiin','qochor'],'qo-prefix rate'); const pv = approxPValue(0.532,33,60); console.log('r=',r.toFixed(3),'feature=',p?.toFixed(3),'p=',pv.toFixed(3)); if (r < 0 || p === null || pv > 0.1) throw new Error('sanity failed');"
```

Expected output (exact values may vary slightly):
```
r= 1.000 feature= 0.667 p= 0.000
```

- [ ] **Step 1.3: Commit**

```bash
git add typescript/deploy/voynich-orchestrator/stat-types.ts
git commit -m "feat(voynich-stat): stat-types — HypothesisSpec, StatFinding, shared utilities"
```

---

## Task 2: voynich-stat-executor App

**Files:**
- Create: `typescript/deploy/voynich-stat-executor/app.ts`
- Create: `typescript/deploy/voynich-stat-executor/package.json`
- Create: `typescript/deploy/voynich-stat-executor/app.yaml`
- Create: `typescript/deploy/voynich-stat-executor/appkit-agent/` (copy from voynich-critic)

- [ ] **Step 2.1: Bootstrap the directory**

```bash
mkdir -p typescript/deploy/voynich-stat-executor/appkit-agent
cp typescript/deploy/voynich-critic/appkit-agent/index.mjs typescript/deploy/voynich-stat-executor/appkit-agent/
cp typescript/deploy/voynich-critic/appkit-agent/index.d.mts typescript/deploy/voynich-stat-executor/appkit-agent/
```

- [ ] **Step 2.2: Create package.json**

```json
{
  "name": "voynich-stat-executor",
  "private": true,
  "type": "module",
  "scripts": { "start": "tsx app.ts" },
  "dependencies": {
    "express": "^4.21.0",
    "zod": "^4.0.0",
    "zod-to-json-schema": "^3.25.0",
    "tsx": "^4.20.0",
    "typescript": "~5.9.0"
  }
}
```

- [ ] **Step 2.3: Create app.yaml**

```yaml
command:
  - npx
  - tsx
  - app.ts

env:
  - name: PORT
    value: "8000"
  - name: DATABRICKS_HOST
    value: "https://fevm-serverless-stable-qh44kx.cloud.databricks.com"
  - name: DATABRICKS_WAREHOUSE_ID
    value: "76cf70399b8d0ef0"
  - name: DATABRICKS_CLIENT_ID
    value: "PLACEHOLDER_STAT_EXECUTOR_CLIENT_ID"
  - name: DATABRICKS_CLIENT_SECRET
    value: "PLACEHOLDER_STAT_EXECUTOR_CLIENT_SECRET"
```

- [ ] **Step 2.4: Create app.ts**

```typescript
// typescript/deploy/voynich-stat-executor/app.ts

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
import {
  HypothesisSpec,
  StatFinding,
  BOTANICAL_FAMILIES,
  extractEvaWords,
  classifyPlantFamily,
  rankBiserial,
  shuffle,
  computeFeature,
  approxPValue,
} from '../voynich-orchestrator/stat-types.ts';

// ---------------------------------------------------------------------------
// SQL helper — same pattern as morpho-axis-permutation.ts
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Permutation p-value — same approach as morpho-axis-permutation.ts
// ---------------------------------------------------------------------------

function computePermutationP(
  folios: Array<{ family: string; value: number }>,
  familyA: string,
  realR: number,
  nPerms: number,
): number {
  let nAbove = 0;
  for (let i = 0; i < nPerms; i++) {
    const shuffled = shuffle(folios.map((f) => f.family));
    const groupA = folios.filter((_, j) => shuffled[j] === familyA).map((f) => f.value);
    const groupB = folios.filter((_, j) => shuffled[j] !== familyA).map((f) => f.value);
    const permR = rankBiserial(groupA, groupB);
    if (Math.abs(permR) >= Math.abs(realR)) nAbove++;
  }
  return nAbove / nPerms;
}

function mean(arr: number[]): number {
  return arr.length === 0 ? 0 : arr.reduce((a, b) => a + b, 0) / arr.length;
}

// ---------------------------------------------------------------------------
// Tool: execute_stat_test
// ---------------------------------------------------------------------------

const HypothesisSpecSchema = z.object({
  feature: z.string(),
  method: z.string(),
  family_a: z.string(),
  family_b: z.string(),
  folio_filter: z.string().optional(),
  null_model: z.string().optional(),
  rationale: z.string(),
});

const executeStatTest = defineTool({
  name: 'execute_stat_test',
  description:
    'Run a statistical hypothesis test against the Voynich herbal corpus. ' +
    'Loads EVA words per folio, computes the specified feature, splits by family, ' +
    'and returns effect size + p-value. Supports rank-biserial and permutation-test methods.',
  parameters: z.object({ spec: HypothesisSpecSchema }),
  handler: async ({ spec }) => {
    // Validate family names
    if (!BOTANICAL_FAMILIES.has(spec.family_a)) {
      return { error: `Unknown family_a: ${spec.family_a}. Must be one of: ${[...BOTANICAL_FAMILIES].join(', ')}` };
    }
    if (spec.family_b !== 'all-botanical' && !BOTANICAL_FAMILIES.has(spec.family_b)) {
      return { error: `Unknown family_b: ${spec.family_b}. Use a family name or "all-botanical"` };
    }

    // Load folio data
    const [visionRows, evaRows] = await Promise.all([
      executeSql(`
        SELECT folio_id, subject_candidates
        FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
        WHERE section = 'herbal'
        ORDER BY folio_id
      `),
      executeSql(`
        SELECT folio_id, eva_text
        FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
        WHERE section = 'herbal'
        ORDER BY folio_id
      `),
    ]);

    // Build EVA word map
    const evaMap = new Map<string, string[]>();
    for (const r of evaRows) {
      if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
    }

    // Build folio records
    interface FolioRecord { family: string; value: number; }
    const folios: FolioRecord[] = [];
    for (const r of visionRows) {
      if (!r.folio_id) continue;
      const words = evaMap.get(r.folio_id) ?? [];
      if (words.length < 10) continue;
      const family = classifyPlantFamily(r.subject_candidates ?? '[]');
      if (!BOTANICAL_FAMILIES.has(family)) continue;
      const value = computeFeature(words, spec.feature);
      if (value === null) continue;
      folios.push({ family, value });
    }

    if (folios.length < 10) {
      return { error: `Insufficient botanical folios loaded: ${folios.length} (need ≥10)` };
    }

    // Split into groups
    const groupA = folios.filter((f) => f.family === spec.family_a).map((f) => f.value);
    const groupB = spec.family_b === 'all-botanical'
      ? folios.filter((f) => f.family !== spec.family_a).map((f) => f.value)
      : folios.filter((f) => f.family === spec.family_b).map((f) => f.value);

    if (groupA.length < 3) return { error: `Insufficient ${spec.family_a} folios: ${groupA.length} (need ≥3)` };
    if (groupB.length < 3) return { error: `Insufficient ${spec.family_b} folios: ${groupB.length} (need ≥3)` };

    // Compute statistic
    const r = rankBiserial(groupA, groupB);
    const N_PERMS = spec.method === 'permutation-test' ? 1000 : 0;
    const pValue = N_PERMS > 0
      ? computePermutationP(folios, spec.family_a, r, N_PERMS)
      : approxPValue(r, groupA.length, groupB.length);

    const direction = r > 0 ? 'higher' : 'lower';
    const result_table = [
      `| Feature | ${spec.family_a} (n=${groupA.length}) | ${spec.family_b} (n=${groupB.length}) | r | p |`,
      `|---|---|---|---|---|`,
      `| ${spec.feature} | ${mean(groupA).toFixed(3)} | ${mean(groupB).toFixed(3)} | ${r.toFixed(3)} | ${pValue.toFixed(3)} |`,
    ].join('\n');

    const finding: StatFinding = {
      spec,
      effect_size: Math.round(r * 1000) / 1000,
      p_value: Math.round(pValue * 1000) / 1000,
      n_samples: folios.length,
      result_table,
      interpretation:
        `${spec.family_a} folios show ${direction} ${spec.feature} than ${spec.family_b} ` +
        `(r=${r.toFixed(3)}, p=${pValue.toFixed(3)}, n_${spec.family_a}=${groupA.length}, n_${spec.family_b}=${groupB.length}). ` +
        `Method: ${spec.method}${N_PERMS > 0 ? ` (${N_PERMS} permutations)` : ' (normal approximation)'}.`,
    };

    return finding;
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Statistical Executor.',
    'When given a HypothesisSpec, call execute_stat_test to run the statistical test.',
    'Return the StatFinding result directly — do not interpret or elaborate.',
  ].join('\n'),
  tools: [executeStatTest],
});

const agentExports = () => agentPlugin.exports();

const app = express();
app.use(express.json());
agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  { name: 'voynich-stat-executor', description: 'Runs statistical hypothesis tests against the Voynich herbal corpus' },
  agentExports,
);
discoveryPlugin.setup();

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8005');
app.listen(port, () => {
  console.log(`Voynich Stat Executor running at http://localhost:${port}`);
});
```

- [ ] **Step 2.5: Smoke-test locally (requires Databricks credentials)**

```bash
cd typescript/deploy/voynich-stat-executor
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
PORT=8005 npx tsx app.ts &
sleep 3
curl -s -X POST http://localhost:8005/api/agent/tools/execute_stat_test \
  -H "Content-Type: application/json" \
  -d '{"spec":{"feature":"qo-prefix rate","method":"rank-biserial","family_a":"solanaceae","family_b":"all-botanical","rationale":"replication of FINDINGS.md result"}}' \
  | jq '{effect_size: .effect_size, p_value: .p_value, n_samples: .n_samples}'
```

Expected: `effect_size` near 0.532, `p_value` < 0.05, `n_samples` near 93.

```bash
kill %1
```

- [ ] **Step 2.6: Commit**

```bash
git add typescript/deploy/voynich-stat-executor/
git commit -m "feat(voynich-stat): stat-executor app — execute_stat_test tool with SQL + permutation"
```

---

## Task 3: voynich-stat-mutation App

**Files:**
- Create: `typescript/deploy/voynich-stat-mutation/app.ts`
- Create: `typescript/deploy/voynich-stat-mutation/package.json`
- Create: `typescript/deploy/voynich-stat-mutation/app.yaml`
- Create: `typescript/deploy/voynich-stat-mutation/appkit-agent/` (copy from voynich-critic)

- [ ] **Step 3.1: Bootstrap the directory**

```bash
mkdir -p typescript/deploy/voynich-stat-mutation/appkit-agent
cp typescript/deploy/voynich-critic/appkit-agent/index.mjs typescript/deploy/voynich-stat-mutation/appkit-agent/
cp typescript/deploy/voynich-critic/appkit-agent/index.d.mts typescript/deploy/voynich-stat-mutation/appkit-agent/
```

- [ ] **Step 3.2: Create package.json**

```json
{
  "name": "voynich-stat-mutation",
  "private": true,
  "type": "module",
  "scripts": { "start": "tsx app.ts" },
  "dependencies": {
    "express": "^4.21.0",
    "zod": "^4.0.0",
    "zod-to-json-schema": "^3.25.0",
    "tsx": "^4.20.0",
    "typescript": "~5.9.0"
  }
}
```

- [ ] **Step 3.3: Create app.yaml**

```yaml
command:
  - npx
  - tsx
  - app.ts

env:
  - name: PORT
    value: "8000"
  - name: DATABRICKS_HOST
    value: "https://fevm-serverless-stable-qh44kx.cloud.databricks.com"
  - name: DATABRICKS_CLIENT_ID
    value: "PLACEHOLDER_STAT_MUTATION_CLIENT_ID"
  - name: DATABRICKS_CLIENT_SECRET
    value: "PLACEHOLDER_STAT_MUTATION_CLIENT_SECRET"
```

- [ ] **Step 3.4: Create app.ts**

```typescript
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
import { HypothesisSpec, FINDINGS_SUMMARY } from '../voynich-orchestrator/stat-types.ts';

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
```

- [ ] **Step 3.5: Smoke-test locally**

```bash
cd typescript/deploy/voynich-stat-mutation
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
PORT=8006 npx tsx app.ts &
sleep 3
curl -s -X POST http://localhost:8006/api/agent/tools/generate_hypothesis \
  -H "Content-Type: application/json" \
  -d '{"top_findings":[],"generation":1}' \
  | jq .
```

Expected: JSON with `feature`, `method`, `family_a`, `family_b`, `rationale` fields. Feature must NOT be one of the five already-established morphological features for solanaceae.

```bash
kill %1
```

- [ ] **Step 3.6: Commit**

```bash
git add typescript/deploy/voynich-stat-mutation/
git commit -m "feat(voynich-stat): stat-mutation app — generate_hypothesis tool via LLM"
```

---

## Task 4: Add score_statistical_finding to Critic

**Files:**
- Modify: `typescript/deploy/voynich-critic/app.ts`

- [ ] **Step 4.1: Add the tool definition**

After the closing `});` of `llmJudge` (around line 446) and before the `// AppKit wiring` comment, insert:

```typescript
// ---------------------------------------------------------------------------
// Tool: score_statistical_finding — score a StatFinding on validity/novelty/interpretability
// ---------------------------------------------------------------------------

import { FINDINGS_SUMMARY } from '../voynich-orchestrator/stat-types.ts';

const scoreStatisticalFinding = defineTool({
  name: 'score_statistical_finding',
  description:
    'Score a statistical finding about Voynich EVA vocabulary. ' +
    'Evaluates validity (p-value, effect size, sample size), novelty relative to FINDINGS.md, ' +
    'and interpretability. Returns a 0-1 critic_score and text feedback.',
  parameters: z.object({
    finding: z.object({
      spec: z.object({
        feature: z.string(),
        method: z.string(),
        family_a: z.string(),
        family_b: z.string(),
        rationale: z.string(),
      }),
      effect_size: z.number(),
      p_value: z.number(),
      n_samples: z.number(),
      interpretation: z.string(),
    }),
  }),
  handler: async ({ finding }) => {
    // Quick heuristic gate — skip LLM for obviously invalid findings
    const heuristicValidity =
      finding.p_value < 0.05 &&
      Math.abs(finding.effect_size) >= 0.2 &&
      finding.n_samples >= 10;

    if (!heuristicValidity) {
      const reasons: string[] = [];
      if (finding.p_value >= 0.05) reasons.push(`p=${finding.p_value} not significant`);
      if (Math.abs(finding.effect_size) < 0.2) reasons.push(`|r|=${Math.abs(finding.effect_size)} too small`);
      if (finding.n_samples < 10) reasons.push(`n=${finding.n_samples} < 10`);
      return {
        critic_score: 0.1,
        validity: 0.1,
        novelty: 0,
        interpretability: 0,
        critic_feedback: `Rejected by heuristic gate: ${reasons.join('; ')}`,
      };
    }

    const prompt = [
      'You are evaluating a statistical finding about EVA vocabulary in the Voynich manuscript.',
      'Reply on four lines in this exact format:',
      'VALIDITY_SCORE: <0-100>',
      'NOVELTY_SCORE: <0-100>',
      'INTERPRETABILITY_SCORE: <0-100>',
      'REASON: <one sentence>',
      '',
      `Feature: ${finding.spec.feature}`,
      `Method: ${finding.spec.method}`,
      `Family A: ${finding.spec.family_a}  Family B: ${finding.spec.family_b}`,
      `Effect size (r): ${finding.effect_size}`,
      `P-value: ${finding.p_value}`,
      `N samples: ${finding.n_samples}`,
      `Interpretation: ${finding.interpretation}`,
      '',
      'ESTABLISHED PRIOR WORK (deduct novelty if this replicates without extension):',
      FINDINGS_SUMMARY,
      '',
      'VALIDITY (0-100): Is p<0.05? Is |r|>0.2? Is n≥10? Is the method appropriate for the data?',
      'NOVELTY (0-100): Does this test a feature/family/comparison NOT already established?',
      'INTERPRETABILITY (0-100): Can this result be stated as a clear, falsifiable claim?',
      'Score 0 for replicated-without-extension results on NOVELTY, not for all three.',
    ].join('\n');

    const t0 = Date.now();
    const host = resolveHost();
    const token = await resolveToken();
    try {
      const res = await fetch(`${host}/serving-endpoints/${JUDGE_MODEL}/invocations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          model: JUDGE_MODEL,
          messages: [{ role: 'user', content: prompt }],
          max_tokens: 300,
        }),
      });

      if (!res.ok) {
        return { critic_score: 0.3, validity: 0.5, novelty: 0, interpretability: 0,
          critic_feedback: `Judge call failed (${res.status})`, duration_ms: Date.now() - t0 };
      }

      const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
      const text = data.choices?.[0]?.message?.content ?? '';

      const valMatch = text.match(/VALIDITY_SCORE:\s*(\d+)/i);
      const novMatch = text.match(/NOVELTY_SCORE:\s*(\d+)/i);
      const intMatch = text.match(/INTERPRETABILITY_SCORE:\s*(\d+)/i);
      const reasonMatch = text.match(/REASON:\s*(.+?)(?:\n|$)/i);

      const validity = (parseInt(valMatch?.[1] ?? '0') / 100);
      const novelty = (parseInt(novMatch?.[1] ?? '0') / 100);
      const interpretability = (parseInt(intMatch?.[1] ?? '0') / 100);
      const critic_score = Math.round((0.4 * validity + 0.4 * novelty + 0.2 * interpretability) * 1000) / 1000;

      return {
        critic_score,
        validity,
        novelty,
        interpretability,
        critic_feedback: reasonMatch?.[1].trim() ?? text.trim().slice(0, 200),
        duration_ms: Date.now() - t0,
      };
    } catch (err) {
      return { critic_score: 0.2, validity: 0, novelty: 0, interpretability: 0,
        critic_feedback: `Judge threw: ${(err as Error).message}`, duration_ms: Date.now() - t0 };
    }
  },
});
```

- [ ] **Step 4.2: Add scoreStatisticalFinding to the agentPlugin tools array**

Change the tools line in `createAgentPlugin` from:

```typescript
  tools: [findContradictions, scoreLatinLikelihood, nullBaselineTest, llmJudge],
```

To:

```typescript
  tools: [findContradictions, scoreLatinLikelihood, nullBaselineTest, llmJudge, scoreStatisticalFinding],
```

- [ ] **Step 4.3: Update critic instructions to mention the new tool**

In `createAgentPlugin` instructions, append after the existing bullet 4:

```
'5. score_statistical_finding — when given a StatFinding JSON, score its validity, novelty, and interpretability.',
```

- [ ] **Step 4.4: Smoke-test locally**

```bash
cd typescript/deploy/voynich-critic
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
PORT=8003 npx tsx app.ts &
sleep 3
curl -s -X POST http://localhost:8003/api/agent/tools/score_statistical_finding \
  -H "Content-Type: application/json" \
  -d '{
    "finding": {
      "spec": {"feature":"qo-prefix rate","method":"permutation-test","family_a":"solanaceae","family_b":"all-botanical","rationale":"known result"},
      "effect_size": 0.532,
      "p_value": 0.001,
      "n_samples": 93,
      "interpretation": "solanaceae shows higher qo-prefix rate (r=0.532, p<0.001)"
    }
  }' | jq '{critic_score:.critic_score, novelty:.novelty}'
```

Expected: `critic_score` between 0.3 and 0.6 (valid but low novelty — already established). `novelty` < 0.3.

```bash
kill %1
```

- [ ] **Step 4.5: Commit**

```bash
git add typescript/deploy/voynich-critic/app.ts
git commit -m "feat(voynich-stat): critic — add score_statistical_finding tool (validity/novelty/interpretability)"
```

---

## Task 5: StatEvolutionaryAgent

**Files:**
- Create: `typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts`

- [ ] **Step 5.1: Create stat-evolutionary-agent.ts**

```typescript
// typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts

import { z } from 'zod';
import { defineTool, resolveHost, resolveToken, createTrace, addSpan, endSpan, endTrace, runWithContext } from './appkit-agent/index.mjs';
import { HypothesisSpec, StatFinding } from './stat-types.ts';

const STAT_TABLE = process.env.STAT_POPULATION_TABLE
  ?? 'serverless_stable_qh44kx_catalog.voynich.stat_findings';

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '60s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Table setup — idempotent
// ---------------------------------------------------------------------------

async function ensureTable(): Promise<void> {
  await executeSql(`
    CREATE TABLE IF NOT EXISTS ${STAT_TABLE} (
      id STRING,
      generation INT,
      batch_label STRING,
      spec STRING,
      finding STRING,
      critic_score DOUBLE,
      critic_feedback STRING,
      created_at TIMESTAMP
    )
  `);
}

// ---------------------------------------------------------------------------
// Agent-to-agent call helper (mirrors callCriticTool in theory-loop.ts)
// ---------------------------------------------------------------------------

async function callAgentTool(
  agentUrl: string,
  toolName: string,
  params: Record<string, unknown>,
): Promise<unknown | null> {
  const url = `${agentUrl.replace(/\/$/, '')}/api/agent/tools/${toolName}`;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  try { headers.Authorization = `Bearer ${await resolveToken()}`; } catch { /* no auth */ }

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120_000);
    let res: globalThis.Response;
    try {
      res = await fetch(url, {
        method: 'POST', headers,
        body: JSON.stringify(params),
        signal: controller.signal,
      });
    } finally { clearTimeout(timer); }

    if (!res.ok) {
      console.warn(`[stat-ea] ${agentUrl}/${toolName} failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.warn(`[stat-ea] ${agentUrl}/${toolName} threw: ${(err as Error).message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Top-N findings from the population table
// ---------------------------------------------------------------------------

async function getTopFindings(n = 5): Promise<Array<{
  feature: string; method: string; family_a: string; family_b: string;
  effect_size: number; p_value: number; critic_score: number; critic_feedback: string;
}>> {
  const rows = await executeSql(`
    SELECT spec, critic_score, critic_feedback
    FROM ${STAT_TABLE}
    ORDER BY critic_score DESC
    LIMIT ${n}
  `).catch(() => []);

  return rows.flatMap((r) => {
    try {
      const spec = JSON.parse(r.spec ?? '{}') as HypothesisSpec;
      return [{
        feature: spec.feature,
        method: spec.method,
        family_a: spec.family_a,
        family_b: spec.family_b,
        effect_size: 0,
        p_value: 1,
        critic_score: parseFloat(r.critic_score ?? '0'),
        critic_feedback: r.critic_feedback ?? '',
      }];
    } catch { return []; }
  });
}

// ---------------------------------------------------------------------------
// Persist one finding
// ---------------------------------------------------------------------------

async function persistFinding(
  generation: number,
  batchLabel: string,
  finding: StatFinding,
  criticScore: number,
  criticFeedback: string,
): Promise<void> {
  const id = crypto.randomUUID();
  const specJson = JSON.stringify(finding.spec).replace(/'/g, "''");
  const findingJson = JSON.stringify(finding).replace(/'/g, "''");
  const feedbackEsc = criticFeedback.replace(/'/g, "''");

  await executeSql(`
    INSERT INTO ${STAT_TABLE} (id, generation, batch_label, spec, finding, critic_score, critic_feedback, created_at)
    VALUES ('${id}', ${generation}, '${batchLabel}', '${specJson}', '${findingJson}', ${criticScore}, '${feedbackEsc}', current_timestamp())
  `);
}

// ---------------------------------------------------------------------------
// Regression guard — warn if best score drops across generations
// ---------------------------------------------------------------------------

async function checkRegression(generation: number, currentBest: number): Promise<void> {
  if (generation < 2) return;
  const rows = await executeSql(`
    SELECT MAX(critic_score) AS best
    FROM ${STAT_TABLE}
    WHERE generation = ${generation - 1}
  `).catch(() => []);
  const prevBest = parseFloat(rows[0]?.best ?? '0');
  if (currentBest < prevBest - 0.05) {
    console.warn(`[stat-ea] REGRESSION: gen ${generation} best=${currentBest.toFixed(3)} < gen ${generation-1} best=${prevBest.toFixed(3)}`);
  }
}

// ---------------------------------------------------------------------------
// StatEvolutionaryAgent — exposes tools for the RouterAgent
// ---------------------------------------------------------------------------

type AgentTool = ReturnType<typeof defineTool>;

export class StatEvolutionaryAgent {
  private mutationAgentUrl: string;
  private executorAgentUrl: string;
  private criticAgentUrl: string;
  private _tools: AgentTool[];

  constructor(opts: {
    mutationAgentUrl: string;
    executorAgentUrl: string;
    criticAgentUrl: string;
  }) {
    this.mutationAgentUrl = opts.mutationAgentUrl;
    this.executorAgentUrl = opts.executorAgentUrl;
    this.criticAgentUrl = opts.criticAgentUrl;
    this._tools = this.buildTools();
  }

  collectTools(): AgentTool[] {
    return this._tools;
  }

  async run(messages: Array<{ role: string; content: string }>): Promise<string> {
    const last = messages[messages.length - 1]?.content?.toLowerCase() ?? '';
    if (last.includes('run') || last.includes('start') || last.includes('loop')) {
      return 'Use the run_stat_loop tool to start the EA loop, or list_findings to see current results.';
    }
    return 'Statistical EA ready. Tools: run_stat_loop, list_findings.';
  }

  async *stream(messages: Array<{ role: string; content: string }>): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  private buildTools(): AgentTool[] {
    const self = this;

    const runStatLoop = defineTool({
      name: 'run_stat_loop',
      description:
        'Run N rounds of the statistical EA loop: generate hypothesis → execute test → score with critic → persist result. ' +
        'Each round calls the stat-mutation agent, stat-executor, and critic in sequence.',
      parameters: z.object({
        n_rounds: z.number().int().min(1).max(50).describe('Number of EA rounds to run'),
        batch_label: z.string().optional().describe('Optional label for this run (e.g. "gen-1")'),
      }),
      handler: async ({ n_rounds, batch_label }) => {
        await ensureTable();
        const label = batch_label ?? `batch-${Date.now()}`;

        const rows = await executeSql(`SELECT MAX(generation) AS maxgen FROM ${STAT_TABLE}`).catch(() => []);
        const generation = (parseInt(rows[0]?.maxgen ?? '0') || 0) + 1;

        const results: Array<{ spec: HypothesisSpec; effect_size: number; p_value: number; critic_score: number }> = [];

        for (let round = 0; round < n_rounds; round++) {
          console.log(`[stat-ea] gen=${generation} round=${round + 1}/${n_rounds}`);

          // 1. Generate hypothesis
          const topFindings = await getTopFindings(5);
          const spec = await callAgentTool(self.mutationAgentUrl, 'generate_hypothesis', {
            top_findings: topFindings,
            generation,
          }) as HypothesisSpec | null;

          if (!spec?.feature) {
            console.warn(`[stat-ea] round ${round + 1}: mutation agent returned invalid spec`);
            continue;
          }

          // 2. Execute test
          const finding = await callAgentTool(self.executorAgentUrl, 'execute_stat_test', {
            spec,
          }) as StatFinding | null;

          if (!finding?.effect_size === undefined || (finding as any)?.error) {
            console.warn(`[stat-ea] round ${round + 1}: executor returned error: ${(finding as any)?.error}`);
            continue;
          }

          // 3. Score with critic
          const scored = await callAgentTool(self.criticAgentUrl, 'score_statistical_finding', {
            finding,
          }) as { critic_score?: number; critic_feedback?: string } | null;

          const criticScore = scored?.critic_score ?? 0;
          const criticFeedback = scored?.critic_feedback ?? '';

          // 4. Persist
          await persistFinding(generation, label, finding!, criticScore, criticFeedback);

          results.push({
            spec,
            effect_size: finding!.effect_size,
            p_value: finding!.p_value,
            critic_score: criticScore,
          });

          console.log(`[stat-ea] round ${round + 1}: feature="${spec.feature}" ${spec.family_a} vs ${spec.family_b} r=${finding!.effect_size} p=${finding!.p_value} score=${criticScore.toFixed(3)}`);
        }

        const bestScore = results.reduce((max, r) => Math.max(max, r.critic_score), 0);
        await checkRegression(generation, bestScore);

        return {
          generation,
          batch_label: label,
          rounds_completed: results.length,
          best_critic_score: Math.round(bestScore * 1000) / 1000,
          results,
        };
      },
    });

    const listFindings = defineTool({
      name: 'list_findings',
      description: 'List the top statistical findings from the EA population, ordered by critic score.',
      parameters: z.object({
        limit: z.number().int().min(1).max(50).default(10).describe('Number of findings to return'),
      }),
      handler: async ({ limit }) => {
        await ensureTable();
        const rows = await executeSql(`
          SELECT id, generation, batch_label, spec, finding, critic_score, critic_feedback, created_at
          FROM ${STAT_TABLE}
          ORDER BY critic_score DESC
          LIMIT ${limit}
        `);
        return rows.map((r) => {
          try {
            const spec = JSON.parse(r.spec ?? '{}') as HypothesisSpec;
            const finding = JSON.parse(r.finding ?? '{}') as StatFinding;
            return {
              id: r.id,
              generation: r.generation,
              feature: spec.feature,
              method: spec.method,
              family_a: spec.family_a,
              family_b: spec.family_b,
              effect_size: finding.effect_size,
              p_value: finding.p_value,
              n_samples: finding.n_samples,
              critic_score: parseFloat(r.critic_score ?? '0'),
              critic_feedback: r.critic_feedback,
              interpretation: finding.interpretation,
              created_at: r.created_at,
            };
          } catch { return { id: r.id, error: 'failed to parse' }; }
        });
      },
    });

    return [runStatLoop, listFindings];
  }
}
```

- [ ] **Step 5.2: Verify the module compiles**

```bash
cd typescript/deploy/voynich-orchestrator
npx tsx --no-cache -e "import { StatEvolutionaryAgent } from './stat-evolutionary-agent.ts'; console.log('StatEvolutionaryAgent loaded OK');"
```

Expected:
```
StatEvolutionaryAgent loaded OK
```

- [ ] **Step 5.3: Commit**

```bash
git add typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts
git commit -m "feat(voynich-stat): StatEvolutionaryAgent — run_stat_loop + list_findings + Delta persistence"
```

---

## Task 6: Wire Route 3 into app.ts

**Files:**
- Modify: `typescript/deploy/voynich-orchestrator/app.ts`

- [ ] **Step 6.1: Add import**

After the existing `import { TheoryInvestigator }` line, add:

```typescript
import { StatEvolutionaryAgent } from './stat-evolutionary-agent.ts';
```

- [ ] **Step 6.2: Add env var guards and agent instantiation**

After the `const theoryInvestigator = new TheoryInvestigator();` line, add:

```typescript
// ---------------------------------------------------------------------------
// Route 3: Statistical EA (hypothesis generation + execution + critic scoring)
// ---------------------------------------------------------------------------

const statMutationUrl = process.env.STAT_MUTATION_AGENT_URL ?? '';
const statExecutorUrl = process.env.STAT_EXECUTOR_AGENT_URL ?? '';
if (!statMutationUrl) {
  console.warn('[orchestrator] STAT_MUTATION_AGENT_URL not set — stat EA will be unavailable');
}
if (!statExecutorUrl) {
  console.warn('[orchestrator] STAT_EXECUTOR_AGENT_URL not set — stat EA will be unavailable');
}

const statAgent = new StatEvolutionaryAgent({
  mutationAgentUrl: statMutationUrl,
  executorAgentUrl: statExecutorUrl,
  criticAgentUrl: criticAgentUrl,
});
```

- [ ] **Step 6.3: Add STAT_KEYWORDS and Route 3 to the RouterAgent**

Add the constant after the existing `THEORY_KEYWORDS` line:

```typescript
const STAT_KEYWORDS = ['analyz', 'hypothesis', 'statistic', 'run stat', 'finding', 'test feature', 'test family', 'ea loop', 'stat loop'];
```

In the `RouterAgent` routes array, add Route 3 after the `theory_investigation` route:

```typescript
    {
      name: 'statistical_analysis',
      description: 'Statistical hypothesis generation — propose and test new features/family comparisons about EVA vocabulary',
      agent: statAgent,
      condition: (msgs) => {
        const last = msgs[msgs.length - 1]?.content?.toLowerCase() ?? '';
        return STAT_KEYWORDS.some((kw) => last.includes(kw));
      },
    },
```

- [ ] **Step 6.4: Add stat tools to the agentPlugin tool list**

Change:

```typescript
const allTools = [
  ...evolutionaryAgent.collectTools(),
  ...theoryInvestigator.collectTools(),
];
```

To:

```typescript
const allTools = [
  ...evolutionaryAgent.collectTools(),
  ...theoryInvestigator.collectTools(),
  ...statAgent.collectTools(),
];
```

- [ ] **Step 6.5: Update agentPlugin instructions to mention stat tools**

In `createAgentPlugin` instructions, append:

```typescript
'  Statistical Analysis: run_stat_loop, list_findings',
'',
'When users ask about analyzing EVA features, testing hypotheses, or running statistical tests, use the stat tools.',
```

- [ ] **Step 6.6: Add env vars to app.yaml**

Append to `typescript/deploy/voynich-orchestrator/app.yaml`:

```yaml
  - name: STAT_MUTATION_AGENT_URL
    value: "PLACEHOLDER_STAT_MUTATION_URL"
  - name: STAT_EXECUTOR_AGENT_URL
    value: "PLACEHOLDER_STAT_EXECUTOR_URL"
  - name: STAT_POPULATION_TABLE
    value: "serverless_stable_qh44kx_catalog.voynich.stat_findings"
```

- [ ] **Step 6.7: Verify the app starts**

```bash
cd typescript/deploy/voynich-orchestrator
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
export MUTATION_AGENT_URL=http://localhost:8099
export FITNESS_AGENT_URLS=http://localhost:8099
PORT=8000 LOOP_MODE=none npx tsx app.ts &
sleep 4
curl -s http://localhost:8000/.well-known/agent.json | jq '{name:.name, skills: [.skills[].id]}'
kill %1
```

Expected: skill list includes `run_stat_loop` and `list_findings` alongside the existing tools.

- [ ] **Step 6.8: Commit**

```bash
git add typescript/deploy/voynich-orchestrator/app.ts typescript/deploy/voynich-orchestrator/app.yaml
git commit -m "feat(voynich-stat): orchestrator Route 3 — StatEvolutionaryAgent wired into RouterAgent"
```

---

## Task 7: Calibration Script

**Files:**
- Create: `typescript/deploy/voynich-orchestrator/stat-calibrate.ts`

- [ ] **Step 7.1: Create stat-calibrate.ts**

```typescript
// typescript/deploy/voynich-orchestrator/stat-calibrate.ts
// End-to-end calibration for the statistical EA pipeline.
//
// Run:
//   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
//   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
//   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
//   STAT_EXECUTOR_URL=http://localhost:8005 STAT_MUTATION_URL=http://localhost:8006 \
//   CRITIC_URL=http://localhost:8003 npx tsx stat-calibrate.ts

import { resolveToken } from './appkit-agent/index.mjs';
import { rankBiserial, computeFeature, approxPValue, FINDINGS_SUMMARY } from './stat-types.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) { console.error(`  FAIL: ${msg}`); process.exitCode = 1; }
  else        { console.log (`  PASS: ${msg}`); }
}

async function callTool(baseUrl: string, tool: string, params: unknown): Promise<unknown> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  try { headers.Authorization = `Bearer ${await resolveToken()}`; } catch { /* no auth */ }
  const res = await fetch(`${baseUrl.replace(/\/$/, '')}/api/agent/tools/${tool}`, {
    method: 'POST', headers,
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`${tool} returned ${res.status}: ${await res.text().then(t => t.slice(0, 200))}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Unit checks — pure functions
// ---------------------------------------------------------------------------

console.log('\n=== Unit: rankBiserial ===');
{
  const r = rankBiserial([1, 2, 3], [0, 0.5]);
  assert(r === 1.0, `all A > all B → r=1.0 (got ${r})`);
  const r2 = rankBiserial([0.1, 0.2], [0.5, 0.6]);
  assert(r2 === -1.0, `all A < all B → r=-1.0 (got ${r2})`);
  const r3 = rankBiserial([], [1, 2]);
  assert(r3 === 0, `empty groupA → r=0 (got ${r3})`);
}

console.log('\n=== Unit: computeFeature ===');
{
  const words = ['qokeedy', 'daiin', 'qochor', 'chedy', 'ol'];
  const qo = computeFeature(words, 'qo-prefix rate');
  assert(qo !== null && Math.abs(qo - 0.4) < 0.001, `qo-prefix rate = 2/5 = 0.4 (got ${qo})`);
  const unk = computeFeature(words, 'nonexistent feature');
  assert(unk === null, `unknown feature → null`);
  const short = computeFeature(words, 'short word rate');
  assert(short !== null && Math.abs(short - 0.2) < 0.001, `short word rate = 1/5 = 0.2 (got ${short})`);
}

console.log('\n=== Unit: approxPValue ===');
{
  const p = approxPValue(0.532, 33, 60);
  assert(p < 0.001, `r=0.532, n1=33, n2=60 → p < 0.001 (got ${p.toFixed(4)})`);
  const pNS = approxPValue(0.05, 10, 10);
  assert(pNS > 0.5, `r=0.05, n=10 → p > 0.5 (not significant, got ${pNS.toFixed(3)})`);
}

console.log('\n=== Unit: FINDINGS_SUMMARY ===');
assert(FINDINGS_SUMMARY.includes('qo-prefix'), 'FINDINGS_SUMMARY mentions qo-prefix');
assert(FINDINGS_SUMMARY.includes('permutation'), 'FINDINGS_SUMMARY mentions permutation');
assert(FINDINGS_SUMMARY.length > 200, 'FINDINGS_SUMMARY is substantive');

// ---------------------------------------------------------------------------
// Integration: executor
// ---------------------------------------------------------------------------

const EXECUTOR_URL = process.env.STAT_EXECUTOR_URL ?? '';
const MUTATION_URL = process.env.STAT_MUTATION_URL ?? '';
const CRITIC_URL   = process.env.CRITIC_URL ?? '';

if (!EXECUTOR_URL) {
  console.log('\nSkipping executor/mutation/critic integration tests (STAT_EXECUTOR_URL not set)');
  console.log('Set STAT_EXECUTOR_URL, STAT_MUTATION_URL, CRITIC_URL to run integration tests.');
  process.exit(process.exitCode ?? 0);
}

console.log('\n=== Integration: executor — known-good spec ===');
{
  const knownGoodSpec = {
    feature: 'qo-prefix rate',
    method: 'rank-biserial',
    family_a: 'solanaceae',
    family_b: 'all-botanical',
    rationale: 'Replication of FINDINGS.md qo-prefix result (r≈0.532)',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: knownGoodSpec }) as any;
  assert(!result.error, `known-good: no error (got: ${result.error})`);
  assert(typeof result.effect_size === 'number', `known-good: effect_size is a number (got ${result.effect_size})`);
  assert(result.effect_size > 0.3, `known-good: effect_size > 0.3 (got ${result.effect_size})`);
  assert(result.p_value < 0.05, `known-good: p_value < 0.05 (got ${result.p_value})`);
  assert(result.n_samples >= 50, `known-good: n_samples >= 50 (got ${result.n_samples})`);
}

console.log('\n=== Integration: executor — known-weak spec ===');
{
  const knownWeakSpec = {
    feature: 'short word rate',
    method: 'rank-biserial',
    family_a: 'thistle',
    family_b: 'lily-family',
    rationale: 'Random control — small families with no expected signal',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: knownWeakSpec }) as any;
  // May return error (small n) or valid result — just verify it doesn't crash
  assert(typeof result === 'object', `known-weak: returns object (got ${typeof result})`);
  if (!result.error) {
    assert(typeof result.effect_size === 'number', `known-weak: effect_size is number`);
  }
}

console.log('\n=== Integration: executor — malformed spec (missing family_b) ===');
{
  const malformedSpec = {
    feature: 'qo-prefix rate',
    method: 'rank-biserial',
    family_a: 'solanaceae',
    family_b: '',
    rationale: 'Malformed: empty family_b',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: malformedSpec }) as any;
  assert(result.error !== undefined, `malformed: returns error field (got: ${JSON.stringify(result).slice(0, 100)})`);
}

// ---------------------------------------------------------------------------
// Integration: mutation — smoke test
// ---------------------------------------------------------------------------

if (MUTATION_URL) {
  console.log('\n=== Integration: mutation — smoke test ===');
  const result = await callTool(MUTATION_URL, 'generate_hypothesis', {
    top_findings: [],
    generation: 1,
  }) as any;
  assert(typeof result.feature === 'string' && result.feature.length > 0, `mutation: feature is non-empty string`);
  assert(typeof result.method === 'string', `mutation: method is string`);
  assert(typeof result.family_a === 'string', `mutation: family_a is string`);
  assert(typeof result.family_b === 'string', `mutation: family_b is string`);
  assert(typeof result.rationale === 'string' && result.rationale.length > 0, `mutation: rationale is non-empty`);
  const knownEstablished = ['qo-prefix rate + solanaceae', '-chy suffix rate + solanaceae', '-dy suffix rate + solanaceae'];
  const proposed = `${result.feature} + ${result.family_a}`;
  assert(!knownEstablished.some(k => k === proposed), `mutation gen-1: proposes something beyond established solanaceae morphology (got: ${proposed})`);
}

// ---------------------------------------------------------------------------
// Integration: critic — score known-good finding
// ---------------------------------------------------------------------------

if (CRITIC_URL) {
  console.log('\n=== Integration: critic — score known-good finding ===');
  const knownGoodFinding = {
    spec: { feature: 'qo-prefix rate', method: 'permutation-test', family_a: 'solanaceae', family_b: 'all-botanical', rationale: 'known result' },
    effect_size: 0.532,
    p_value: 0.001,
    n_samples: 93,
    interpretation: 'solanaceae shows significantly higher qo-prefix rate vs all-botanical (r=0.532, p<0.001)',
  };
  const result = await callTool(CRITIC_URL, 'score_statistical_finding', { finding: knownGoodFinding }) as any;
  assert(typeof result.critic_score === 'number', `critic known-good: returns critic_score number`);
  assert(result.critic_score >= 0.2, `critic known-good: score >= 0.2 (valid result, low novelty, got ${result.critic_score})`);
  assert(result.critic_score <= 0.7, `critic known-good: score <= 0.7 (known result, not highly novel, got ${result.critic_score})`);
  assert(typeof result.critic_feedback === 'string', `critic known-good: returns critic_feedback string`);

  console.log('\n=== Integration: critic — score low-validity finding ===');
  const lowValidityFinding = {
    spec: { feature: 'short word rate', method: 'rank-biserial', family_a: 'rose', family_b: 'verbena', rationale: 'control' },
    effect_size: 0.05,
    p_value: 0.45,
    n_samples: 8,
    interpretation: 'no significant difference',
  };
  const result2 = await callTool(CRITIC_URL, 'score_statistical_finding', { finding: lowValidityFinding }) as any;
  assert(result2.critic_score <= 0.3, `critic low-validity: score <= 0.3 (got ${result2.critic_score})`);
}

console.log('\n=== Calibration complete ===');
console.log(`Exit code: ${process.exitCode ?? 0}`);
```

- [ ] **Step 7.2: Run unit tests (no Databricks required)**

```bash
cd typescript/deploy/voynich-orchestrator
npx tsx stat-calibrate.ts
```

Expected output:
```
=== Unit: rankBiserial ===
  PASS: all A > all B → r=1.0
  PASS: all A < all B → r=-1.0
  PASS: empty groupA → r=0

=== Unit: computeFeature ===
  PASS: qo-prefix rate = 2/5 = 0.4
  PASS: unknown feature → null
  PASS: short word rate = 1/5 = 0.2

=== Unit: approxPValue ===
  PASS: r=0.532, n1=33, n2=60 → p < 0.001
  PASS: r=0.05, n=10 → p > 0.5 (not significant)

=== Unit: FINDINGS_SUMMARY ===
  PASS: FINDINGS_SUMMARY mentions qo-prefix
  PASS: FINDINGS_SUMMARY mentions permutation
  PASS: FINDINGS_SUMMARY is substantive

Skipping executor/mutation/critic integration tests (STAT_EXECUTOR_URL not set)
```

All PASS, exit code 0.

- [ ] **Step 7.3: Run integration tests (requires all three agents running locally)**

```bash
# Terminal 1: start executor
cd typescript/deploy/voynich-stat-executor && PORT=8005 npx tsx app.ts

# Terminal 2: start mutation
cd typescript/deploy/voynich-stat-mutation && PORT=8006 npx tsx app.ts

# Terminal 3: start critic
cd typescript/deploy/voynich-critic && PORT=8003 npx tsx app.ts

# Terminal 4: run calibration
cd typescript/deploy/voynich-orchestrator
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
STAT_EXECUTOR_URL=http://localhost:8005 \
STAT_MUTATION_URL=http://localhost:8006 \
CRITIC_URL=http://localhost:8003 \
npx tsx stat-calibrate.ts
```

Expected: all PASS, exit code 0.

- [ ] **Step 7.4: Commit**

```bash
git add typescript/deploy/voynich-orchestrator/stat-calibrate.ts
git commit -m "test(voynich-stat): stat-calibrate — unit + integration calibration for EA pipeline"
```

---

## Self-Review Checklist

All spec sections covered:

| Spec section | Task(s) |
|---|---|
| Architecture (3 routes) | Task 6 |
| HypothesisSpec interface | Task 1 |
| StatFinding interface | Task 1 |
| `voynich.stat_findings` Delta table | Task 5 (ensureTable) |
| stat-mutation app | Task 3 |
| stat-executor app | Task 2 |
| critic adapted with statistical mode | Task 4 |
| `stat-evolutionary-agent.ts` wrapper | Task 5 |
| `app.ts` Route 3 + env vars | Task 6 |
| Calibration (known-good, known-weak, malformed) | Task 7 |
| Mutation smoke test | Task 7 |
| Regression guard | Task 5 (checkRegression) |
| Manual review gate (list_findings) | Task 5 (listFindings tool) |

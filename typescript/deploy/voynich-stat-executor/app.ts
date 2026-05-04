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
  type HypothesisSpec,
  type StatFinding,
  BOTANICAL_FAMILIES,
  extractEvaWords,
  classifyPlantFamily,
  rankBiserial,
  shuffle,
  computeFeature,
  approxPValue,
} from './stat-types.ts';

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
      spec: spec as HypothesisSpec,
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

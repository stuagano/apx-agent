// typescript/deploy/voynich-stat-executor/app.ts

import express from 'express';
import { z } from 'zod';
import {
  defineTool,
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  resolveHost,
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
// M2M token cache — bypasses OBO token (limited scopes) for SQL/API calls
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
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await getM2mToken();
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
// In-memory corpus cache — EVA words and family per folio (corpus is static)
// ---------------------------------------------------------------------------

interface FolioEntry { family: string; words: string[]; }

let corpusCache: FolioEntry[] | null = null;

async function loadCorpus(): Promise<FolioEntry[]> {
  if (corpusCache) return corpusCache;

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

  const evaMap = new Map<string, string[]>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractEvaWords(r.eva_text));
  }

  const entries: FolioEntry[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const words = evaMap.get(r.folio_id) ?? [];
    if (words.length < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL_FAMILIES.has(family)) continue;
    entries.push({ family, words });
  }

  corpusCache = entries;
  console.log(`[executor] corpus loaded: ${entries.length} botanical folios (cached)`);
  return entries;
}

// ---------------------------------------------------------------------------
// Permutation p-value — same approach as morpho-axis-permutation.ts
// ---------------------------------------------------------------------------

function computePermutationP(
  folios: Array<{ family: string; value: number }>,
  familyA: string,
  familyB: string,
  realR: number,
  nPerms: number,
): number {
  // For family-vs-family, only permute within the two families (correct null model).
  // For family-vs-all-botanical, permute all labels.
  const pool = familyB === 'all-botanical'
    ? folios
    : folios.filter((f) => f.family === familyA || f.family === familyB);

  let nAbove = 0;
  for (let i = 0; i < nPerms; i++) {
    const shuffled = shuffle(pool.map((f) => f.family));
    const groupA = pool.filter((_, j) => shuffled[j] === familyA).map((f) => f.value);
    const groupB = familyB === 'all-botanical'
      ? pool.filter((_, j) => shuffled[j] !== familyA).map((f) => f.value)
      : pool.filter((_, j) => shuffled[j] === familyB).map((f) => f.value);
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

    // Load corpus (cached after first call within this app instance)
    const corpus = await loadCorpus();

    // Compute feature values for this specific test
    interface FolioRecord { family: string; value: number; }
    const folios: FolioRecord[] = [];
    for (const { family, words } of corpus) {
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
      ? computePermutationP(folios, spec.family_a, spec.family_b, r, N_PERMS)
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
      n_samples: groupA.length + groupB.length,
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

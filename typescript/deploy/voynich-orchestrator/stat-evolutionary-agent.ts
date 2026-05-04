// typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts

import { z } from 'zod';
import { defineTool, resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { type HypothesisSpec, type StatFinding } from './stat-types.ts';

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
    SELECT spec, finding, critic_score, critic_feedback
    FROM ${STAT_TABLE}
    ORDER BY critic_score DESC
    LIMIT ${n}
  `).catch(() => []);

  return rows.flatMap((r) => {
    try {
      const spec = JSON.parse(r.spec ?? '{}') as HypothesisSpec;
      const finding = JSON.parse(r.finding ?? '{}') as StatFinding;
      return [{
        feature: spec.feature,
        method: spec.method,
        family_a: spec.family_a,
        family_b: spec.family_b,
        effect_size: finding.effect_size ?? 0,
        p_value: finding.p_value ?? 1,
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
          }) as (StatFinding & { error?: string }) | null;

          if (!finding || finding.error) {
            console.warn(`[stat-ea] round ${round + 1}: executor returned error: ${finding?.error}`);
            continue;
          }

          // 3. Score with critic
          const scored = await callAgentTool(self.criticAgentUrl, 'score_statistical_finding', {
            finding,
          }) as { critic_score?: number; critic_feedback?: string } | null;

          const criticScore = scored?.critic_score ?? 0;
          const criticFeedback = scored?.critic_feedback ?? '';

          // 4. Persist
          await persistFinding(generation, label, finding, criticScore, criticFeedback);

          results.push({
            spec,
            effect_size: finding.effect_size,
            p_value: finding.p_value,
            critic_score: criticScore,
          });

          console.log(`[stat-ea] round ${round + 1}: feature="${spec.feature}" ${spec.family_a} vs ${spec.family_b} r=${finding.effect_size} p=${finding.p_value} score=${criticScore.toFixed(3)}`);
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

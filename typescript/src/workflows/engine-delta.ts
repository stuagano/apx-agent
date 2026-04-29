/**
 * DeltaEngine — durable WorkflowEngine backed by Delta tables via the
 * Databricks SQL Statements API.
 *
 * Stores run metadata in `{tablePrefix}_runs` and step records in
 * `{tablePrefix}_steps`. Tables are created lazily on first use via
 * `CREATE TABLE IF NOT EXISTS`. Step results are JSON-serialized.
 *
 * Reuses the same auth path (`resolveToken()`) and SQL transport pattern
 * as `PopulationStore`, so OBO / M2M token resolution works identically.
 *
 * Per-process cache for `step()` lookups keeps replays inside one run
 * cheap. Cross-process race on the same (runId, stepKey) is possible but
 * rare; MERGE is used on writes to keep that case idempotent.
 */

import { resolveToken } from '../connectors/types.js';
import type {
  RunFilter,
  RunSnapshot,
  RunStatus,
  RunSummary,
  StepRecord,
  WorkflowEngine,
} from './engine.js';
import { StepFailedError } from './engine.js';

export interface DeltaEngineConfig {
  /**
   * Fully-qualified table prefix, e.g. `main.apx_agent.workflow`. The engine
   * writes to `${tablePrefix}_runs` and `${tablePrefix}_steps`.
   */
  tablePrefix: string;
  /** Databricks workspace host. Defaults to `DATABRICKS_HOST`. */
  host?: string;
  /** SQL warehouse ID. Defaults to `DATABRICKS_WAREHOUSE_ID`. */
  warehouseId?: string;
  /**
   * Whether to cache step lookups in-process. Default true. Disable for
   * tests that want every call to round-trip.
   */
  cacheEnabled?: boolean;
}

interface SqlStatementResponse {
  statement_id: string;
  status: { state: string };
  manifest?: {
    schema?: {
      columns?: Array<{ name: string }>;
    };
  };
  result?: {
    data_array?: Array<Array<string | null>>;
  };
}

export class DeltaEngine implements WorkflowEngine {
  private host: string;
  private warehouseId: string;
  private runsTable: string;
  private stepsTable: string;
  private cacheEnabled: boolean;
  private stepCache: Map<string, StepRecord> = new Map();
  private bootstrapPromise: Promise<void> | null = null;

  constructor(config: DeltaEngineConfig) {
    const rawHost = config.host ?? process.env.DATABRICKS_HOST;
    if (!rawHost) {
      throw new Error('No Databricks host: pass host in config or set DATABRICKS_HOST env var');
    }
    const normalized = rawHost.startsWith('http') ? rawHost : `https://${rawHost}`;
    this.host = normalized.replace(/\/$/, '');

    const wh = config.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID;
    if (!wh) {
      throw new Error('No warehouse ID: pass warehouseId in config or set DATABRICKS_WAREHOUSE_ID env var');
    }
    this.warehouseId = wh;

    this.runsTable = `${config.tablePrefix}_runs`;
    this.stepsTable = `${config.tablePrefix}_steps`;
    this.cacheEnabled = config.cacheEnabled ?? true;
  }

  // ---------------------------------------------------------------------------
  // WorkflowEngine
  // ---------------------------------------------------------------------------

  async startRun(
    workflowName: string,
    input: unknown,
    opts?: { runId?: string },
  ): Promise<string> {
    await this.bootstrap();

    const runId = opts?.runId ?? randomId();
    const inputJson = esc(JSON.stringify(input ?? null));
    const escWorkflow = esc(safeName(workflowName, 'workflowName'));
    const escRunId = esc(safeName(runId, 'runId'));

    // MERGE: re-open existing or insert new. Status flips to 'running' either
    // way — `startRun` is the canonical "this run is active" signal.
    const statement = `
      MERGE INTO ${this.runsTable} AS target
      USING (SELECT
        '${escRunId}' AS run_id,
        '${escWorkflow}' AS workflow_name,
        '${inputJson}' AS input
      ) AS source
      ON target.run_id = source.run_id
      WHEN MATCHED THEN UPDATE SET
        target.status = 'running',
        target.updated_at = current_timestamp()
      WHEN NOT MATCHED THEN INSERT (
        run_id, workflow_name, status, input, started_at, updated_at
      ) VALUES (
        source.run_id, source.workflow_name, 'running', source.input,
        current_timestamp(), current_timestamp()
      )
    `;
    await this.executeSql(statement);
    return runId;
  }

  async step<T>(
    runId: string,
    stepKey: string,
    handler: () => Promise<T>,
  ): Promise<T> {
    await this.bootstrap();

    const cached = await this.lookupStep(runId, stepKey);
    if (cached) {
      if (cached.status === 'completed') {
        return cached.output as T;
      }
      throw new StepFailedError(stepKey, cached.error ?? 'step failed');
    }

    const startMs = Date.now();
    try {
      const result = await handler();
      const record: StepRecord = {
        stepKey,
        status: 'completed',
        output: result,
        durationMs: Date.now() - startMs,
        recordedAt: new Date().toISOString(),
      };
      await this.persistStep(runId, record);
      if (this.cacheEnabled) this.stepCache.set(cacheKey(runId, stepKey), record);
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const record: StepRecord = {
        stepKey,
        status: 'failed',
        error: message,
        durationMs: Date.now() - startMs,
        recordedAt: new Date().toISOString(),
      };
      await this.persistStep(runId, record);
      if (this.cacheEnabled) this.stepCache.set(cacheKey(runId, stepKey), record);
      throw err;
    }
  }

  async finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void> {
    await this.bootstrap();

    const setOutput =
      output === undefined
        ? ''
        : `, output = '${esc(JSON.stringify(output))}'`;
    const statement = `
      UPDATE ${this.runsTable}
      SET status = '${esc(safeName(status, 'status'))}'${setOutput}, updated_at = current_timestamp()
      WHERE run_id = '${esc(safeName(runId, 'runId'))}'
    `;
    await this.executeSql(statement);
  }

  async getRun(runId: string): Promise<RunSnapshot | null> {
    await this.bootstrap();

    const runResp = await this.executeSql(
      `SELECT run_id, workflow_name, status, input, output, started_at, updated_at FROM ${this.runsTable} WHERE run_id = '${esc(safeName(runId, 'runId'))}'`,
    );
    const runRows = parseRows(runResp);
    if (runRows.length === 0) return null;
    const row = runRows[0];

    const stepsResp = await this.executeSql(
      `SELECT step_key, status, output, error, duration_ms, recorded_at FROM ${this.stepsTable} WHERE run_id = '${esc(safeName(runId, 'runId'))}'`,
    );
    const stepRows = parseRows(stepsResp);

    return {
      runId: row['run_id'] ?? runId,
      workflowName: row['workflow_name'] ?? '',
      status: (row['status'] ?? 'running') as RunStatus,
      input: parseJsonOrNull(row['input']),
      output: row['output'] === null || row['output'] === undefined ? undefined : parseJsonOrNull(row['output']),
      startedAt: row['started_at'] ?? '',
      updatedAt: row['updated_at'] ?? '',
      steps: stepRows.map((s) => ({
        stepKey: s['step_key'] ?? '',
        status: (s['status'] ?? 'completed') as 'completed' | 'failed',
        output: s['output'] === null || s['output'] === undefined ? undefined : parseJsonOrNull(s['output']),
        error: s['error'] ?? undefined,
        durationMs: Number(s['duration_ms'] ?? 0),
        recordedAt: s['recorded_at'] ?? '',
      })),
    };
  }

  async listRuns(filter?: RunFilter): Promise<RunSummary[]> {
    await this.bootstrap();

    const conditions: string[] = [];
    if (filter?.workflowName) conditions.push(`workflow_name = '${esc(safeName(filter.workflowName, 'workflowName'))}'`);
    if (filter?.status) conditions.push(`status = '${esc(safeName(filter.status, 'status'))}'`);
    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
    const limit = filter?.limit !== undefined ? `LIMIT ${Math.max(0, Math.floor(filter.limit))}` : '';

    const resp = await this.executeSql(
      `SELECT run_id, workflow_name, status, started_at, updated_at FROM ${this.runsTable} ${where} ORDER BY started_at DESC ${limit}`,
    );
    return parseRows(resp).map((r) => ({
      runId: r['run_id'] ?? '',
      workflowName: r['workflow_name'] ?? '',
      status: (r['status'] ?? 'running') as RunStatus,
      startedAt: r['started_at'] ?? '',
      updatedAt: r['updated_at'] ?? '',
    }));
  }

  /** Drop all in-process caches. Useful for tests. */
  clearCache(): void {
    this.stepCache.clear();
  }

  // ---------------------------------------------------------------------------
  // Internal
  // ---------------------------------------------------------------------------

  private bootstrap(): Promise<void> {
    if (this.bootstrapPromise) return this.bootstrapPromise;
    this.bootstrapPromise = (async () => {
      await this.executeSql(`
        CREATE TABLE IF NOT EXISTS ${this.runsTable} (
          run_id STRING NOT NULL,
          workflow_name STRING NOT NULL,
          status STRING NOT NULL,
          input STRING,
          output STRING,
          started_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        ) USING DELTA
      `);
      await this.executeSql(`
        CREATE TABLE IF NOT EXISTS ${this.stepsTable} (
          run_id STRING NOT NULL,
          step_key STRING NOT NULL,
          status STRING NOT NULL,
          output STRING,
          error STRING,
          duration_ms BIGINT,
          recorded_at TIMESTAMP NOT NULL
        ) USING DELTA
      `);
    })();
    return this.bootstrapPromise;
  }

  private async lookupStep(runId: string, stepKey: string): Promise<StepRecord | null> {
    const ck = cacheKey(runId, stepKey);
    if (this.cacheEnabled) {
      const hit = this.stepCache.get(ck);
      if (hit) return hit;
    }

    const resp = await this.executeSql(
      `SELECT step_key, status, output, error, duration_ms, recorded_at FROM ${this.stepsTable} WHERE run_id = '${esc(safeName(runId, 'runId'))}' AND step_key = '${esc(safeName(stepKey, 'stepKey'))}' LIMIT 1`,
    );
    const rows = parseRows(resp);
    if (rows.length === 0) return null;

    const r = rows[0];
    const record: StepRecord = {
      stepKey: r['step_key'] ?? stepKey,
      status: (r['status'] ?? 'completed') as 'completed' | 'failed',
      output: r['output'] === null || r['output'] === undefined ? undefined : parseJsonOrNull(r['output']),
      error: r['error'] ?? undefined,
      durationMs: Number(r['duration_ms'] ?? 0),
      recordedAt: r['recorded_at'] ?? '',
    };
    if (this.cacheEnabled) this.stepCache.set(ck, record);
    return record;
  }

  private async persistStep(runId: string, record: StepRecord): Promise<void> {
    const outputJson = record.output === undefined ? 'NULL' : `'${esc(JSON.stringify(record.output))}'`;
    const errorVal = record.error === undefined ? 'NULL' : `'${esc(record.error)}'`;

    // MERGE so that a same-key write is idempotent (defends against
    // cross-process races on the same step).
    const statement = `
      MERGE INTO ${this.stepsTable} AS target
      USING (SELECT
        '${esc(safeName(runId, 'runId'))}' AS run_id,
        '${esc(safeName(record.stepKey, 'stepKey'))}' AS step_key,
        '${esc(safeName(record.status, 'status'))}' AS status,
        ${outputJson} AS output,
        ${errorVal} AS error,
        ${record.durationMs} AS duration_ms
      ) AS source
      ON target.run_id = source.run_id AND target.step_key = source.step_key
      WHEN NOT MATCHED THEN INSERT (
        run_id, step_key, status, output, error, duration_ms, recorded_at
      ) VALUES (
        source.run_id, source.step_key, source.status, source.output,
        source.error, source.duration_ms, current_timestamp()
      )
    `;
    await this.executeSql(statement);
  }

  private async executeSql(statement: string): Promise<SqlStatementResponse> {
    const token = await resolveToken();

    const url = `${this.host}/api/2.0/sql/statements/`;
    const body = {
      statement,
      warehouse_id: this.warehouseId,
      wait_timeout: '30s',
      on_wait_timeout: 'CANCEL',
      disposition: 'INLINE',
      format: 'JSON_ARRAY',
    };

    const res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Databricks SQL API ${res.status}: ${text}`);
    }

    return res.json() as Promise<SqlStatementResponse>;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Escape values for inline SQL strings. Handles single quotes and backslashes. */
function esc(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/'/g, "''");
}

/** Reject values containing obvious SQL injection patterns. */
function safeName(s: string, label: string): string {
  if (s.length > 1000) {
    throw new Error(`${label} too long (${s.length} chars)`);
  }
  if (/;\s*(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC|UNION)\b/i.test(s)) {
    throw new Error(`Suspicious SQL pattern in ${label}`);
  }
  return s;
}

function cacheKey(runId: string, stepKey: string): string {
  return `${runId}::${stepKey}`;
}

function randomId(): string {
  // Avoid pulling in `node:crypto` here to stay isomorphic with edge runtimes.
  return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function parseJsonOrNull(s: string | null | undefined): unknown {
  if (s === null || s === undefined || s === '') return null;
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

function parseRows(response: SqlStatementResponse): Array<Record<string, string | null>> {
  const columns = response.manifest?.schema?.columns ?? [];
  const dataArray = response.result?.data_array ?? [];
  return dataArray.map((row) => {
    const obj: Record<string, string | null> = {};
    columns.forEach((col, i) => {
      obj[col.name] = row[i] ?? null;
    });
    return obj;
  });
}

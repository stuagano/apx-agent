/**
 * Tests for DeltaEngine — the SQL-Statements-API backed WorkflowEngine.
 *
 * Runs against a mocked fetch that simulates a tiny Delta-shaped store:
 * it intercepts MERGE / UPDATE / INSERT statements and returns appropriate
 * row sets for SELECT statements.
 *
 * The goal isn't to simulate Delta faithfully — it's to exercise the SQL
 * DeltaEngine actually emits and prove the WorkflowEngine contract holds
 * when the transport round-trips through the SQL Statements API shape.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { DeltaEngine } from '../src/workflows/engine-delta.js';
import { StepFailedError } from '../src/workflows/engine.js';

// ---------------------------------------------------------------------------
// Mock SQL-Statements-API backend
// ---------------------------------------------------------------------------

interface RunRow {
  run_id: string;
  workflow_name: string;
  status: string;
  input: string | null;
  output: string | null;
  started_at: string;
  updated_at: string;
}

interface StepRow {
  run_id: string;
  step_key: string;
  status: string;
  output: string | null;
  error: string | null;
  duration_ms: number;
  recorded_at: string;
}

function makeSqlMock() {
  const runs = new Map<string, RunRow>();
  const steps = new Map<string, StepRow>(); // key: `${run_id}::${step_key}`
  const statements: string[] = [];

  const now = () => '2026-04-19T00:00:00Z';

  function respond(columns: string[], rows: Array<Array<string | null>>) {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        statement_id: `stmt-${statements.length}`,
        status: { state: 'SUCCEEDED' },
        manifest: { schema: { columns: columns.map((name) => ({ name })) } },
        result: { data_array: rows },
      }),
    };
  }

  const fetchMock = vi.fn(async (_url: string, init: RequestInit | undefined) => {
    const body = JSON.parse((init?.body as string) ?? '{}') as { statement: string };
    const sql = body.statement.trim();
    statements.push(sql);

    // CREATE TABLE — no-op response
    if (/^CREATE TABLE IF NOT EXISTS/i.test(sql)) {
      return respond([], []);
    }

    // MERGE INTO ..._runs
    if (/MERGE INTO .*_runs/i.test(sql)) {
      const runId = extract(sql, /'([^']+)' AS run_id/);
      const workflow = extract(sql, /'([^']+)' AS workflow_name/);
      const input = extractSqlString(sql, 'input');
      if (runs.has(runId)) {
        const existing = runs.get(runId)!;
        existing.status = 'running';
        existing.updated_at = now();
      } else {
        runs.set(runId, {
          run_id: runId,
          workflow_name: workflow,
          status: 'running',
          input,
          output: null,
          started_at: now(),
          updated_at: now(),
        });
      }
      return respond([], []);
    }

    // MERGE INTO ..._steps
    if (/MERGE INTO .*_steps/i.test(sql)) {
      const runId = extract(sql, /'([^']+)' AS run_id/);
      const stepKey = extract(sql, /'([^']+)' AS step_key/);
      const status = extract(sql, /'([^']+)' AS status/);
      const key = `${runId}::${stepKey}`;
      if (!steps.has(key)) {
        steps.set(key, {
          run_id: runId,
          step_key: stepKey,
          status,
          output: extractNullableAs(sql, 'output'),
          error: extractNullableAs(sql, 'error'),
          duration_ms: Number(extract(sql, /(\d+) AS duration_ms/) || '0'),
          recorded_at: now(),
        });
      }
      return respond([], []);
    }

    // UPDATE ..._runs SET ...
    if (/^UPDATE .*_runs/i.test(sql)) {
      const runId = extract(sql, /WHERE run_id = '([^']+)'/);
      const run = runs.get(runId);
      if (run) {
        const statusMatch = sql.match(/SET status = '([^']+)'/);
        if (statusMatch) run.status = statusMatch[1];
        const outputMatch = sql.match(/, output = '((?:[^']|'')*)'/);
        if (outputMatch) run.output = outputMatch[1].replace(/''/g, "'");
        run.updated_at = now();
      }
      return respond([], []);
    }

    // SELECT from ..._steps WHERE run_id=... AND step_key=... LIMIT 1
    if (/SELECT step_key, status, output, error, duration_ms, recorded_at FROM .*_steps/i.test(sql)) {
      const runId = extract(sql, /run_id = '([^']+)'/);
      if (/AND step_key = '([^']+)'/.test(sql)) {
        const stepKey = extract(sql, /AND step_key = '([^']+)'/);
        const rec = steps.get(`${runId}::${stepKey}`);
        if (!rec) return respond(['step_key', 'status', 'output', 'error', 'duration_ms', 'recorded_at'], []);
        return respond(
          ['step_key', 'status', 'output', 'error', 'duration_ms', 'recorded_at'],
          [[rec.step_key, rec.status, rec.output, rec.error, String(rec.duration_ms), rec.recorded_at]],
        );
      }
      // All steps for a run
      const rows = Array.from(steps.values())
        .filter((s) => s.run_id === runId)
        .map((s) => [s.step_key, s.status, s.output, s.error, String(s.duration_ms), s.recorded_at]);
      return respond(['step_key', 'status', 'output', 'error', 'duration_ms', 'recorded_at'], rows);
    }

    // SELECT from ..._runs WHERE run_id=...
    if (/SELECT run_id, workflow_name, status, input, output, started_at, updated_at FROM .*_runs/i.test(sql)) {
      const runId = extract(sql, /run_id = '([^']+)'/);
      const run = runs.get(runId);
      if (!run) return respond(['run_id', 'workflow_name', 'status', 'input', 'output', 'started_at', 'updated_at'], []);
      return respond(
        ['run_id', 'workflow_name', 'status', 'input', 'output', 'started_at', 'updated_at'],
        [[run.run_id, run.workflow_name, run.status, run.input, run.output, run.started_at, run.updated_at]],
      );
    }

    // SELECT run_id, workflow_name, status, started_at, updated_at FROM ..._runs (listRuns)
    if (/SELECT run_id, workflow_name, status, started_at, updated_at FROM .*_runs/i.test(sql)) {
      let filtered = Array.from(runs.values());
      const wfMatch = sql.match(/workflow_name = '([^']+)'/);
      if (wfMatch) filtered = filtered.filter((r) => r.workflow_name === wfMatch[1]);
      const statusMatch = sql.match(/status = '([^']+)'/);
      if (statusMatch) filtered = filtered.filter((r) => r.status === statusMatch[1]);
      const limitMatch = sql.match(/LIMIT (\d+)/);
      if (limitMatch) filtered = filtered.slice(0, Number(limitMatch[1]));
      return respond(
        ['run_id', 'workflow_name', 'status', 'started_at', 'updated_at'],
        filtered.map((r) => [r.run_id, r.workflow_name, r.status, r.started_at, r.updated_at]),
      );
    }

    throw new Error(`Unhandled SQL in mock: ${sql.slice(0, 120)}`);
  });

  return { fetchMock, statements, runs, steps };
}

function extract(sql: string, re: RegExp): string {
  const m = sql.match(re);
  return m ? m[1] : '';
}

/**
 * Extract a SQL string literal that precedes the ` AS <alias>` anchor,
 * handling `''`-escaped internal quotes. Returns null if the value is
 * literal `NULL`.
 */
function extractNullableAs(sql: string, alias: string): string | null {
  const re = new RegExp(`((?:NULL)|(?:'(?:[^']|'')*'))\\s+AS ${alias}\\b`);
  const m = sql.match(re);
  if (!m) return null;
  const raw = m[1];
  if (raw === 'NULL') return null;
  return raw.slice(1, -1).replace(/''/g, "'");
}

/**
 * Extract a quoted SQL string that precedes ` AS <alias>`, handling
 * `''`-escaped internal quotes.
 */
function extractSqlString(sql: string, alias: string): string {
  const re = new RegExp(`'((?:[^']|'')*)'\\s+AS\\s+${alias}\\b`);
  const m = sql.match(re);
  if (!m) return '';
  return m[1].replace(/''/g, "'");
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DeltaEngine', () => {
  let engine: DeltaEngine;
  let mock: ReturnType<typeof makeSqlMock>;

  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
    mock = makeSqlMock();
    vi.stubGlobal('fetch', mock.fetchMock);

    engine = new DeltaEngine({
      host: 'https://test-host.databricks.com',
      warehouseId: 'wh-123',
      tablePrefix: 'main.apx_agent.workflow',
    });
  });

  afterEach(() => {
    engine.clearCache();
  });

  // -------------------------------------------------------------------------
  // Bootstrap
  // -------------------------------------------------------------------------

  it('emits CREATE TABLE IF NOT EXISTS for runs and steps on first use', async () => {
    await engine.startRun('wf', {});
    const createStatements = mock.statements.filter((s) => /CREATE TABLE/i.test(s));
    expect(createStatements).toHaveLength(2);
    expect(createStatements.some((s) => /_runs/.test(s))).toBe(true);
    expect(createStatements.some((s) => /_steps/.test(s))).toBe(true);
  });

  it('bootstraps once across multiple calls', async () => {
    await engine.startRun('wf', {});
    await engine.startRun('wf', {});
    await engine.startRun('wf', {});
    const createStatements = mock.statements.filter((s) => /CREATE TABLE/i.test(s));
    expect(createStatements).toHaveLength(2);
  });

  // -------------------------------------------------------------------------
  // startRun
  // -------------------------------------------------------------------------

  it('startRun emits a MERGE into the runs table', async () => {
    await engine.startRun('my-wf', { seed: 1 });
    const merges = mock.statements.filter((s) => /MERGE INTO .*_runs/i.test(s));
    expect(merges).toHaveLength(1);
  });

  it('startRun with existing runId reopens it', async () => {
    const runId = await engine.startRun('wf', {}, { runId: 'custom-id' });
    expect(runId).toBe('custom-id');

    await engine.finishRun(runId, 'paused');
    expect(mock.runs.get('custom-id')?.status).toBe('paused');

    await engine.startRun('wf', {}, { runId });
    expect(mock.runs.get('custom-id')?.status).toBe('running');
  });

  // -------------------------------------------------------------------------
  // step
  // -------------------------------------------------------------------------

  it('step invokes the handler on cache miss and persists the output', async () => {
    const runId = await engine.startRun('wf', {});
    let invocations = 0;

    const result = await engine.step(runId, 'a', async () => {
      invocations++;
      return { value: 42 };
    });

    expect(result).toEqual({ value: 42 });
    expect(invocations).toBe(1);
    const stepMerges = mock.statements.filter((s) => /MERGE INTO .*_steps/i.test(s));
    expect(stepMerges).toHaveLength(1);
  });

  it('step returns cached output without re-invoking within one process', async () => {
    const runId = await engine.startRun('wf', {});
    let invocations = 0;
    const handler = async () => {
      invocations++;
      return { value: invocations };
    };

    const first = await engine.step(runId, 'a', handler);
    const second = await engine.step(runId, 'a', handler);

    expect(first).toEqual({ value: 1 });
    expect(second).toEqual({ value: 1 });
    expect(invocations).toBe(1);
  });

  it('step hits the store cache across engine instances sharing the same tables', async () => {
    // First engine persists the step.
    const runId = await engine.startRun('wf', {}, { runId: 'shared-run' });
    await engine.step(runId, 'a', async () => ({ value: 'persisted' }));

    // Simulate a "second process" — fresh engine, same table prefix, same
    // backing store. The in-process cache is empty, so lookup round-trips
    // through the SQL API.
    const engine2 = new DeltaEngine({
      host: 'https://test-host.databricks.com',
      warehouseId: 'wh-123',
      tablePrefix: 'main.apx_agent.workflow',
    });
    let invocations = 0;
    const result = await engine2.step(runId, 'a', async () => {
      invocations++;
      return { value: 'should-not-run' };
    });

    expect(invocations).toBe(0);
    expect(result).toEqual({ value: 'persisted' });
  });

  it('step persists failures and re-throws StepFailedError on replay', async () => {
    const runId = await engine.startRun('wf', {});

    await expect(
      engine.step(runId, 'a', async () => {
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');

    // Clear in-process cache to force a round-trip for the second call.
    engine.clearCache();
    await expect(
      engine.step(runId, 'a', async () => 'unused'),
    ).rejects.toBeInstanceOf(StepFailedError);
  });

  // -------------------------------------------------------------------------
  // finishRun / getRun / listRuns
  // -------------------------------------------------------------------------

  it('finishRun updates status and output', async () => {
    const runId = await engine.startRun('wf', {});
    await engine.finishRun(runId, 'completed', { final: true });

    const snapshot = await engine.getRun(runId);
    expect(snapshot?.status).toBe('completed');
    expect(snapshot?.output).toEqual({ final: true });
  });

  it('getRun returns the full snapshot with step records', async () => {
    const runId = await engine.startRun('wf', { seed: 1 });
    await engine.step(runId, 'a', async () => 'first');
    await engine.step(runId, 'b', async () => 'second');

    const snapshot = await engine.getRun(runId);
    expect(snapshot?.runId).toBe(runId);
    expect(snapshot?.workflowName).toBe('wf');
    expect(snapshot?.steps.map((s) => s.stepKey).sort()).toEqual(['a', 'b']);
  });

  it('getRun returns null for unknown runId', async () => {
    expect(await engine.getRun('ghost')).toBeNull();
  });

  it('listRuns filters by workflowName and status', async () => {
    await engine.startRun('wf-a', {}, { runId: 'r1' });
    await engine.startRun('wf-b', {}, { runId: 'r2' });
    await engine.finishRun('r1', 'completed');

    const completed = await engine.listRuns({ status: 'completed' });
    expect(completed.map((r) => r.runId)).toEqual(['r1']);

    const wfB = await engine.listRuns({ workflowName: 'wf-b' });
    expect(wfB.map((r) => r.runId)).toEqual(['r2']);
  });

  // -------------------------------------------------------------------------
  // SQL safety
  // -------------------------------------------------------------------------

  it("escapes single quotes in run input", async () => {
    const runId = await engine.startRun('wf', { note: "it's fine" });
    const snapshot = await engine.getRun(runId);
    expect(snapshot?.input).toEqual({ note: "it's fine" });
  });
});

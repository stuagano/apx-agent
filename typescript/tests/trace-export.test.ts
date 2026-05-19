/**
 * Tests for trace-export.ts — MLflow traces → Delta table.
 *
 * Mirrors python/tests/test_trace_export.py.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  _normaliseTrace,
  exportTraces,
  escapeSql,
} from '../src/trace-export.js';
import type {
  ExportResult,
  RawTrace,
  SqlExecutor,
  TraceSearch,
} from '../src/trace-export.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTraceSearch(traces: RawTrace[]): {
  search: TraceSearch;
  spy: ReturnType<typeof vi.fn>;
} {
  const spy = vi.fn().mockResolvedValue(traces);
  return { search: spy as unknown as TraceSearch, spy };
}

function makeSqlExecutor(): {
  exec: SqlExecutor;
  spy: ReturnType<typeof vi.fn>;
  calls: () => string[];
} {
  const spy = vi.fn().mockResolvedValue(undefined);
  return {
    exec: spy as unknown as SqlExecutor,
    spy,
    calls: () => spy.mock.calls.map((c: unknown[]) => c[0] as string),
  };
}

// ---------------------------------------------------------------------------
// _normaliseTrace
// ---------------------------------------------------------------------------

describe('_normaliseTrace', () => {
  it('normalises DataFrame row with full apx.* attrs', () => {
    const row = {
      trace_id: 'tr-1',
      experiment_id: 'exp-1',
      tags: {
        'apx.agent.name': 'triage',
        'apx.operation': 'predict',
        'apx.session.id': 'user:alice:thread-1',
        'apx.user.token_provided': true,
        'apx.model.endpoint': 'databricks-claude-sonnet-4-6',
        'apx.tools.count': 5,
        'apx.watchdog.action': 'allow',
      },
      status: 'OK',
      timestamp_ms: 1700000000000,
      execution_time_ms: 1234,
    };
    const norm = _normaliseTrace(row);
    expect(norm).not.toBeNull();
    expect(norm!.trace_id).toBe('tr-1');
    expect(norm!.agent_name).toBe('triage');
    expect(norm!.operation).toBe('predict');
    expect(norm!.session_id).toBe('user:alice:thread-1');
    expect(norm!.user_token_provided).toBe(true);
    expect(norm!.model_endpoint).toBe('databricks-claude-sonnet-4-6');
    expect(norm!.tool_count).toBe(5);
    expect(norm!.watchdog_action).toBe('allow');
    expect(norm!.status).toBe('OK');
    expect(norm!.start_time_ms).toBe(1700000000000);
    expect(norm!.execution_time_ms).toBe(1234);
    // Tags JSON keeps only apx.* keys
    const tagsParsed = JSON.parse(norm!.tags) as Record<string, unknown>;
    expect(tagsParsed['apx.agent.name']).toBe('triage');
  });

  it('normalises Trace object (apx attrs from root span)', () => {
    const span = {
      attributes: {
        'apx.agent.name': 'billing',
        'apx.operation': 'tool_call',
      },
    };
    const info = {
      trace_id: 'tr-2',
      experiment_id: 'exp-1',
      status: 'OK',
      timestamp_ms: 1700000001000,
      execution_time_ms: 42,
      tags: {},
    };
    const trace = { info, data: { spans: [span] } };
    const norm = _normaliseTrace(trace);
    expect(norm).not.toBeNull();
    expect(norm!.trace_id).toBe('tr-2');
    expect(norm!.agent_name).toBe('billing');
    expect(norm!.operation).toBe('tool_call');
  });

  it('span attrs win over trace tags', () => {
    const span = {
      attributes: { 'apx.agent.name': 'span-win' },
    };
    const info = {
      trace_id: 'tr-3',
      experiment_id: 'exp-1',
      status: 'OK',
      tags: { 'apx.agent.name': 'tag-lose', 'apx.session.id': 'sess-1' },
    };
    const trace = { info, data: { spans: [span] } };
    const norm = _normaliseTrace(trace);
    expect(norm!.agent_name).toBe('span-win');
    // Tag-only attrs still flow through
    expect(norm!.session_id).toBe('sess-1');
  });

  it('returns null when no trace_id (dict)', () => {
    expect(_normaliseTrace({ tags: {} })).toBeNull();
  });

  it('returns null when info is null (object)', () => {
    expect(_normaliseTrace({ info: null })).toBeNull();
  });

  it('returns null when object info has no trace_id', () => {
    const trace = {
      info: { experiment_id: 'exp', status: 'OK', tags: {} },
      data: { spans: [] },
    };
    expect(_normaliseTrace(trace)).toBeNull();
  });

  it('falls back to request_id when trace_id missing (dict)', () => {
    const row = { request_id: 'req-9', tags: {}, status: 'OK' };
    const norm = _normaliseTrace(row);
    expect(norm).not.toBeNull();
    expect(norm!.trace_id).toBe('req-9');
  });
});

// ---------------------------------------------------------------------------
// escapeSql
// ---------------------------------------------------------------------------

describe('escapeSql', () => {
  it('null becomes NULL literal', () => {
    expect(escapeSql(null)).toBe('NULL');
    expect(escapeSql(undefined)).toBe('NULL');
  });

  it('doubles embedded single quotes', () => {
    expect(escapeSql("user's-id")).toBe("'user''s-id'");
  });

  it('wraps plain strings in single quotes', () => {
    expect(escapeSql('tr-1')).toBe("'tr-1'");
  });
});

// ---------------------------------------------------------------------------
// exportTraces — validation
// ---------------------------------------------------------------------------

describe('exportTraces - argument validation', () => {
  it('rejects target_table that is not three-part', async () => {
    const { search } = makeTraceSearch([]);
    const { exec } = makeSqlExecutor();
    await expect(
      exportTraces({
        experimentName: 'x',
        targetTable: 'not.three',
        traceSearch: search,
        sqlExecutor: exec,
      }),
    ).rejects.toThrow(/three-part/);
  });

  it('rejects empty experimentName', async () => {
    const { search } = makeTraceSearch([]);
    const { exec } = makeSqlExecutor();
    await expect(
      exportTraces({
        experimentName: '',
        targetTable: 'main.x.traces',
        traceSearch: search,
        sqlExecutor: exec,
      }),
    ).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// exportTraces — DDL behaviour
// ---------------------------------------------------------------------------

describe('exportTraces - auto-create DDL', () => {
  it('runs CREATE TABLE when autoCreate=true', async () => {
    const { search } = makeTraceSearch([]);
    const { exec, calls } = makeSqlExecutor();

    await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
    });

    const createCalls = calls().filter((sql) => sql.includes('CREATE TABLE IF NOT EXISTS'));
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0]).toContain('main.x.traces');
  });

  it('skips CREATE TABLE when autoCreate=false', async () => {
    const { search } = makeTraceSearch([]);
    const { exec, calls } = makeSqlExecutor();

    await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    const createCalls = calls().filter((sql) => sql.includes('CREATE TABLE'));
    expect(createCalls).toEqual([]);
  });

  it('tolerates CREATE TABLE failure with a warning', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const { search } = makeTraceSearch([]);
      // First call (DDL) throws; subsequent calls (none expected) resolve.
      const spy = vi
        .fn()
        .mockImplementationOnce(() => Promise.reject(new Error('no grant')))
        .mockResolvedValue(undefined);
      const exec = spy as unknown as SqlExecutor;

      const result = await exportTraces({
        experimentName: 'exp',
        targetTable: 'main.x.traces',
        traceSearch: search,
        sqlExecutor: exec,
      });

      expect(result.rowsWritten).toBe(0);
      expect(warnSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// exportTraces — MERGE behaviour
// ---------------------------------------------------------------------------

describe('exportTraces - MERGE write', () => {
  it('writes MERGE with normalised rows', async () => {
    const traces: RawTrace[] = [
      {
        trace_id: 'tr-1',
        tags: { 'apx.agent.name': 'triage', 'apx.operation': 'predict' },
        status: 'OK',
        execution_time_ms: 100,
      },
      {
        trace_id: 'tr-2',
        tags: { 'apx.agent.name': 'billing' },
        status: 'OK',
        execution_time_ms: 50,
      },
    ];
    const { search } = makeTraceSearch(traces);
    const { exec, calls } = makeSqlExecutor();

    const result = await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    expect(result.rowsWritten).toBe(2);
    expect(result.skipped).toBe(0);

    const mergeCalls = calls().filter((sql) => sql.includes('MERGE INTO main.x.traces'));
    expect(mergeCalls).toHaveLength(1);
    const sql = mergeCalls[0];
    expect(sql).toContain('tr-1');
    expect(sql).toContain('tr-2');
    expect(sql).toContain('triage');
    expect(sql).toContain('billing');
  });

  it('returns rowsWritten=0 on SQL failure (no throw)', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const traces: RawTrace[] = [{ trace_id: 'tr-1', tags: {}, status: 'OK' }];
      const { search } = makeTraceSearch(traces);

      const exec = vi
        .fn()
        .mockImplementation((sql: string) => {
          if (sql.includes('MERGE')) {
            return Promise.reject(new Error('merge denied'));
          }
          return Promise.resolve();
        }) as unknown as SqlExecutor;

      const result = await exportTraces({
        experimentName: 'exp',
        targetTable: 'main.x.traces',
        traceSearch: search,
        sqlExecutor: exec,
        autoCreate: false,
      });

      expect(result.rowsWritten).toBe(0);
      expect(result.tracesPulled).toBe(1);
      expect(warnSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
    }
  });

  it('counts traces without trace_id as skipped', async () => {
    const traces: RawTrace[] = [
      { tags: {} }, // no trace_id — should skip
      { trace_id: 'tr-2', tags: { 'apx.agent.name': 'x' }, status: 'OK' },
    ];
    const { search } = makeTraceSearch(traces);
    const { exec } = makeSqlExecutor();

    const result = await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    expect(result.skipped).toBe(1);
    expect(result.rowsWritten).toBe(1);
    expect(result.tracesPulled).toBe(2);
  });

  it('all-skipped run writes no MERGE and returns rowsWritten=0', async () => {
    const traces: RawTrace[] = [{ tags: {} }, { tags: {} }];
    const { search } = makeTraceSearch(traces);
    const { exec, calls } = makeSqlExecutor();

    const result = await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    expect(result.skipped).toBe(2);
    expect(result.rowsWritten).toBe(0);
    expect(result.tracesPulled).toBe(2);
    const mergeCalls = calls().filter((sql) => sql.includes('MERGE'));
    expect(mergeCalls).toEqual([]);
  });

  it('MERGE escapes single quotes in apx attr values', async () => {
    const traces: RawTrace[] = [
      {
        trace_id: "tr-'1",
        tags: { 'apx.agent.name': "user's-agent" },
        status: 'OK',
      },
    ];
    const { search } = makeTraceSearch(traces);
    const { exec, calls } = makeSqlExecutor();

    await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    const mergeCalls = calls().filter((sql) => sql.includes('MERGE'));
    expect(mergeCalls[0]).toContain("tr-''1");
    expect(mergeCalls[0]).toContain("user''s-agent");
  });
});

// ---------------------------------------------------------------------------
// ExportResult shape
// ---------------------------------------------------------------------------

describe('ExportResult', () => {
  it('is frozen / readonly', async () => {
    const { search } = makeTraceSearch([]);
    const { exec } = makeSqlExecutor();
    const result = await exportTraces({
      experimentName: 'exp',
      targetTable: 'main.x.traces',
      traceSearch: search,
      sqlExecutor: exec,
      autoCreate: false,
    });

    expect(Object.isFrozen(result)).toBe(true);
    expect(() => {
      // @ts-expect-error — readonly
      (result as ExportResult).rowsWritten = 99;
    }).toThrow();
  });
});

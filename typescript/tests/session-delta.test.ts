/**
 * Tests for DeltaSessionStore — UC Delta-backed session persistence.
 *
 * Mirrors python/tests/test_session.py (Delta portion). Uses injectable
 * query/exec executors so the SQL the store emits is captured directly.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { DeltaSessionStore } from '../src/session-delta.js';
import type { SessionSnapshot } from '../src/workflows/session.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface Captured {
  queries: string[];
  execs: string[];
  queryFn: ReturnType<typeof vi.fn>;
  execFn: ReturnType<typeof vi.fn>;
}

function captureSql(opts?: {
  selectRows?: Array<Record<string, unknown>>;
  queryThrows?: unknown;
  execThrows?: unknown;
}): Captured {
  const queries: string[] = [];
  const execs: string[] = [];

  const queryFn = vi.fn(async (sql: string) => {
    queries.push(sql);
    if (opts?.queryThrows) throw opts.queryThrows;
    return opts?.selectRows ?? [];
  });

  const execFn = vi.fn(async (sql: string) => {
    execs.push(sql);
    if (opts?.execThrows) throw opts.execThrows;
  });

  return { queries, execs, queryFn, execFn };
}

function makeSnapshot(id: string, overrides?: Partial<SessionSnapshot>): SessionSnapshot {
  const now = new Date().toISOString();
  return {
    id,
    messages: [],
    state: {},
    createdAt: now,
    updatedAt: now,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Constructor validation
// ---------------------------------------------------------------------------

describe('DeltaSessionStore — construction', () => {
  it('rejects a non-three-part table path', () => {
    const { queryFn, execFn } = captureSql();
    expect(
      () =>
        new DeltaSessionStore({
          tablePath: 'main.sessions',
          querySql: queryFn,
          execSql: execFn,
        }),
    ).toThrow(/three-part UC name/);
  });

  it('accepts a valid three-part table path', () => {
    const { queryFn, execFn } = captureSql();
    expect(
      () =>
        new DeltaSessionStore({
          tablePath: 'main.agents.sessions',
          querySql: queryFn,
          execSql: execFn,
        }),
    ).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// load() — missing, parsing, failure
// ---------------------------------------------------------------------------

describe('DeltaSessionStore.load', () => {
  it('returns null when no rows match', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.load('nope')).toBeNull();
  });

  it('parses history and state JSON correctly', async () => {
    const row = {
      session_id: 'x',
      history: JSON.stringify([{ role: 'user', content: 'hi' }]),
      state: JSON.stringify({ prefs: { language: 'en' } }),
      created_at: 1700000000.0,
      updated_at: 1700000100.0,
    };
    const { queryFn, execFn } = captureSql({ selectRows: [row] });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const snap = await store.load('x');
    expect(snap).not.toBeNull();
    expect(snap!.id).toBe('x');
    expect(snap!.messages).toEqual([{ role: 'user', content: 'hi' }]);
    expect(snap!.state).toEqual({ prefs: { language: 'en' } });
    expect(snap!.createdAt).toBe(new Date(1700000000000).toISOString());
    expect(snap!.updatedAt).toBe(new Date(1700000100000).toISOString());
  });

  it('returns null on a SQL exception', async () => {
    const { queryFn, execFn } = captureSql({ queryThrows: new Error('boom') });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.load('x')).toBeNull();
    warn.mockRestore();
  });

  it('degrades to empty session on malformed JSON', async () => {
    const row = {
      session_id: 'x',
      history: 'not-json[',
      state: '{',
      created_at: 0,
      updated_at: 0,
    };
    const { queryFn, execFn } = captureSql({ selectRows: [row] });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const snap = await store.load('x');
    expect(snap).not.toBeNull();
    expect(snap!.messages).toEqual([]);
    expect(snap!.state).toEqual({});
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// save() — MERGE, escaping
// ---------------------------------------------------------------------------

describe('DeltaSessionStore.save', () => {
  it('issues a MERGE INTO ... WHEN MATCHED / WHEN NOT MATCHED statement', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.save(
      'x',
      makeSnapshot('x', { messages: [{ role: 'user', content: 'hello' }], state: { lang: 'en' } }),
    );
    expect(execFn).toHaveBeenCalledTimes(1);
    const sql = execs[0]!;
    expect(sql).toContain('MERGE INTO main.x.sessions');
    expect(sql).toContain('WHEN MATCHED');
    expect(sql).toContain('WHEN NOT MATCHED');
  });

  it('escapes single quotes in session_id, history, and state', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.save(
      "user's session",
      makeSnapshot("user's session", {
        messages: [{ role: 'user', content: "don't worry" }],
        state: { key: "O'Hara" },
      }),
    );
    const sql = execs[0]!;
    expect(sql).toContain("user''s session");
    expect(sql).toContain("don''t worry");
    expect(sql).toContain("O''Hara");
  });

  it('round-trips a payload through a stubbed Delta table', async () => {
    // Simulate a tiny in-memory Delta: latest save's row is what load returns.
    let stored: Record<string, unknown> | null = null;
    const queryFn = vi.fn(async () => (stored ? [stored] : []));
    const execFn = vi.fn(async (sql: string) => {
      if (sql.includes('MERGE INTO')) {
        // Extract session_id, history, state by looking for the inline SELECT clauses.
        const re = /USING \(SELECT\s+'((?:[^']|'')*)' AS session_id,\s+'((?:[^']|'')*)' AS history,\s+'((?:[^']|'')*)' AS state,\s+([\d.eE+-]+) AS created_at,\s+([\d.eE+-]+) AS updated_at/;
        const match = sql.match(re);
        if (!match) throw new Error('SQL extract failed: ' + sql);
        const unesc = (s: string) => s.replace(/''/g, "'");
        stored = {
          session_id: unesc(match[1]!),
          history: unesc(match[2]!),
          state: unesc(match[3]!),
          created_at: parseFloat(match[4]!),
          updated_at: parseFloat(match[5]!),
        };
      } else if (sql.startsWith('DELETE FROM')) {
        stored = null;
      }
    });

    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });

    const original = makeSnapshot('x', {
      messages: [{ role: 'user', content: 'hi' }],
      state: { v: 1 },
    });
    await store.save('x', original);
    const loaded = await store.load('x');
    expect(loaded).not.toBeNull();
    expect(loaded!.id).toBe('x');
    expect(loaded!.messages).toEqual([{ role: 'user', content: 'hi' }]);
    expect(loaded!.state).toEqual({ v: 1 });

    // Second save overwrites (upsert behaviour).
    await store.save('x', makeSnapshot('x', { messages: [{ role: 'user', content: 'second' }] }));
    const loaded2 = await store.load('x');
    expect(loaded2!.messages).toEqual([{ role: 'user', content: 'second' }]);
  });

  it('persists updated_at into the MERGE source clause', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const iso = '2026-05-19T12:00:00.000Z';
    await store.save('x', makeSnapshot('x', { updatedAt: iso }));
    const expectedEpoch = Date.parse(iso) / 1000;
    expect(execs[0]!).toContain(`${expectedEpoch} AS updated_at`);
  });
});

// ---------------------------------------------------------------------------
// delete()
// ---------------------------------------------------------------------------

describe('DeltaSessionStore.delete', () => {
  it('issues DELETE FROM ... WHERE session_id = ?', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.delete('x');
    const sql = execs[0]!;
    expect(sql.startsWith('DELETE FROM main.x.sessions')).toBe(true);
    expect(sql).toContain("session_id = 'x'");
  });

  it('logs and continues on failure', async () => {
    const { queryFn } = captureSql();
    const execFn = vi.fn(async () => {
      throw new Error('boom');
    });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(store.delete('x')).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// Apostrophes / unicode round-trip
// ---------------------------------------------------------------------------

describe('DeltaSessionStore — special characters', () => {
  it('round-trips apostrophes and unicode through the SQL escape', async () => {
    let stored: Record<string, unknown> | null = null;
    const queryFn = vi.fn(async () => (stored ? [stored] : []));
    const execFn = vi.fn(async (sql: string) => {
      if (sql.includes('MERGE INTO')) {
        const re = /USING \(SELECT\s+'((?:[^']|'')*)' AS session_id,\s+'((?:[^']|'')*)' AS history,\s+'((?:[^']|'')*)' AS state,\s+([\d.eE+-]+) AS created_at,\s+([\d.eE+-]+) AS updated_at/;
        const match = sql.match(re);
        if (!match) throw new Error('SQL extract failed: ' + sql);
        const unesc = (s: string) => s.replace(/''/g, "'");
        stored = {
          session_id: unesc(match[1]!),
          history: unesc(match[2]!),
          state: unesc(match[3]!),
          created_at: parseFloat(match[4]!),
          updated_at: parseFloat(match[5]!),
        };
      }
    });

    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });

    const id = "user's session";
    await store.save(
      id,
      makeSnapshot(id, {
        messages: [{ role: 'user', content: "don't worry — résumé" }],
        state: { key: "O'Hara" },
      }),
    );
    const loaded = await store.load(id);
    expect(loaded).not.toBeNull();
    expect(loaded!.messages[0]!.content).toBe("don't worry — résumé");
    expect(loaded!.state).toEqual({ key: "O'Hara" });
  });
});

// ---------------------------------------------------------------------------
// list()
// ---------------------------------------------------------------------------

describe('DeltaSessionStore.list', () => {
  it('returns session_ids from the table', async () => {
    const rows = [{ session_id: 'a' }, { session_id: 'b' }];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.list()).toEqual(['a', 'b']);
  });

  it('returns [] and logs when the query throws', async () => {
    const { queryFn, execFn } = captureSql({ queryThrows: new Error('boom') });
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list()).toEqual([]);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// auto-create behaviour
// ---------------------------------------------------------------------------

describe('DeltaSessionStore — auto-create', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('runs CREATE TABLE on first use', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
    });
    await store.load('x');
    const createCalls = execs.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0]!).toContain('CREATE TABLE IF NOT EXISTS main.x.sessions');
  });

  it('runs CREATE TABLE only once across multiple operations', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
    });
    await store.load('x');
    await store.load('y');
    await store.delete('z');
    const createCalls = execs.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toHaveLength(1);
  });

  it('does not run CREATE TABLE when autoCreate=false', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaSessionStore({
      tablePath: 'main.x.sessions',
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.load('x');
    const createCalls = execs.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toEqual([]);
  });
});

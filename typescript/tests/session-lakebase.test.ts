/**
 * Tests for LakebaseSessionStore — Postgres-backed session persistence.
 *
 * Uses a tiny in-memory dict as a stand-in for a Postgres backend. The store
 * emits `INSERT ... ON CONFLICT (session_id) DO UPDATE` upserts, so the stub
 * just simulates that semantics.
 *
 * Mirrors python/tests/test_session_lakebase.py.
 */

import { describe, it, expect, vi } from 'vitest';
import { LakebaseSessionStore } from '../src/session-lakebase.js';
import type {
  PgExecExecutor,
  PgQueryExecutor,
} from '../src/session-lakebase.js';
import type { SessionSnapshot } from '../src/workflows/session.js';

// ---------------------------------------------------------------------------
// In-memory Postgres-like stub
// ---------------------------------------------------------------------------

interface Row {
  session_id: string;
  history: string;
  state: string;
  created_at: number;
  updated_at: number;
}

interface FakeDB {
  rows: Map<string, Row>;
  tableCreated: boolean;
  createCalls: number;
  failNext: boolean;
  querySql: PgQueryExecutor;
  execSql: PgExecExecutor;
}

function makeFakeDb(): FakeDB {
  const db: FakeDB = {
    rows: new Map(),
    tableCreated: false,
    createCalls: 0,
    failNext: false,
    querySql: async () => [],
    execSql: async () => {},
  };

  db.querySql = async (sql, params) => {
    if (db.failNext) {
      db.failNext = false;
      throw new Error('simulated SQL failure');
    }
    if (!db.tableCreated) throw new Error('table not created');
    if (sql.startsWith('SELECT session_id FROM')) {
      return Array.from(db.rows.values()).map((r) => ({ session_id: r.session_id }));
    }
    if (sql.startsWith('SELECT session_id, history, state')) {
      const sid = String(params.sid);
      const row = db.rows.get(sid);
      return row ? [{ ...row }] : [];
    }
    throw new Error('unrecognized query: ' + sql);
  };

  db.execSql = async (sql, params) => {
    if (sql.startsWith('CREATE TABLE')) {
      db.createCalls += 1;
      db.tableCreated = true;
      return;
    }
    if (!db.tableCreated) throw new Error('table not created');
    if (sql.startsWith('INSERT INTO')) {
      const row: Row = {
        session_id: String(params.sid),
        history: String(params.history),
        state: String(params.state),
        created_at: Number(params.created_at),
        updated_at: Number(params.updated_at),
      };
      db.rows.set(row.session_id, row);
      return;
    }
    if (sql.startsWith('DELETE FROM')) {
      db.rows.delete(String(params.sid));
      return;
    }
    throw new Error('unrecognized exec: ' + sql);
  };

  return db;
}

function makeStore(db: FakeDB, autoCreate = true): LakebaseSessionStore {
  return new LakebaseSessionStore({
    querySql: db.querySql,
    execSql: db.execSql,
    tableName: 'apx_sessions',
    autoCreate,
  });
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
// get / put / delete
// ---------------------------------------------------------------------------

describe('LakebaseSessionStore — basic CRUD', () => {
  it('load returns null on missing session', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(await store.load('nope')).toBeNull();
  });

  it('save then load round-trips', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const snap = makeSnapshot('x', {
      messages: [{ role: 'user', content: 'hi' }],
      state: { prefs: { language: 'en' } },
    });
    await store.save('x', snap);
    const got = await store.load('x');
    expect(got).not.toBeNull();
    expect(got!.id).toBe('x');
    expect(got!.messages).toEqual([{ role: 'user', content: 'hi' }]);
    expect(got!.state).toEqual({ prefs: { language: 'en' } });
  });

  it('save upserts on session_id collision', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.save(
      'x',
      makeSnapshot('x', { messages: [{ role: 'user', content: 'first' }] }),
    );
    await store.save(
      'x',
      makeSnapshot('x', { messages: [{ role: 'user', content: 'second' }] }),
    );
    const got = await store.load('x');
    expect(got!.messages).toEqual([{ role: 'user', content: 'second' }]);
  });

  it('saves the updatedAt from the snapshot', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const iso = '2026-05-19T12:00:00.000Z';
    await store.save('x', makeSnapshot('x', { updatedAt: iso }));
    const got = await store.load('x');
    expect(got!.updatedAt).toBe(iso);
  });

  it('delete removes the row and tolerates missing', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.save('x', makeSnapshot('x', { messages: [{ role: 'user', content: 'hi' }] }));
    await store.delete('x');
    expect(await store.load('x')).toBeNull();
    // No-op on missing id
    await expect(store.delete('not-there')).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// JSON round-trip with special characters
// ---------------------------------------------------------------------------

describe('LakebaseSessionStore — special characters', () => {
  it('round-trips apostrophes and unicode', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const id = "user's session";
    await store.save(
      id,
      makeSnapshot(id, {
        messages: [{ role: 'user', content: "don't worry — résumé" }],
        state: { key: "O'Hara" },
      }),
    );
    const got = await store.load(id);
    expect(got).not.toBeNull();
    expect(got!.messages[0]!.content).toBe("don't worry — résumé");
    expect(got!.state).toEqual({ key: "O'Hara" });
  });
});

// ---------------------------------------------------------------------------
// Failure modes
// ---------------------------------------------------------------------------

describe('LakebaseSessionStore — failure modes', () => {
  it('load returns null on a SQL exception', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    // Force the next query to throw — auto-create has already succeeded by
    // the time we reach this point because makeStore runs lazily.
    await store.save('x', makeSnapshot('x'));
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.load('x')).toBeNull();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('load returns empty session on malformed JSON', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    // Trigger CREATE first via a no-op save.
    await store.save('warmup', makeSnapshot('warmup'));
    // Insert a corrupted row directly.
    db.rows.set('x', {
      session_id: 'x',
      history: 'not-json[',
      state: '{',
      created_at: 0,
      updated_at: 0,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const got = await store.load('x');
    expect(got).not.toBeNull();
    expect(got!.messages).toEqual([]);
    expect(got!.state).toEqual({});
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('delete logs and continues on failure', async () => {
    const db = makeFakeDb();
    const failingExec: PgExecExecutor = async (sql) => {
      if (sql.startsWith('CREATE TABLE')) return;
      throw new Error('boom');
    };
    const store = new LakebaseSessionStore({
      querySql: db.querySql,
      execSql: failingExec,
      tableName: 'apx_sessions',
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(store.delete('x')).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('LakebaseSessionStore.list', () => {
  it('returns all stored session_ids', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.save('a', makeSnapshot('a'));
    await store.save('b', makeSnapshot('b'));
    const ids = await store.list();
    expect(ids.sort()).toEqual(['a', 'b']);
  });

  it('returns [] when the query throws', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.save('warmup', makeSnapshot('warmup'));
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list()).toEqual([]);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// auto-create
// ---------------------------------------------------------------------------

describe('LakebaseSessionStore — auto-create', () => {
  it('runs CREATE TABLE on first use and only once', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.load('x');
    await store.load('y');
    await store.save('z', makeSnapshot('z'));
    expect(db.createCalls).toBe(1);
  });

  it('does not run CREATE TABLE when autoCreate=false', async () => {
    const db = makeFakeDb();
    const store = makeStore(db, false);
    // Pre-flag the DB as if a platform team had provisioned it.
    db.tableCreated = true;
    await store.load('x');
    expect(db.createCalls).toBe(0);
  });

  it('emits the expected CREATE TABLE DDL', async () => {
    const captured: string[] = [];
    const execFn: PgExecExecutor = async (sql) => {
      captured.push(sql);
    };
    const queryFn: PgQueryExecutor = async () => [];
    const store = new LakebaseSessionStore({
      querySql: queryFn,
      execSql: execFn,
      tableName: 'apx_sessions',
    });
    await store.load('x');
    const ddl = captured.find((s) => s.startsWith('CREATE TABLE'));
    expect(ddl).toBeDefined();
    expect(ddl!).toContain('CREATE TABLE IF NOT EXISTS apx_sessions');
    expect(ddl!).toContain('session_id TEXT PRIMARY KEY');
    expect(ddl!).toContain('history TEXT NOT NULL');
    expect(ddl!).toContain('state TEXT NOT NULL');
    expect(ddl!).toContain('created_at DOUBLE PRECISION');
    expect(ddl!).toContain('updated_at DOUBLE PRECISION');
  });

  it('save emits ON CONFLICT (session_id) DO UPDATE', async () => {
    const captured: string[] = [];
    const execFn: PgExecExecutor = async (sql) => {
      captured.push(sql);
    };
    const queryFn: PgQueryExecutor = async () => [];
    const store = new LakebaseSessionStore({
      querySql: queryFn,
      execSql: execFn,
      tableName: 'apx_sessions',
      autoCreate: false,
    });
    await store.save('x', makeSnapshot('x'));
    const insert = captured.find((s) => s.startsWith('INSERT INTO'));
    expect(insert).toBeDefined();
    expect(insert!).toContain('ON CONFLICT (session_id) DO UPDATE');
    expect(insert!).toContain('EXCLUDED.history');
  });
});

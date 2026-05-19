/**
 * Tests for LakebaseMemoryStore — pgvector-backed `MemoryStore`.
 *
 * Same mock-executor style as `session-lakebase.test.ts` — a tiny in-memory
 * Postgres-like stub records (sql, params) tuples and serves SELECTs from a
 * Map. Vector recall is simulated client-side using cosine similarity over
 * the stored embedding text-literal.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  LakebaseMemoryStore,
  tagsLiteral,
  vectorLiteral,
  type PgExecExecutor,
  type PgQueryExecutor,
} from '../src/memory-lakebase.js';
import type { EmbeddingFn, MemoryInput } from '../src/memory.js';

// ---------------------------------------------------------------------------
// Toy embedder — deterministic 4-dim hash so we can reason about recall order
// ---------------------------------------------------------------------------

const DIM = 4;

function toyEmbedder(): EmbeddingFn {
  return async (texts) =>
    texts.map((t) => {
      const vec = new Array(DIM).fill(0);
      for (let i = 0; i < t.length; i++) {
        vec[i % DIM] += t.charCodeAt(i);
      }
      const norm = Math.sqrt(vec.reduce((a, b) => a + b * b, 0)) || 1;
      return vec.map((v) => v / norm);
    });
}

// ---------------------------------------------------------------------------
// In-memory Postgres-like stub
// ---------------------------------------------------------------------------

interface PgRow {
  id: string;
  principal_id: string;
  namespace: string;
  content: string;
  tags: string; // raw '{...}' text literal as it would land on the wire
  importance: number;
  embedding: string; // raw '[...]' text literal
  metadata: string; // JSON string
  created_at: number;
  updated_at: number;
}

interface FakeDB {
  rows: Map<string, PgRow>;
  tableCreated: boolean;
  extensionCreated: boolean;
  createTableCalls: number;
  createExtensionCalls: number;
  createIndexCalls: number;
  failNext: boolean;
  execLog: Array<{ sql: string; params: readonly unknown[] }>;
  queryLog: Array<{ sql: string; params: readonly unknown[] }>;
  querySql: PgQueryExecutor;
  execSql: PgExecExecutor;
}

function parseVector(s: string): number[] {
  const inner = s.replace(/^\[/, '').replace(/\]$/, '');
  if (!inner) return [];
  return inner.split(',').map((p) => Number(p));
}

function cosineSim(a: readonly number[], b: readonly number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += (a[i] ?? 0) * (b[i] ?? 0);
    na += (a[i] ?? 0) ** 2;
    nb += (b[i] ?? 0) ** 2;
  }
  if (na === 0 || nb === 0) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

function makeFakeDb(): FakeDB {
  const db: FakeDB = {
    rows: new Map(),
    tableCreated: false,
    extensionCreated: false,
    createTableCalls: 0,
    createExtensionCalls: 0,
    createIndexCalls: 0,
    failNext: false,
    execLog: [],
    queryLog: [],
    querySql: async () => [],
    execSql: async () => {},
  };

  db.querySql = async (sql, params) => {
    db.queryLog.push({ sql, params });
    if (db.failNext) {
      db.failNext = false;
      throw new Error('simulated SQL failure');
    }
    // SELECT for get by id
    if (sql.startsWith('SELECT id, principal_id') && sql.includes('WHERE id = $1')) {
      const row = db.rows.get(String(params[0]));
      return row ? [{ ...row }] : [];
    }
    // SELECT for list / recall
    if (sql.startsWith('SELECT id, principal_id') && sql.includes('WHERE principal_id =')) {
      const isRecall = sql.includes('embedding <=> $1::vector');
      if (isRecall) {
        const qvec = parseVector(String(params[0]));
        const pid = String(params[1]);
        const ns = params[2] as string | null;
        const tagsRaw = params[3] as string | null;
        const minImp = params[4] as number | null;
        const k = Number(params[5]);
        const filterTags = tagsRaw ? parseTagSet(tagsRaw) : null;
        const scored = Array.from(db.rows.values())
          .filter((r) => r.principal_id === pid)
          .filter((r) => ns === null || r.namespace === ns)
          .filter((r) => filterTags === null || arrayOverlap(parseTagSet(r.tags), filterTags))
          .filter((r) => minImp === null || r.importance >= minImp)
          .filter((r) => r.embedding && r.embedding.length > 0)
          .map((r) => {
            const dist = 1 - cosineSim(qvec, parseVector(r.embedding));
            return { row: r, dist };
          })
          .sort((a, b) => a.dist - b.dist)
          .slice(0, k)
          .map((x) => ({ ...x.row, score: 1 - x.dist }));
        return scored;
      } else {
        const pid = String(params[0]);
        const ns = params[1] as string | null;
        const tagsRaw = params[2] as string | null;
        const minImp = params[3] as number | null;
        const limit = Number(params[4]);
        const filterTags = tagsRaw ? parseTagSet(tagsRaw) : null;
        return Array.from(db.rows.values())
          .filter((r) => r.principal_id === pid)
          .filter((r) => ns === null || r.namespace === ns)
          .filter((r) => filterTags === null || arrayOverlap(parseTagSet(r.tags), filterTags))
          .filter((r) => minImp === null || r.importance >= minImp)
          .sort((a, b) => b.updated_at - a.updated_at)
          .slice(0, limit)
          .map((r) => ({ ...r }));
      }
    }
    throw new Error('unrecognized query: ' + sql);
  };

  db.execSql = async (sql, params) => {
    db.execLog.push({ sql, params });
    if (sql.startsWith('CREATE EXTENSION')) {
      db.createExtensionCalls += 1;
      db.extensionCreated = true;
      return;
    }
    if (sql.startsWith('CREATE TABLE')) {
      db.createTableCalls += 1;
      db.tableCreated = true;
      return;
    }
    if (sql.startsWith('CREATE INDEX')) {
      db.createIndexCalls += 1;
      return;
    }
    if (sql.startsWith('INSERT INTO')) {
      // Detect single vs multi-row by counting VALUES groups.
      const groups = sql.match(/\(\$\d+/g);
      const cols = 10;
      const groupCount = groups ? groups.length : 1;
      for (let g = 0; g < groupCount; g++) {
        const base = g * cols;
        const row: PgRow = {
          id: String(params[base]),
          principal_id: String(params[base + 1]),
          namespace: String(params[base + 2]),
          content: String(params[base + 3]),
          tags: String(params[base + 4]),
          importance: Number(params[base + 5]),
          embedding: String(params[base + 6]),
          metadata: String(params[base + 7]),
          created_at: Number(params[base + 8]),
          updated_at: Number(params[base + 9]),
        };
        db.rows.set(row.id, row);
      }
      return;
    }
    if (sql.startsWith('UPDATE')) {
      const id = String(params[9]);
      const existing = db.rows.get(id);
      if (!existing) return;
      db.rows.set(id, {
        ...existing,
        principal_id: String(params[0]),
        namespace: String(params[1]),
        content: String(params[2]),
        tags: String(params[3]),
        importance: Number(params[4]),
        embedding: String(params[5]),
        metadata: String(params[6]),
        created_at: Number(params[7]),
        updated_at: Number(params[8]),
      });
      return;
    }
    if (sql.startsWith('DELETE FROM')) {
      db.rows.delete(String(params[0]));
      return;
    }
    throw new Error('unrecognized exec: ' + sql);
  };

  return db;
}

function parseTagSet(literal: string): string[] {
  let s = literal.trim();
  if (s.startsWith('{') && s.endsWith('}')) s = s.slice(1, -1);
  if (!s) return [];
  return s.split(',').map((p) => p.replace(/^"|"$/g, '').replace(/\\(.)/g, '$1'));
}

function arrayOverlap(a: readonly string[], b: readonly string[]): boolean {
  const set = new Set(a);
  for (const t of b) if (set.has(t)) return true;
  return false;
}

function makeStore(
  db: FakeDB,
  overrides: Partial<{
    embeddingFn: EmbeddingFn;
    embeddingDim: number;
    autoCreate: boolean;
    ensureExtension: boolean;
    tableName: string;
  }> = {},
): LakebaseMemoryStore {
  return new LakebaseMemoryStore({
    querySql: db.querySql,
    execSql: db.execSql,
    embeddingFn: overrides.embeddingFn ?? toyEmbedder(),
    embeddingDim: overrides.embeddingDim ?? DIM,
    tableName: overrides.tableName ?? 'apx_memories',
    autoCreate: overrides.autoCreate ?? true,
    ensureExtension: overrides.ensureExtension ?? true,
  });
}

function makeInput(overrides: Partial<MemoryInput> = {}): MemoryInput {
  return {
    principalId: 'user-1',
    content: 'apple banana',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Construction / validation
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore — construction', () => {
  it('throws when embeddingFn is missing', () => {
    const db = makeFakeDb();
    expect(
      () =>
        new LakebaseMemoryStore({
          querySql: db.querySql,
          execSql: db.execSql,
          // @ts-expect-error intentionally missing
          embeddingFn: undefined,
          embeddingDim: DIM,
        }),
    ).toThrow(/embeddingFn/);
  });

  it('throws when embeddingDim is missing', () => {
    const db = makeFakeDb();
    expect(
      () =>
        new LakebaseMemoryStore({
          querySql: db.querySql,
          execSql: db.execSql,
          embeddingFn: toyEmbedder(),
          // @ts-expect-error intentionally missing
          embeddingDim: undefined,
        }),
    ).toThrow();
  });

  it('throws when embeddingDim is non-positive', () => {
    const db = makeFakeDb();
    expect(
      () =>
        new LakebaseMemoryStore({
          querySql: db.querySql,
          execSql: db.execSql,
          embeddingFn: toyEmbedder(),
          embeddingDim: 0,
        }),
    ).toThrow();
  });

  it('uses default tableName "apx_memories"', () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(store.tableName).toBe('apx_memories');
  });

  it('honors custom tableName', () => {
    const db = makeFakeDb();
    const store = makeStore(db, { tableName: 'myschema.mem' });
    expect(store.tableName).toBe('myschema.mem');
  });

  it('exposes embeddingDim publicly', () => {
    const db = makeFakeDb();
    const store = makeStore(db, { embeddingDim: 17 });
    expect(store.embeddingDim).toBe(17);
  });
});

// ---------------------------------------------------------------------------
// Vector + tag literal helpers
// ---------------------------------------------------------------------------

describe('vectorLiteral / tagsLiteral', () => {
  it('renders an empty tag array as {}', () => {
    expect(tagsLiteral([])).toBe('{}');
  });

  it('renders simple tags without quoting', () => {
    expect(tagsLiteral(['a', 'b', 'c'])).toBe('{a,b,c}');
  });

  it('quotes tags containing commas, braces, quotes, whitespace, backslash', () => {
    expect(tagsLiteral(['a,b'])).toBe('{"a,b"}');
    expect(tagsLiteral(['{x}'])).toBe('{"{x}"}');
    expect(tagsLiteral(['has space'])).toBe('{"has space"}');
    expect(tagsLiteral(['back\\slash'])).toBe('{"back\\\\slash"}');
    expect(tagsLiteral(['quote"in'])).toBe('{"quote\\"in"}');
  });

  it('renders a vector as [v1,v2,...]', () => {
    expect(vectorLiteral([0.1, 0.2, 0.3])).toBe('[0.1,0.2,0.3]');
  });

  it('coerces NaN/Infinity to 0 in vector literal', () => {
    expect(vectorLiteral([NaN, Infinity, 1])).toBe('[0,0,1]');
  });

  it('quotes empty-string tags as ""', () => {
    expect(tagsLiteral([''])).toBe('{""}');
  });
});

// ---------------------------------------------------------------------------
// Auto-create / DDL
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore — auto-create', () => {
  it('runs CREATE EXTENSION + CREATE TABLE + indexes on first use', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    expect(db.createExtensionCalls).toBe(1);
    expect(db.createTableCalls).toBe(1);
    expect(db.createIndexCalls).toBe(2);
  });

  it('only runs DDL once across many operations', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    await store.add(makeInput({ content: 'second' }));
    await store.list({ principalId: 'user-1' });
    await store.recall({ principalId: 'user-1', query: 'apple' });
    expect(db.createTableCalls).toBe(1);
    expect(db.createExtensionCalls).toBe(1);
  });

  it('skips CREATE EXTENSION when ensureExtension=false', async () => {
    const db = makeFakeDb();
    const store = makeStore(db, { ensureExtension: false });
    await store.add(makeInput());
    expect(db.createExtensionCalls).toBe(0);
    expect(db.createTableCalls).toBe(1);
  });

  it('skips all DDL when autoCreate=false', async () => {
    const db = makeFakeDb();
    db.tableCreated = true;
    const store = makeStore(db, { autoCreate: false });
    await store.add(makeInput());
    expect(db.createTableCalls).toBe(0);
    expect(db.createExtensionCalls).toBe(0);
  });

  it('emits the expected CREATE TABLE DDL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const ddl = db.execLog.find((e) => e.sql.startsWith('CREATE TABLE'));
    expect(ddl).toBeDefined();
    expect(ddl!.sql).toContain('CREATE TABLE IF NOT EXISTS apx_memories');
    expect(ddl!.sql).toContain('id TEXT PRIMARY KEY');
    expect(ddl!.sql).toContain('principal_id TEXT NOT NULL');
    expect(ddl!.sql).toContain('tags TEXT[]');
    expect(ddl!.sql).toContain('embedding vector(4)');
    expect(ddl!.sql).toContain('metadata JSONB');
  });

  it('emits ivfflat cosine index DDL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const idx = db.execLog.find((e) => e.sql.includes('USING ivfflat'));
    expect(idx).toBeDefined();
    expect(idx!.sql).toContain('vector_cosine_ops');
    expect(idx!.sql).toContain('lists = 100');
  });

  it('embedding column dimensionality matches embeddingDim option', async () => {
    const db = makeFakeDb();
    const store = makeStore(db, { embeddingDim: 8, embeddingFn: async (ts) => ts.map(() => new Array(8).fill(0.1)) });
    await store.add(makeInput());
    const ddl = db.execLog.find((e) => e.sql.startsWith('CREATE TABLE'));
    expect(ddl!.sql).toContain('vector(8)');
  });
});

// ---------------------------------------------------------------------------
// add / addBatch / get
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore — add / get', () => {
  it('add inserts and returns a fully materialized Memory', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ content: 'hello' }));
    expect(m.id).toMatch(/^mem_/);
    expect(m.content).toBe('hello');
    expect(m.namespace).toBe('default');
    expect(m.embedding?.length).toBe(DIM);
    expect(db.rows.size).toBe(1);
  });

  it('add INSERT params include vector literal, tag literal, json metadata', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(
      makeInput({ tags: ['x', 'y'], metadata: { source: 'unit-test' } }),
    );
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(ins).toBeDefined();
    const params = ins!.params;
    expect(String(params[4])).toBe('{x,y}');
    expect(String(params[6])).toMatch(/^\[/);
    expect(String(params[6])).toMatch(/\]$/);
    expect(JSON.parse(String(params[7]))).toEqual({ source: 'unit-test' });
  });

  it('add SQL uses $N positional placeholders + casts', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(ins!.sql).toContain('$1');
    expect(ins!.sql).toContain('$5::text[]');
    expect(ins!.sql).toContain('$7::vector');
    expect(ins!.sql).toContain('$8::jsonb');
  });

  it('add honors caller-supplied id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ id: 'fixed-id' }));
    expect(m.id).toBe('fixed-id');
  });

  it('add throws when embedder returns wrong dimensionality', async () => {
    const db = makeFakeDb();
    const store = makeStore(db, {
      embeddingFn: async (ts) => ts.map(() => [1, 2, 3]), // dim 3 not 4
    });
    await expect(store.add(makeInput())).rejects.toThrow(/dims/);
  });

  it('addBatch inserts everything in one multi-row INSERT', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const inputs = [
      makeInput({ content: 'one' }),
      makeInput({ content: 'two' }),
      makeInput({ content: 'three' }),
    ];
    const out = await store.addBatch(inputs);
    expect(out).toHaveLength(3);
    expect(db.rows.size).toBe(3);
    const inserts = db.execLog.filter((e) => e.sql.startsWith('INSERT INTO'));
    expect(inserts).toHaveLength(1);
  });

  it('addBatch with no inputs is a no-op', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const out = await store.addBatch([]);
    expect(out).toEqual([]);
    expect(db.execLog.find((e) => e.sql.startsWith('INSERT INTO'))).toBeUndefined();
  });

  it('addBatch calls the embedder once with all texts', async () => {
    const db = makeFakeDb();
    const calls: string[][] = [];
    const ef: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => new Array(DIM).fill(0.5));
    };
    const store = makeStore(db, { embeddingFn: ef });
    await store.addBatch([makeInput({ content: 'a' }), makeInput({ content: 'b' })]);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual(['a', 'b']);
  });

  it('get returns null on missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    expect(await store.get('nope')).toBeNull();
  });

  it('get round-trips tags, metadata, embedding through pg types', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(
      makeInput({
        tags: ['alpha', 'beta'],
        metadata: { k: 'v', nested: { n: 1 } },
        importance: 0.7,
      }),
    );
    const got = await store.get(m.id);
    expect(got).not.toBeNull();
    expect(got!.tags).toEqual(['alpha', 'beta']);
    expect(got!.metadata).toEqual({ k: 'v', nested: { n: 1 } });
    expect(got!.importance).toBeCloseTo(0.7);
    expect(got!.embedding?.length).toBe(DIM);
  });

  it('get returns null and warns on SQL error', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.get('whatever')).toBeNull();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// update / delete
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore — update', () => {
  it('returns null when updating a missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(await store.update('nope', { content: 'x' })).toBeNull();
  });

  it('re-embeds when patch.content is provided', async () => {
    const db = makeFakeDb();
    let embedCalls = 0;
    const ef: EmbeddingFn = async (ts) => {
      embedCalls += 1;
      return ts.map((t) => {
        const v = new Array(DIM).fill(0);
        for (let i = 0; i < t.length; i++) v[i % DIM] += t.charCodeAt(i);
        return v;
      });
    };
    const store = makeStore(db, { embeddingFn: ef });
    const m = await store.add(makeInput({ content: 'old' }));
    const before = embedCalls;
    const next = await store.update(m.id, { content: 'new' });
    expect(next).not.toBeNull();
    expect(next!.content).toBe('new');
    expect(embedCalls).toBe(before + 1);
  });

  it('does NOT re-embed when only tags / importance / metadata change', async () => {
    const db = makeFakeDb();
    let embedCalls = 0;
    const ef: EmbeddingFn = async (ts) => {
      embedCalls += 1;
      return ts.map(() => new Array(DIM).fill(0.1));
    };
    const store = makeStore(db, { embeddingFn: ef });
    const m = await store.add(makeInput({ content: 'stable' }));
    const before = embedCalls;
    await store.update(m.id, { tags: ['new'], importance: 0.9, metadata: { x: 1 } });
    expect(embedCalls).toBe(before);
  });

  it('UPDATE SQL uses $N placeholders with proper casts', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput());
    db.execLog.length = 0;
    await store.update(m.id, { tags: ['a'] });
    const upd = db.execLog.find((e) => e.sql.startsWith('UPDATE'));
    expect(upd).toBeDefined();
    expect(upd!.sql).toContain('$4::text[]');
    expect(upd!.sql).toContain('$6::vector');
    expect(upd!.sql).toContain('$7::jsonb');
    expect(upd!.sql).toContain('WHERE id = $10');
  });

  it('persists tag changes', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ tags: ['old'] }));
    await store.update(m.id, { tags: ['new1', 'new2'] });
    const got = await store.get(m.id);
    expect(got!.tags).toEqual(['new1', 'new2']);
  });
});

describe('LakebaseMemoryStore — delete', () => {
  it('returns false for a missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(await store.delete('nope')).toBe(false);
  });

  it('returns true and removes the row', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput());
    expect(await store.delete(m.id)).toBe(true);
    expect(await store.get(m.id)).toBeNull();
  });

  it('emits DELETE $1 SQL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput());
    db.execLog.length = 0;
    await store.delete(m.id);
    const del = db.execLog.find((e) => e.sql.startsWith('DELETE FROM'));
    expect(del).toBeDefined();
    expect(del!.sql).toContain('WHERE id = $1');
    expect(del!.params[0]).toBe(m.id);
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore.list', () => {
  it('returns rows sorted by updated_at DESC', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const a = await store.add(makeInput({ content: 'first' }));
    // Force temporal ordering by mutating the stored row.
    await new Promise((r) => setTimeout(r, 5));
    const b = await store.add(makeInput({ content: 'second' }));
    const rows = await store.list({ principalId: 'user-1' });
    expect(rows.map((r) => r.id)).toEqual([b.id, a.id]);
  });

  it('filters by namespace', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'a', namespace: 'profile' }));
    await store.add(makeInput({ content: 'b', namespace: 'episodic' }));
    const out = await store.list({ principalId: 'user-1', namespace: 'profile' });
    expect(out).toHaveLength(1);
    expect(out[0]!.namespace).toBe('profile');
  });

  it('filters by tags any-of (&&) overlap', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'a', tags: ['red'] }));
    await store.add(makeInput({ content: 'b', tags: ['green'] }));
    await store.add(makeInput({ content: 'c', tags: ['blue', 'red'] }));
    const out = await store.list({ principalId: 'user-1', tags: ['red'] });
    expect(out).toHaveLength(2);
  });

  it('filters by minImportance', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'a', importance: 0.2 }));
    await store.add(makeInput({ content: 'b', importance: 0.8 }));
    const out = await store.list({ principalId: 'user-1', minImportance: 0.5 });
    expect(out).toHaveLength(1);
    expect(out[0]!.importance).toBeCloseTo(0.8);
  });

  it('emits SQL with IS NULL guards for optional filters', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.list({ principalId: 'user-1' });
    const sel = db.queryLog.find((q) => q.sql.includes('WHERE principal_id ='));
    expect(sel).toBeDefined();
    expect(sel!.sql).toContain('$2::text IS NULL');
    expect(sel!.sql).toContain('$3::text[] IS NULL');
    expect(sel!.sql).toContain('$4::real IS NULL');
    expect(sel!.sql).toContain('tags && $3::text[]');
    expect(sel!.params[1]).toBeNull();
    expect(sel!.params[2]).toBeNull();
    expect(sel!.params[3]).toBeNull();
  });

  it('respects the limit parameter', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    for (let i = 0; i < 5; i++) {
      await store.add(makeInput({ content: `c${i}` }));
    }
    const out = await store.list({ principalId: 'user-1', limit: 2 });
    expect(out).toHaveLength(2);
  });

  it('returns [] when query throws', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list({ principalId: 'user-1' })).toEqual([]);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// recall
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore.recall', () => {
  it('uses embedding <=> $1::vector and ORDER BY distance', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'apple banana' }));
    await store.recall({ principalId: 'user-1', query: 'apple' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel).toBeDefined();
    expect(sel!.sql).toContain('ORDER BY embedding <=> $1::vector');
    expect(sel!.sql).toContain('1 - (embedding <=> $1::vector) AS score');
  });

  it('returns score = 1 - distance (similarity)', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'identical' }));
    const out = await store.recall({ principalId: 'user-1', query: 'identical', k: 1 });
    expect(out).toHaveLength(1);
    expect(out[0]!.score).toBeGreaterThan(0.99);
  });

  it('ranks closer queries higher', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'cat sat on the mat' }));
    await store.add(makeInput({ content: 'completely unrelated topic xyz' }));
    const out = await store.recall({ principalId: 'user-1', query: 'cat sat on the mat', k: 2 });
    expect(out[0]!.memory.content).toBe('cat sat on the mat');
    expect(out[0]!.score).toBeGreaterThanOrEqual(out[1]!.score);
  });

  it('honors the k parameter', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    for (let i = 0; i < 6; i++) await store.add(makeInput({ content: `c${i}` }));
    const out = await store.recall({ principalId: 'user-1', query: 'c0', k: 3 });
    expect(out).toHaveLength(3);
  });

  it('embeds the query exactly once', async () => {
    const db = makeFakeDb();
    let calls = 0;
    const ef: EmbeddingFn = async (ts) => {
      calls += 1;
      return ts.map(() => new Array(DIM).fill(0.25));
    };
    const store = makeStore(db, { embeddingFn: ef });
    await store.add(makeInput());
    const before = calls;
    await store.recall({ principalId: 'user-1', query: 'q' });
    expect(calls).toBe(before + 1);
  });

  it('filters by namespace, tags, minImportance in SQL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ content: 'a', namespace: 'profile', tags: ['x'], importance: 0.9 }));
    await store.recall({
      principalId: 'user-1',
      query: 'a',
      namespace: 'profile',
      tags: ['x'],
      minImportance: 0.5,
    });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel!.sql).toContain('$3::text IS NULL OR namespace = $3');
    expect(sel!.sql).toContain('$4::text[] IS NULL OR tags && $4::text[]');
    expect(sel!.sql).toContain('$5::real IS NULL OR importance >= $5');
    expect(sel!.params[2]).toBe('profile');
    expect(sel!.params[3]).toBe('{x}');
    expect(sel!.params[4]).toBe(0.5);
  });

  it('passes the vector literal as $1', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.recall({ principalId: 'user-1', query: 'abc' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(String(sel!.params[0])).toMatch(/^\[.*\]$/);
  });

  it('returns [] when SQL fails', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.recall({ principalId: 'user-1', query: 'q' })).toEqual([]);
    warn.mockRestore();
  });

  it('throws when the recall query embedder returns wrong dim', async () => {
    const db = makeFakeDb();
    let first = true;
    const ef: EmbeddingFn = async (ts) =>
      ts.map(() => {
        if (first) {
          first = false;
          return new Array(DIM).fill(1);
        }
        return [1, 2, 3];
      });
    const store = makeStore(db, { embeddingFn: ef });
    await store.add(makeInput());
    await expect(store.recall({ principalId: 'user-1', query: 'q' })).rejects.toThrow(/dims/);
  });

  it('SQL pre-filters embedding IS NOT NULL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.recall({ principalId: 'user-1', query: 'q' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel!.sql).toContain('embedding IS NOT NULL');
  });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe('LakebaseMemoryStore — edge cases', () => {
  it('round-trips a tag with a comma and quote', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ tags: ['a,b', 'has "quote"'] }));
    const got = await store.get(m.id);
    expect(got!.tags).toEqual(['a,b', 'has "quote"']);
  });

  it('round-trips unicode content', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ content: "don't worry — résumé 🚀" }));
    const got = await store.get(m.id);
    expect(got!.content).toBe("don't worry — résumé 🚀");
  });

  it('persists empty metadata as {} on the wire', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput());
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(String(ins!.params[7])).toBe('{}');
    const got = await store.get(m.id);
    expect(got!.metadata).toEqual({});
  });

  it('lets caller-supplied namespace override default', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const m = await store.add(makeInput({ namespace: 'episodic' }));
    expect(m.namespace).toBe('episodic');
  });
});

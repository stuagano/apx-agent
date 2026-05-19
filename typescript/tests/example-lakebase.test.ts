/**
 * Tests for LakebaseExampleStore — pgvector-backed `ExampleStore`.
 *
 * Mirrors `memory-lakebase.test.ts` but for examples: agent_id scope,
 * input-side embedding, `findSimilar` instead of `recall`, score column
 * is part of the schema (not just a recall projection).
 */

import { describe, it, expect, vi } from 'vitest';
import {
  LakebaseExampleStore,
  type PgExecExecutor,
  type PgQueryExecutor,
} from '../src/example-lakebase.js';
import type { EmbeddingFn } from '../src/memory.js';
import type { ExampleInput } from '../src/example.js';

// ---------------------------------------------------------------------------
// Toy embedder
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
  agent_id: string;
  intent: string;
  input: string;
  output: string;
  score: number | null;
  tags: string;
  embedding: string;
  metadata: string;
  created_at: number;
  updated_at: number;
}

interface FakeDB {
  rows: Map<string, PgRow>;
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

function makeFakeDb(): FakeDB {
  const db: FakeDB = {
    rows: new Map(),
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
    if (sql.startsWith('SELECT id, agent_id') && sql.includes('WHERE id = $1')) {
      const row = db.rows.get(String(params[0]));
      return row ? [{ ...row }] : [];
    }
    if (sql.startsWith('SELECT id, agent_id') && sql.includes('WHERE agent_id =')) {
      const isFind = sql.includes('embedding <=> $1::vector');
      if (isFind) {
        const qvec = parseVector(String(params[0]));
        const aid = String(params[1]);
        const intent = params[2] as string | null;
        const tagsRaw = params[3] as string | null;
        const minScore = params[4] as number | null;
        const k = Number(params[5]);
        const filterTags = tagsRaw ? parseTagSet(tagsRaw) : null;
        const scored = Array.from(db.rows.values())
          .filter((r) => r.agent_id === aid)
          .filter((r) => intent === null || r.intent === intent)
          .filter((r) => filterTags === null || arrayOverlap(parseTagSet(r.tags), filterTags))
          .filter((r) => minScore === null || (r.score !== null && r.score >= minScore))
          .filter((r) => r.embedding && r.embedding.length > 0)
          .map((r) => {
            const dist = 1 - cosineSim(qvec, parseVector(r.embedding));
            return { row: r, dist };
          })
          .sort((a, b) => a.dist - b.dist)
          .slice(0, k)
          .map((x) => ({ ...x.row, similarity: 1 - x.dist }));
        return scored;
      } else {
        const aid = String(params[0]);
        const intent = params[1] as string | null;
        const tagsRaw = params[2] as string | null;
        const minScore = params[3] as number | null;
        const limit = Number(params[4]);
        const filterTags = tagsRaw ? parseTagSet(tagsRaw) : null;
        return Array.from(db.rows.values())
          .filter((r) => r.agent_id === aid)
          .filter((r) => intent === null || r.intent === intent)
          .filter((r) => filterTags === null || arrayOverlap(parseTagSet(r.tags), filterTags))
          .filter((r) => minScore === null || (r.score !== null && r.score >= minScore))
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
      return;
    }
    if (sql.startsWith('CREATE TABLE')) {
      db.createTableCalls += 1;
      return;
    }
    if (sql.startsWith('CREATE INDEX')) {
      db.createIndexCalls += 1;
      return;
    }
    if (sql.startsWith('INSERT INTO')) {
      const groups = sql.match(/\(\$\d+/g);
      const cols = 11;
      const groupCount = groups ? groups.length : 1;
      for (let g = 0; g < groupCount; g++) {
        const base = g * cols;
        const row: PgRow = {
          id: String(params[base]),
          agent_id: String(params[base + 1]),
          intent: String(params[base + 2]),
          input: String(params[base + 3]),
          output: String(params[base + 4]),
          score: params[base + 5] === null ? null : Number(params[base + 5]),
          tags: String(params[base + 6]),
          embedding: String(params[base + 7]),
          metadata: String(params[base + 8]),
          created_at: Number(params[base + 9]),
          updated_at: Number(params[base + 10]),
        };
        db.rows.set(row.id, row);
      }
      return;
    }
    if (sql.startsWith('UPDATE')) {
      const id = String(params[10]);
      const existing = db.rows.get(id);
      if (!existing) return;
      db.rows.set(id, {
        ...existing,
        agent_id: String(params[0]),
        intent: String(params[1]),
        input: String(params[2]),
        output: String(params[3]),
        score: params[4] === null ? null : Number(params[4]),
        tags: String(params[5]),
        embedding: String(params[6]),
        metadata: String(params[7]),
        created_at: Number(params[8]),
        updated_at: Number(params[9]),
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

function makeStore(
  db: FakeDB,
  overrides: Partial<{
    embeddingFn: EmbeddingFn;
    embeddingDim: number;
    autoCreate: boolean;
    ensureExtension: boolean;
    tableName: string;
  }> = {},
): LakebaseExampleStore {
  return new LakebaseExampleStore({
    querySql: db.querySql,
    execSql: db.execSql,
    embeddingFn: overrides.embeddingFn ?? toyEmbedder(),
    embeddingDim: overrides.embeddingDim ?? DIM,
    tableName: overrides.tableName ?? 'apx_examples',
    autoCreate: overrides.autoCreate ?? true,
    ensureExtension: overrides.ensureExtension ?? true,
  });
}

function makeInput(overrides: Partial<ExampleInput> = {}): ExampleInput {
  return {
    agentId: 'agent-1',
    input: 'classify this question',
    output: 'answer-A',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore — construction', () => {
  it('throws when embeddingFn is missing', () => {
    const db = makeFakeDb();
    expect(
      () =>
        new LakebaseExampleStore({
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
        new LakebaseExampleStore({
          querySql: db.querySql,
          execSql: db.execSql,
          embeddingFn: toyEmbedder(),
          // @ts-expect-error intentionally missing
          embeddingDim: undefined,
        }),
    ).toThrow();
  });

  it('rejects non-positive embeddingDim', () => {
    const db = makeFakeDb();
    expect(
      () =>
        new LakebaseExampleStore({
          querySql: db.querySql,
          execSql: db.execSql,
          embeddingFn: toyEmbedder(),
          embeddingDim: -1,
        }),
    ).toThrow();
  });

  it('uses default tableName "apx_examples"', () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(store.tableName).toBe('apx_examples');
  });

  it('honors custom tableName', () => {
    const db = makeFakeDb();
    const store = makeStore(db, { tableName: 'myschema.ex' });
    expect(store.tableName).toBe('myschema.ex');
  });

  it('exposes embeddingDim', () => {
    const db = makeFakeDb();
    const store = makeStore(db, {
      embeddingDim: 12,
      embeddingFn: async (ts) => ts.map(() => new Array(12).fill(0.1)),
    });
    expect(store.embeddingDim).toBe(12);
  });
});

// ---------------------------------------------------------------------------
// Auto-create
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore — auto-create', () => {
  it('runs CREATE EXTENSION + CREATE TABLE + indexes on first use', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    expect(db.createExtensionCalls).toBe(1);
    expect(db.createTableCalls).toBe(1);
    expect(db.createIndexCalls).toBe(2);
  });

  it('runs DDL only once', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    await store.add(makeInput({ input: 'two' }));
    await store.findSimilar({ agentId: 'agent-1', query: 'two' });
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
    const store = makeStore(db, { autoCreate: false });
    await store.add(makeInput());
    expect(db.createTableCalls).toBe(0);
  });

  it('emits the expected CREATE TABLE DDL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const ddl = db.execLog.find((e) => e.sql.startsWith('CREATE TABLE'));
    expect(ddl).toBeDefined();
    expect(ddl!.sql).toContain('CREATE TABLE IF NOT EXISTS apx_examples');
    expect(ddl!.sql).toContain('id TEXT PRIMARY KEY');
    expect(ddl!.sql).toContain('agent_id TEXT NOT NULL');
    expect(ddl!.sql).toContain('intent TEXT NOT NULL');
    expect(ddl!.sql).toContain('input TEXT NOT NULL');
    expect(ddl!.sql).toContain('output TEXT NOT NULL');
    expect(ddl!.sql).toContain('score REAL');
    expect(ddl!.sql).toContain('tags TEXT[]');
    expect(ddl!.sql).toContain('embedding vector(4)');
    expect(ddl!.sql).toContain('metadata JSONB');
  });

  it('emits ivfflat index DDL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const idx = db.execLog.find((e) => e.sql.includes('USING ivfflat'));
    expect(idx).toBeDefined();
    expect(idx!.sql).toContain('vector_cosine_ops');
  });
});

// ---------------------------------------------------------------------------
// add / addBatch / get
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore — add / get', () => {
  it('add inserts and returns a materialized Example', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ input: 'hello', output: 'world' }));
    expect(ex.id).toMatch(/^ex_/);
    expect(ex.input).toBe('hello');
    expect(ex.output).toBe('world');
    expect(ex.intent).toBe('default');
    expect(ex.embedding?.length).toBe(DIM);
  });

  it('embeds the input field, not output', async () => {
    const db = makeFakeDb();
    const captured: string[] = [];
    const ef: EmbeddingFn = async (texts) => {
      captured.push(...texts);
      return texts.map(() => new Array(DIM).fill(0.1));
    };
    const store = makeStore(db, { embeddingFn: ef });
    await store.add({ agentId: 'agent-1', input: 'INPUT-X', output: 'OUTPUT-Y' });
    expect(captured).toContain('INPUT-X');
    expect(captured).not.toContain('OUTPUT-Y');
  });

  it('add SQL uses positional $N placeholders + casts', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(ins!.sql).toContain('$1');
    expect(ins!.sql).toContain('$7::text[]');
    expect(ins!.sql).toContain('$8::vector');
    expect(ins!.sql).toContain('$9::jsonb');
  });

  it('add INSERT params include vector + tag + json', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ tags: ['x', 'y'], score: 0.8, metadata: { src: 'unit' } }));
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(String(ins!.params[6])).toBe('{x,y}');
    expect(String(ins!.params[7])).toMatch(/^\[.*\]$/);
    expect(JSON.parse(String(ins!.params[8]))).toEqual({ src: 'unit' });
    expect(ins!.params[5]).toBeCloseTo(0.8);
  });

  it('add persists null score when not provided', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput());
    expect(ex.score).toBeUndefined();
    const got = await store.get(ex.id);
    expect(got!.score).toBeUndefined();
  });

  it('add honors caller-supplied id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ id: 'ex-fixed' }));
    expect(ex.id).toBe('ex-fixed');
  });

  it('add throws on wrong embedder dimensionality', async () => {
    const db = makeFakeDb();
    const store = makeStore(db, { embeddingFn: async (ts) => ts.map(() => [1, 2]) });
    await expect(store.add(makeInput())).rejects.toThrow(/dims/);
  });

  it('addBatch builds a single multi-row INSERT', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const inputs = [makeInput(), makeInput({ input: 'two' }), makeInput({ input: 'three' })];
    const out = await store.addBatch(inputs);
    expect(out).toHaveLength(3);
    const inserts = db.execLog.filter((e) => e.sql.startsWith('INSERT INTO'));
    expect(inserts).toHaveLength(1);
  });

  it('addBatch with empty input is a no-op', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const out = await store.addBatch([]);
    expect(out).toEqual([]);
  });

  it('addBatch embeds inputs in one call', async () => {
    const db = makeFakeDb();
    const calls: string[][] = [];
    const ef: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => new Array(DIM).fill(0.5));
    };
    const store = makeStore(db, { embeddingFn: ef });
    await store.addBatch([makeInput({ input: 'a' }), makeInput({ input: 'b' })]);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual(['a', 'b']);
  });

  it('get returns null on missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    expect(await store.get('nope')).toBeNull();
  });

  it('get round-trips score / tags / metadata', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(
      makeInput({ tags: ['t1', 't2'], score: 0.42, metadata: { rater: 'me' } }),
    );
    const got = await store.get(ex.id);
    expect(got!.score).toBeCloseTo(0.42);
    expect(got!.tags).toEqual(['t1', 't2']);
    expect(got!.metadata).toEqual({ rater: 'me' });
  });

  it('get returns null on SQL error and warns', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.get('whatever')).toBeNull();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// update / delete
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore — update', () => {
  it('returns null on missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(await store.update('nope', { input: 'x' })).toBeNull();
  });

  it('re-embeds when patch.input changes', async () => {
    const db = makeFakeDb();
    let calls = 0;
    const ef: EmbeddingFn = async (ts) => {
      calls += 1;
      return ts.map(() => new Array(DIM).fill(0.5));
    };
    const store = makeStore(db, { embeddingFn: ef });
    const ex = await store.add(makeInput({ input: 'old' }));
    const before = calls;
    await store.update(ex.id, { input: 'new' });
    expect(calls).toBe(before + 1);
  });

  it('does not re-embed when patch.output changes', async () => {
    const db = makeFakeDb();
    let calls = 0;
    const ef: EmbeddingFn = async (ts) => {
      calls += 1;
      return ts.map(() => new Array(DIM).fill(0.5));
    };
    const store = makeStore(db, { embeddingFn: ef });
    const ex = await store.add(makeInput({ input: 'stable' }));
    const before = calls;
    await store.update(ex.id, { output: 'totally different' });
    expect(calls).toBe(before);
  });

  it('UPDATE SQL uses $N placeholders + casts', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput());
    db.execLog.length = 0;
    await store.update(ex.id, { score: 0.5 });
    const upd = db.execLog.find((e) => e.sql.startsWith('UPDATE'));
    expect(upd).toBeDefined();
    expect(upd!.sql).toContain('$6::text[]');
    expect(upd!.sql).toContain('$7::vector');
    expect(upd!.sql).toContain('$8::jsonb');
    expect(upd!.sql).toContain('WHERE id = $11');
  });

  it('persists score / tags / output changes', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ output: 'old-out' }));
    await store.update(ex.id, { output: 'new-out', score: 0.9, tags: ['t'] });
    const got = await store.get(ex.id);
    expect(got!.output).toBe('new-out');
    expect(got!.score).toBeCloseTo(0.9);
    expect(got!.tags).toEqual(['t']);
  });
});

describe('LakebaseExampleStore — delete', () => {
  it('returns false on missing id', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    expect(await store.delete('nope')).toBe(false);
  });

  it('returns true and removes the row', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput());
    expect(await store.delete(ex.id)).toBe(true);
    expect(await store.get(ex.id)).toBeNull();
  });

  it('emits DELETE FROM ... WHERE id = $1', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput());
    db.execLog.length = 0;
    await store.delete(ex.id);
    const del = db.execLog.find((e) => e.sql.startsWith('DELETE FROM'));
    expect(del!.sql).toContain('WHERE id = $1');
    expect(del!.params[0]).toBe(ex.id);
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore.list', () => {
  it('returns rows ordered by updated_at DESC', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const a = await store.add(makeInput({ input: 'first' }));
    await new Promise((r) => setTimeout(r, 5));
    const b = await store.add(makeInput({ input: 'second' }));
    const rows = await store.list({ agentId: 'agent-1' });
    expect(rows.map((r) => r.id)).toEqual([b.id, a.id]);
  });

  it('filters by intent', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ intent: 'classify' }));
    await store.add(makeInput({ intent: 'extract' }));
    const out = await store.list({ agentId: 'agent-1', intent: 'classify' });
    expect(out).toHaveLength(1);
    expect(out[0]!.intent).toBe('classify');
  });

  it('filters by tags any-of overlap', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ tags: ['a'] }));
    await store.add(makeInput({ tags: ['b'] }));
    await store.add(makeInput({ tags: ['a', 'c'] }));
    const out = await store.list({ agentId: 'agent-1', tags: ['a'] });
    expect(out).toHaveLength(2);
  });

  it('filters by minScore — skips null scores', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ input: 'no-score' })); // null score
    await store.add(makeInput({ input: 'low', score: 0.2 }));
    await store.add(makeInput({ input: 'high', score: 0.9 }));
    const out = await store.list({ agentId: 'agent-1', minScore: 0.5 });
    expect(out).toHaveLength(1);
    expect(out[0]!.score).toBeCloseTo(0.9);
  });

  it('emits SQL with IS NULL guards', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.list({ agentId: 'agent-1' });
    const sel = db.queryLog.find((q) => q.sql.includes('WHERE agent_id ='));
    expect(sel!.sql).toContain('$2::text IS NULL');
    expect(sel!.sql).toContain('$3::text[] IS NULL');
    expect(sel!.sql).toContain('$4::real IS NULL');
    expect(sel!.sql).toContain('tags && $3::text[]');
    expect(sel!.sql).toContain('score IS NOT NULL AND score >= $4');
  });

  it('honors limit', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    for (let i = 0; i < 4; i++) await store.add(makeInput({ input: `i${i}` }));
    const out = await store.list({ agentId: 'agent-1', limit: 2 });
    expect(out).toHaveLength(2);
  });

  it('returns [] on SQL error', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list({ agentId: 'agent-1' })).toEqual([]);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// findSimilar
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore.findSimilar', () => {
  it('SQL uses embedding <=> $1::vector and ORDER BY distance', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    await store.findSimilar({ agentId: 'agent-1', query: 'x' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel!.sql).toContain('ORDER BY embedding <=> $1::vector');
    expect(sel!.sql).toContain('1 - (embedding <=> $1::vector)');
  });

  it('returns score = 1 - distance', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ input: 'identical' }));
    const out = await store.findSimilar({ agentId: 'agent-1', query: 'identical', k: 1 });
    expect(out).toHaveLength(1);
    expect(out[0]!.score).toBeGreaterThan(0.99);
  });

  it('ranks closer queries higher', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ input: 'cat sat on the mat' }));
    await store.add(makeInput({ input: 'completely unrelated topic xyz' }));
    const out = await store.findSimilar({ agentId: 'agent-1', query: 'cat sat on the mat', k: 2 });
    expect(out[0]!.example.input).toBe('cat sat on the mat');
    expect(out[0]!.score).toBeGreaterThanOrEqual(out[1]!.score);
  });

  it('honors k parameter', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    for (let i = 0; i < 6; i++) await store.add(makeInput({ input: `c${i}` }));
    const out = await store.findSimilar({ agentId: 'agent-1', query: 'c0', k: 3 });
    expect(out).toHaveLength(3);
  });

  it('filters by intent, tags, minScore in SQL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput({ intent: 'classify', tags: ['x'], score: 0.9 }));
    await store.findSimilar({
      agentId: 'agent-1',
      query: 'q',
      intent: 'classify',
      tags: ['x'],
      minScore: 0.5,
    });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel!.sql).toContain('$3::text IS NULL OR intent = $3');
    expect(sel!.sql).toContain('$4::text[] IS NULL OR tags && $4::text[]');
    expect(sel!.sql).toContain('score IS NOT NULL AND score >= $5');
    expect(sel!.params[2]).toBe('classify');
    expect(sel!.params[3]).toBe('{x}');
    expect(sel!.params[4]).toBe(0.5);
  });

  it('passes vector literal as $1', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.findSimilar({ agentId: 'agent-1', query: 'abc' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(String(sel!.params[0])).toMatch(/^\[.*\]$/);
  });

  it('returns [] when SQL fails', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.add(makeInput());
    db.failNext = true;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.findSimilar({ agentId: 'agent-1', query: 'q' })).toEqual([]);
    warn.mockRestore();
  });

  it('pre-filters embedding IS NOT NULL', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    await store.findSimilar({ agentId: 'agent-1', query: 'q' });
    const sel = db.queryLog.find((q) => q.sql.includes('embedding <=> $1::vector'));
    expect(sel!.sql).toContain('embedding IS NOT NULL');
  });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe('LakebaseExampleStore — edge cases', () => {
  it('round-trips tags with commas + quotes', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ tags: ['a,b', 'has "quote"'] }));
    const got = await store.get(ex.id);
    expect(got!.tags).toEqual(['a,b', 'has "quote"']);
  });

  it('round-trips unicode input + output', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ input: "don't 🚀", output: 'résumé' }));
    const got = await store.get(ex.id);
    expect(got!.input).toBe("don't 🚀");
    expect(got!.output).toBe('résumé');
  });

  it('persists empty metadata as {} on the wire', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput());
    const ins = db.execLog.find((e) => e.sql.startsWith('INSERT INTO'));
    expect(String(ins!.params[8])).toBe('{}');
    const got = await store.get(ex.id);
    expect(got!.metadata).toEqual({});
  });

  it('allows caller-supplied intent to override default', async () => {
    const db = makeFakeDb();
    const store = makeStore(db);
    const ex = await store.add(makeInput({ intent: 'extract_fields' }));
    expect(ex.intent).toBe('extract_fields');
  });
});

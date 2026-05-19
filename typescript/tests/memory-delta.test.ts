/**
 * Tests for DeltaMemoryStore — UC Delta-backed MemoryStore.
 *
 * Mirrors session-delta.test.ts harness style: capture emitted SQL via
 * injectable executors, plus a separate path for VectorSearchClient
 * delegation.
 */

import { describe, it, expect, vi } from 'vitest';
import { DeltaMemoryStore, type VectorSearchClient } from '../src/memory-delta.js';
import type { EmbeddingFn, MemoryInput } from '../src/memory.js';

// ---------------------------------------------------------------------------
// Harness
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

/** Toy embedder: bump positions of first letters of each word. 26-dim. */
function makeToyEmbedder(): EmbeddingFn {
  return async (texts) =>
    texts.map((t) => {
      const vec = new Array(26).fill(0);
      for (const word of t.toLowerCase().split(/\s+/)) {
        const first = word.charCodeAt(0) - 97;
        if (first >= 0 && first < 26) vec[first] += 1;
      }
      return vec;
    });
}

function makeInput(overrides: Partial<MemoryInput> = {}): MemoryInput {
  return { principalId: 'user-1', content: 'apple banana', ...overrides };
}

function makeRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'mem_1',
    principal_id: 'user-1',
    namespace: 'default',
    content: 'hello world',
    tags: ['t1'],
    importance: 0.5,
    embedding: [1, 0, 0],
    metadata: '{"k":"v"}',
    created_at: 1700000000,
    updated_at: 1700000100,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Construction + DDL
// ---------------------------------------------------------------------------

describe('DeltaMemoryStore — construction + DDL', () => {
  it('uses default tableName when omitted', () => {
    const { queryFn, execFn } = captureSql();
    const store = new DeltaMemoryStore({ querySql: queryFn, execSql: execFn });
    expect(store.tableName).toBe('apx_memories');
  });

  it('accepts a fully-qualified UC table name', () => {
    const { queryFn, execFn } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      tableName: 'main.agents.memories',
    });
    expect(store.tableName).toBe('main.agents.memories');
  });

  it('runs CREATE TABLE on first use', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaMemoryStore({ querySql: queryFn, execSql: execFn });
    await store.list({ principalId: 'user-1' });
    const createCalls = execs.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0]!).toContain('CREATE TABLE IF NOT EXISTS apx_memories');
    expect(createCalls[0]!).toContain('embedding ARRAY<FLOAT>');
    expect(createCalls[0]!).toContain('tags ARRAY<STRING>');
    expect(createCalls[0]!).toContain('USING DELTA');
  });

  it('runs CREATE TABLE only once across multiple operations', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaMemoryStore({ querySql: queryFn, execSql: execFn });
    await store.list({ principalId: 'user-1' });
    await store.list({ principalId: 'user-2' });
    await store.delete('mem_x');
    const createCalls = execs.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toHaveLength(1);
  });

  it('does not run CREATE TABLE when autoCreate=false', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1' });
    expect(execs.filter((s) => s.includes('CREATE TABLE'))).toEqual([]);
  });

  it('swallows DDL failures so subsequent ops still run', async () => {
    const queryFn = vi.fn(async () => []);
    const execFn = vi.fn(async (sql: string) => {
      if (sql.includes('CREATE TABLE')) throw new Error('no perms');
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const store = new DeltaMemoryStore({ querySql: queryFn, execSql: execFn });
    await expect(store.list({ principalId: 'user-1' })).resolves.toEqual([]);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// add / addBatch — MERGE INTO
// ---------------------------------------------------------------------------

describe('DeltaMemoryStore.add', () => {
  it('emits a MERGE INTO statement keyed by id', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const m = await store.add(makeInput({ content: 'hello' }));
    expect(m.principalId).toBe('user-1');
    expect(m.content).toBe('hello');
    expect(execs).toHaveLength(1);
    const sql = execs[0]!;
    expect(sql).toContain('MERGE INTO apx_memories');
    expect(sql).toContain('ON target.id = src.id');
    expect(sql).toContain('WHEN MATCHED THEN UPDATE');
    expect(sql).toContain('WHEN NOT MATCHED THEN INSERT');
  });

  it('embeds content when embeddingFn is wired', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.add(makeInput({ content: 'apple' }));
    expect(fn).toHaveBeenCalledWith(['apple']);
    const sql = execs[0]!;
    expect(sql).toContain('array(');  // ARRAY<FLOAT> literal present
    expect(sql).not.toContain('CAST(NULL AS ARRAY<FLOAT>) AS embedding');
  });

  it('uses NULL embedding when no embeddingFn is wired', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(makeInput());
    expect(execs[0]!).toContain('CAST(NULL AS ARRAY<FLOAT>) AS embedding');
  });

  it('escapes single quotes in content, namespace, and tags', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(
      makeInput({
        content: "don't worry",
        namespace: "user's space",
        tags: ["O'Hara"],
      }),
    );
    const sql = execs[0]!;
    expect(sql).toContain("don''t worry");
    expect(sql).toContain("user''s space");
    expect(sql).toContain("O''Hara");
  });

  it('serializes metadata as a JSON string', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(makeInput({ metadata: { source: 'chat', n: 7 } }));
    expect(execs[0]!).toContain('"source":"chat"');
    expect(execs[0]!).toContain('"n":7');
  });

  it('addBatch issues one MERGE per memory', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const results = await store.addBatch([
      makeInput({ content: 'a' }),
      makeInput({ content: 'b' }),
      makeInput({ content: 'c' }),
    ]);
    expect(results).toHaveLength(3);
    expect(execs).toHaveLength(3);
    expect(execs.every((s) => s.includes('MERGE INTO'))).toBe(true);
  });

  it('addBatch returns [] for empty input without DDL or SQL', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
    });
    expect(await store.addBatch([])).toEqual([]);
    expect(execs).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// get / update / delete
// ---------------------------------------------------------------------------

describe('DeltaMemoryStore.get / update / delete', () => {
  it('get returns null when no rows', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.get('mem_x')).toBeNull();
  });

  it('get parses metadata JSON and arrays', async () => {
    const { queryFn, execFn } = captureSql({
      selectRows: [makeRow({ id: 'mem_1', tags: ['a', 'b'], metadata: '{"x":1}' })],
    });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const m = await store.get('mem_1');
    expect(m).not.toBeNull();
    expect(m!.id).toBe('mem_1');
    expect(m!.tags).toEqual(['a', 'b']);
    expect(m!.metadata).toEqual({ x: 1 });
    expect(m!.createdAt).toBe(new Date(1700000000000).toISOString());
  });

  it('update returns null when id missing', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.update('mem_x', { content: 'new' })).toBeNull();
  });

  it('update re-embeds when content changes and embeddingFn is wired', async () => {
    const { queryFn, execFn, execs } = captureSql({
      selectRows: [makeRow({ id: 'mem_1', content: 'old' })],
    });
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    const out = await store.update('mem_1', { content: 'new content' });
    expect(out).not.toBeNull();
    expect(out!.content).toBe('new content');
    expect(fn).toHaveBeenCalledWith(['new content']);
    expect(execs[execs.length - 1]!).toContain('MERGE INTO');
  });

  it('update without content change does not re-embed', async () => {
    const { queryFn, execFn } = captureSql({
      selectRows: [makeRow({ id: 'mem_1' })],
    });
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.update('mem_1', { tags: ['new-tag'] });
    expect(fn).not.toHaveBeenCalled();
  });

  it('delete returns true on success and emits DELETE FROM', async () => {
    const { execFn, queryFn, execs } = captureSql();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.delete('mem_1')).toBe(true);
    expect(execs[0]!).toContain('DELETE FROM apx_memories');
    expect(execs[0]!).toContain("id = 'mem_1'");
  });

  it('delete returns false and logs when SQL throws', async () => {
    const { queryFn } = captureSql();
    const execFn = vi.fn(async (sql: string) => {
      if (sql.startsWith('DELETE')) throw new Error('boom');
    });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.delete('mem_1')).toBe(false);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// list — WHERE filters
// ---------------------------------------------------------------------------

describe('DeltaMemoryStore.list', () => {
  it('filters by principal_id only when other filters omitted', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1' });
    expect(queries[0]!).toContain("principal_id = 'user-1'");
    expect(queries[0]!).not.toContain('namespace =');
    expect(queries[0]!).not.toContain('importance >=');
    expect(queries[0]!).toContain('ORDER BY updated_at DESC LIMIT 100');
  });

  it('adds namespace filter when present', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1', namespace: 'profile' });
    expect(queries[0]!).toContain("namespace = 'profile'");
  });

  it('adds tag filter using array_intersect', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1', tags: ['important', 'pinned'] });
    expect(queries[0]!).toContain("array_intersect(tags, array('important', 'pinned'))");
  });

  it('adds minImportance filter when present', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1', minImportance: 0.7 });
    expect(queries[0]!).toContain('importance >= 0.7');
  });

  it('honors custom limit', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ principalId: 'user-1', limit: 7 });
    expect(queries[0]!).toContain('LIMIT 7');
  });

  it('returns [] when SQL throws', async () => {
    const { queryFn, execFn } = captureSql({ queryThrows: new Error('boom') });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list({ principalId: 'user-1' })).toEqual([]);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// recall — decision tree
// ---------------------------------------------------------------------------

describe('DeltaMemoryStore.recall — Vector Search delegation', () => {
  function makeVS(rows: Array<Record<string, unknown>>): {
    client: VectorSearchClient;
    similaritySearch: ReturnType<typeof vi.fn>;
  } {
    const similaritySearch = vi.fn(async () => ({ rows }));
    return {
      client: { similaritySearch },
      similaritySearch,
    };
  }

  it('delegates to vectorSearch with queryText when no embeddingFn', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([
      { ...makeRow({ id: 'mem_a' }), _score: 0.91 },
    ]);
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'main.x.mem_idx',
    });
    const out = await store.recall({
      principalId: 'user-1',
      query: 'apples',
      k: 3,
    });
    expect(similaritySearch).toHaveBeenCalledTimes(1);
    const arg = similaritySearch.mock.calls[0]![0] as Record<string, unknown>;
    expect(arg.indexName).toBe('main.x.mem_idx');
    expect(arg.queryText).toBe('apples');
    expect(arg.queryVector).toBeUndefined();
    expect(arg.numResults).toBe(3);
    expect(out).toHaveLength(1);
    expect(out[0]!.score).toBeCloseTo(0.91);
    expect(out[0]!.memory.id).toBe('mem_a');
  });

  it('embeds locally and passes queryVector when embeddingFn is wired', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([
      { ...makeRow({ id: 'mem_b' }), score: 0.5 },
    ]);
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'idx',
      embeddingFn: fn,
    });
    await store.recall({ principalId: 'user-1', query: 'orange' });
    expect(fn).toHaveBeenCalledWith(['orange']);
    const arg = similaritySearch.mock.calls[0]![0] as Record<string, unknown>;
    expect(arg.queryVector).toBeDefined();
    expect(Array.isArray(arg.queryVector)).toBe(true);
    expect(arg.queryText).toBeUndefined();
  });

  it('passes principal_id and namespace through filters', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([]);
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'idx',
    });
    await store.recall({
      principalId: 'user-1',
      query: 'q',
      namespace: 'profile',
    });
    const arg = similaritySearch.mock.calls[0]![0] as { filters?: Record<string, unknown> };
    expect(arg.filters).toEqual({ principal_id: 'user-1', namespace: 'profile' });
  });

  it('falls back to score field when _score absent', async () => {
    const { queryFn, execFn } = captureSql();
    const { client } = makeVS([{ ...makeRow({ id: 'mem_x' }), score: 0.42 }]);
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'idx',
    });
    const out = await store.recall({ principalId: 'user-1', query: 'q' });
    expect(out[0]!.score).toBeCloseTo(0.42);
  });
});

describe('DeltaMemoryStore.recall — client-side cosine', () => {
  it('lists candidates via SQL and ranks via cosine when no vectorSearch', async () => {
    // Toy embedder bumps positions of first letters. We craft rows so the
    // query 'apple' ranks the row with embedding [1,0,0,...] (a-bump) highest.
    const aVec = new Array(26).fill(0);
    aVec[0] = 1;
    const zVec = new Array(26).fill(0);
    zVec[25] = 1;
    const rows = [
      makeRow({ id: 'mem_a', content: 'apple', embedding: aVec }),
      makeRow({ id: 'mem_z', content: 'zebra', embedding: zVec }),
    ];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const fn = makeToyEmbedder();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    const out = await store.recall({
      principalId: 'user-1',
      query: 'apple',
      k: 2,
    });
    expect(out).toHaveLength(2);
    expect(out[0]!.memory.id).toBe('mem_a');
    expect(out[0]!.score).toBeGreaterThan(out[1]!.score);
  });

  it('applies WHERE filters in the candidate SELECT (namespace + tags)', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const fn = makeToyEmbedder();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.recall({
      principalId: 'user-1',
      query: 'q',
      namespace: 'profile',
      tags: ['pin'],
    });
    expect(queries[0]!).toContain("namespace = 'profile'");
    expect(queries[0]!).toContain("array_intersect(tags, array('pin'))");
  });

  it('drops candidates with empty embeddings before ranking', async () => {
    const rows = [
      makeRow({ id: 'mem_a', embedding: [1, 0, 0] }),
      makeRow({ id: 'mem_b', embedding: null }),
    ];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const fn = makeToyEmbedder();
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    const out = await store.recall({ principalId: 'user-1', query: 'apple' });
    expect(out).toHaveLength(1);
    expect(out[0]!.memory.id).toBe('mem_a');
  });
});

describe('DeltaMemoryStore.recall — recency fallback', () => {
  it('returns recency-ranked rows when no embeddingFn and no vectorSearch', async () => {
    const rows = [
      makeRow({ id: 'mem_new', updated_at: 1700000200 }),
      makeRow({ id: 'mem_mid', updated_at: 1700000100 }),
      makeRow({ id: 'mem_old', updated_at: 1700000000 }),
    ];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const out = await store.recall({
      principalId: 'user-1',
      query: 'irrelevant',
      k: 2,
    });
    expect(out).toHaveLength(2);
    // SQL ORDER BY desc returns newest first; mapping preserves order.
    expect(out[0]!.memory.id).toBe('mem_new');
    expect(out[0]!.score).toBeGreaterThan(out[1]!.score);
  });

  it('returns [] cleanly when no candidates', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.recall({ principalId: 'user-1', query: 'x' })).toEqual([]);
  });

  it('honors default k=5', async () => {
    const rows = Array.from({ length: 10 }, (_, i) =>
      makeRow({ id: `mem_${i}`, updated_at: 1700000000 + i }),
    );
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const store = new DeltaMemoryStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const out = await store.recall({ principalId: 'user-1', query: 'x' });
    expect(out).toHaveLength(5);
  });
});

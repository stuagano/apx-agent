/**
 * Tests for DeltaExampleStore — UC Delta-backed ExampleStore.
 *
 * Mirrors memory-delta.test.ts. ExampleStore differs from MemoryStore in
 * the scoping key (agentId, not principalId), the discriminating field
 * (input is embedded, not content), and an extra `score` column.
 */

import { describe, it, expect, vi } from 'vitest';
import { DeltaExampleStore } from '../src/example-delta.js';
import type { VectorSearchClient } from '../src/memory-delta.js';
import type { EmbeddingFn, ExampleInput } from '../src/example.js';

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

function captureSql(opts?: {
  selectRows?: Array<Record<string, unknown>>;
  queryThrows?: unknown;
  execThrows?: unknown;
}) {
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

function makeInput(overrides: Partial<ExampleInput> = {}): ExampleInput {
  return {
    agentId: 'agent-1',
    input: 'how do I X?',
    output: 'you do X like this',
    ...overrides,
  };
}

function makeRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'ex_1',
    agent_id: 'agent-1',
    intent: 'default',
    input: 'how do I X?',
    output: 'you do X like this',
    score: 0.8,
    tags: ['demo'],
    embedding: [1, 0, 0],
    metadata: '{"src":"unit-test"}',
    created_at: 1700000000,
    updated_at: 1700000100,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Construction + DDL
// ---------------------------------------------------------------------------

describe('DeltaExampleStore — construction + DDL', () => {
  it('uses default tableName when omitted', () => {
    const { queryFn, execFn } = captureSql();
    const store = new DeltaExampleStore({ querySql: queryFn, execSql: execFn });
    expect(store.tableName).toBe('apx_examples');
  });

  it('runs CREATE TABLE on first use with expected schema', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({ querySql: queryFn, execSql: execFn });
    await store.list({ agentId: 'agent-1' });
    const create = execs.find((s) => s.includes('CREATE TABLE'))!;
    expect(create).toContain('CREATE TABLE IF NOT EXISTS apx_examples');
    expect(create).toContain('input STRING');
    expect(create).toContain('output STRING');
    expect(create).toContain('score FLOAT');
    expect(create).toContain('embedding ARRAY<FLOAT>');
    expect(create).toContain('tags ARRAY<STRING>');
    expect(create).toContain('USING DELTA');
  });

  it('runs CREATE TABLE only once', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({ querySql: queryFn, execSql: execFn });
    await store.list({ agentId: 'agent-1' });
    await store.delete('ex_x');
    await store.list({ agentId: 'agent-2' });
    expect(execs.filter((s) => s.includes('CREATE TABLE'))).toHaveLength(1);
  });

  it('skips CREATE TABLE when autoCreate=false', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ agentId: 'agent-1' });
    expect(execs.filter((s) => s.includes('CREATE TABLE'))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// add — MERGE
// ---------------------------------------------------------------------------

describe('DeltaExampleStore.add', () => {
  it('emits MERGE INTO with input + output columns', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const ex = await store.add(makeInput());
    expect(ex.input).toBe('how do I X?');
    expect(ex.output).toBe('you do X like this');
    const sql = execs[0]!;
    expect(sql).toContain('MERGE INTO apx_examples');
    expect(sql).toContain('ON target.id = src.id');
    expect(sql).toContain("'how do I X?'");
    expect(sql).toContain("'you do X like this'");
  });

  it('embeds the input field (not output) when embeddingFn wired', async () => {
    const { queryFn, execFn } = captureSql();
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.add(makeInput({ input: 'apple', output: 'zebra' }));
    expect(fn).toHaveBeenCalledWith(['apple']);
  });

  it('renders NULL score literal when score is undefined', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(makeInput({ score: undefined }));
    expect(execs[0]!).toContain('CAST(NULL AS FLOAT) AS score');
  });

  it('renders CAST score literal when score is provided', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(makeInput({ score: 0.9 }));
    expect(execs[0]!).toContain('CAST(0.9 AS FLOAT) AS score');
  });

  it('escapes apostrophes in input, output, tags', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.add(
      makeInput({
        input: "can't",
        output: "don't",
        tags: ["O'Hara"],
      }),
    );
    const sql = execs[0]!;
    expect(sql).toContain("can''t");
    expect(sql).toContain("don''t");
    expect(sql).toContain("O''Hara");
  });

  it('addBatch issues one MERGE per example', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const out = await store.addBatch([
      makeInput({ input: 'a' }),
      makeInput({ input: 'b' }),
    ]);
    expect(out).toHaveLength(2);
    expect(execs).toHaveLength(2);
  });

  it('addBatch returns [] for empty input', async () => {
    const { queryFn, execFn } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
    });
    expect(await store.addBatch([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// get / update / delete
// ---------------------------------------------------------------------------

describe('DeltaExampleStore.get / update / delete', () => {
  it('get returns null when no rows', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.get('ex_x')).toBeNull();
  });

  it('get hydrates score, tags, metadata', async () => {
    const { queryFn, execFn } = captureSql({
      selectRows: [makeRow({ id: 'ex_1' })],
    });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const ex = await store.get('ex_1');
    expect(ex).not.toBeNull();
    expect(ex!.score).toBeCloseTo(0.8);
    expect(ex!.tags).toEqual(['demo']);
    expect(ex!.metadata).toEqual({ src: 'unit-test' });
  });

  it('get treats null score as undefined', async () => {
    const { queryFn, execFn } = captureSql({
      selectRows: [makeRow({ id: 'ex_2', score: null })],
    });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const ex = await store.get('ex_2');
    expect(ex!.score).toBeUndefined();
  });

  it('update re-embeds when input changes and embeddingFn wired', async () => {
    const { queryFn, execFn } = captureSql({
      selectRows: [makeRow({ id: 'ex_1' })],
    });
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.update('ex_1', { input: 'new question' });
    expect(fn).toHaveBeenCalledWith(['new question']);
  });

  it('update returns null when id missing', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.update('ex_x', { input: 'x' })).toBeNull();
  });

  it('delete emits DELETE FROM and returns true', async () => {
    const { queryFn, execFn, execs } = captureSql();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.delete('ex_1')).toBe(true);
    expect(execs[0]!).toContain('DELETE FROM apx_examples');
    expect(execs[0]!).toContain("id = 'ex_1'");
  });

  it('delete returns false when SQL throws', async () => {
    const { queryFn } = captureSql();
    const execFn = vi.fn(async (sql: string) => {
      if (sql.startsWith('DELETE')) throw new Error('boom');
    });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.delete('ex_1')).toBe(false);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('DeltaExampleStore.list', () => {
  it('filters by agent_id only when other filters omitted', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ agentId: 'agent-1' });
    expect(queries[0]!).toContain("agent_id = 'agent-1'");
    expect(queries[0]!).not.toContain('intent =');
    expect(queries[0]!).toContain('ORDER BY updated_at DESC');
  });

  it('adds intent filter when present', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ agentId: 'agent-1', intent: 'classify' });
    expect(queries[0]!).toContain("intent = 'classify'");
  });

  it('adds tag filter using array_intersect', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ agentId: 'agent-1', tags: ['gold'] });
    expect(queries[0]!).toContain("array_intersect(tags, array('gold'))");
  });

  it('adds minScore filter when present', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    await store.list({ agentId: 'agent-1', minScore: 0.6 });
    expect(queries[0]!).toContain('score >= 0.6');
  });

  it('returns [] when query throws', async () => {
    const { queryFn, execFn } = captureSql({ queryThrows: new Error('boom') });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(await store.list({ agentId: 'agent-1' })).toEqual([]);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// findSimilar — decision tree
// ---------------------------------------------------------------------------

describe('DeltaExampleStore.findSimilar — Vector Search delegation', () => {
  function makeVS(rows: Array<Record<string, unknown>>) {
    const similaritySearch = vi.fn(async () => ({ rows }));
    return {
      client: { similaritySearch } as VectorSearchClient,
      similaritySearch,
    };
  }

  it('delegates with queryText when no embeddingFn', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([
      { ...makeRow({ id: 'ex_a' }), _score: 0.77 },
    ]);
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'main.x.ex_idx',
    });
    const out = await store.findSimilar({
      agentId: 'agent-1',
      query: 'how to X',
    });
    const arg = similaritySearch.mock.calls[0]![0] as Record<string, unknown>;
    expect(arg.queryText).toBe('how to X');
    expect(arg.queryVector).toBeUndefined();
    expect(out[0]!.score).toBeCloseTo(0.77);
  });

  it('embeds locally when embeddingFn wired and passes queryVector', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([
      { ...makeRow({ id: 'ex_b' }), score: 0.5 },
    ]);
    const fn = vi.fn(makeToyEmbedder());
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'idx',
      embeddingFn: fn,
    });
    await store.findSimilar({ agentId: 'agent-1', query: 'apples' });
    expect(fn).toHaveBeenCalledWith(['apples']);
    const arg = similaritySearch.mock.calls[0]![0] as Record<string, unknown>;
    expect(arg.queryVector).toBeDefined();
    expect(arg.queryText).toBeUndefined();
  });

  it('passes agent_id and intent through filters', async () => {
    const { queryFn, execFn } = captureSql();
    const { client, similaritySearch } = makeVS([]);
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      vectorSearch: client,
      indexName: 'idx',
    });
    await store.findSimilar({ agentId: 'agent-1', query: 'q', intent: 'classify' });
    const arg = similaritySearch.mock.calls[0]![0] as { filters?: Record<string, unknown> };
    expect(arg.filters).toEqual({ agent_id: 'agent-1', intent: 'classify' });
  });
});

describe('DeltaExampleStore.findSimilar — client-side cosine', () => {
  it('lists candidates via SQL and ranks via cosine', async () => {
    const aVec = new Array(26).fill(0);
    aVec[0] = 1;
    const zVec = new Array(26).fill(0);
    zVec[25] = 1;
    const rows = [
      makeRow({ id: 'ex_a', input: 'apple', embedding: aVec }),
      makeRow({ id: 'ex_z', input: 'zebra', embedding: zVec }),
    ];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const fn = makeToyEmbedder();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    const out = await store.findSimilar({
      agentId: 'agent-1',
      query: 'apple',
      k: 2,
    });
    expect(out[0]!.example.id).toBe('ex_a');
    expect(out[0]!.score).toBeGreaterThan(out[1]!.score);
  });

  it('applies intent + tags filters in candidate SELECT', async () => {
    const { queryFn, execFn, queries } = captureSql({ selectRows: [] });
    const fn = makeToyEmbedder();
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
      embeddingFn: fn,
    });
    await store.findSimilar({
      agentId: 'agent-1',
      query: 'q',
      intent: 'classify',
      tags: ['gold'],
    });
    expect(queries[0]!).toContain("intent = 'classify'");
    expect(queries[0]!).toContain("array_intersect(tags, array('gold'))");
  });
});

describe('DeltaExampleStore.findSimilar — recency fallback', () => {
  it('returns recency-ranked rows when no embedder and no vectorSearch', async () => {
    const rows = [
      makeRow({ id: 'ex_new', updated_at: 1700000300 }),
      makeRow({ id: 'ex_mid', updated_at: 1700000200 }),
      makeRow({ id: 'ex_old', updated_at: 1700000100 }),
    ];
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const out = await store.findSimilar({
      agentId: 'agent-1',
      query: 'irrelevant',
      k: 2,
    });
    expect(out).toHaveLength(2);
    expect(out[0]!.example.id).toBe('ex_new');
  });

  it('returns [] when no candidates', async () => {
    const { queryFn, execFn } = captureSql({ selectRows: [] });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    expect(await store.findSimilar({ agentId: 'agent-1', query: 'x' })).toEqual([]);
  });

  it('honors default k=5', async () => {
    const rows = Array.from({ length: 9 }, (_, i) =>
      makeRow({ id: `ex_${i}`, updated_at: 1700000000 + i }),
    );
    const { queryFn, execFn } = captureSql({ selectRows: rows });
    const store = new DeltaExampleStore({
      querySql: queryFn,
      execSql: execFn,
      autoCreate: false,
    });
    const out = await store.findSimilar({ agentId: 'agent-1', query: 'x' });
    expect(out).toHaveLength(5);
  });
});

import { describe, it, expect } from 'vitest';
import {
  InMemoryExampleStore,
  materializeExample,
  applyExamplePatch,
  type ExampleInput,
} from '../src/example.js';
import type { EmbeddingFn } from '../src/memory.js';

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
    agentId: 'triage',
    input: 'Why is my bill so high?',
    output: 'Let me check your usage.',
    ...overrides,
  };
}

describe('materializeExample', () => {
  it('mints id + timestamps, defaults intent to "default"', () => {
    const ex = materializeExample(makeInput());
    expect(ex.id).toMatch(/^ex_/);
    expect(ex.intent).toBe('default');
    expect(ex.createdAt).toBe(ex.updatedAt);
  });

  it('preserves caller-supplied id', () => {
    const ex = materializeExample(makeInput({ id: 'fixed' }));
    expect(ex.id).toBe('fixed');
  });

  it('attaches embedding when provided', () => {
    const ex = materializeExample(makeInput(), [0.1, 0.2]);
    expect(ex.embedding).toEqual([0.1, 0.2]);
  });
});

describe('applyExamplePatch', () => {
  it('updates only provided fields and bumps updatedAt', async () => {
    const ex = materializeExample(makeInput({ output: 'old' }));
    await new Promise((r) => setTimeout(r, 5));
    const patched = applyExamplePatch(ex, { output: 'new' });
    expect(patched.output).toBe('new');
    expect(patched.input).toBe(ex.input);
    expect(patched.updatedAt > ex.updatedAt).toBe(true);
  });
});

describe('InMemoryExampleStore — CRUD', () => {
  it('add persists with materialized id + timestamps', async () => {
    const store = new InMemoryExampleStore();
    const ex = await store.add(makeInput({ input: 'q1' }));
    expect(await store.get(ex.id)).toEqual(ex);
  });

  it('addBatch persists multiple', async () => {
    const store = new InMemoryExampleStore();
    const exs = await store.addBatch([
      makeInput({ input: 'q1' }),
      makeInput({ input: 'q2' }),
    ]);
    expect(exs).toHaveLength(2);
  });

  it('update applies patch + bumps updatedAt', async () => {
    const store = new InMemoryExampleStore();
    const ex = await store.add(makeInput({ output: 'old' }));
    const updated = await store.update(ex.id, { output: 'new', score: 0.9 });
    expect(updated?.output).toBe('new');
    expect(updated?.score).toBe(0.9);
  });

  it('delete returns true on hit, false on miss', async () => {
    const store = new InMemoryExampleStore();
    const ex = await store.add(makeInput());
    expect(await store.delete(ex.id)).toBe(true);
    expect(await store.delete(ex.id)).toBe(false);
  });

  it('list scopes by agentId', async () => {
    const store = new InMemoryExampleStore();
    await store.add(makeInput({ agentId: 'triage' }));
    await store.add(makeInput({ agentId: 'billing' }));
    expect(await store.list({ agentId: 'triage' })).toHaveLength(1);
  });

  it('list filters by intent + tags + minScore', async () => {
    const store = new InMemoryExampleStore();
    await store.add(makeInput({ intent: 'classify', tags: ['x'], score: 0.4 }));
    await store.add(makeInput({ intent: 'classify', tags: ['y'], score: 0.9 }));
    await store.add(makeInput({ intent: 'extract', tags: ['x'], score: 0.5 }));

    expect(await store.list({ agentId: 'triage', intent: 'classify' })).toHaveLength(2);
    expect(await store.list({ agentId: 'triage', tags: ['x'] })).toHaveLength(2);
    expect(await store.list({ agentId: 'triage', minScore: 0.6 })).toHaveLength(1);
  });

  it('list excludes rows with no score when minScore is set', async () => {
    const store = new InMemoryExampleStore();
    await store.add(makeInput()); // no score
    expect(await store.list({ agentId: 'triage', minScore: 0.5 })).toHaveLength(0);
  });
});

describe('InMemoryExampleStore — findSimilar', () => {
  it('ranks by cosine similarity to the query', async () => {
    const store = new InMemoryExampleStore({ embeddingFn: makeToyEmbedder() });
    await store.add(makeInput({ input: 'apple apple' }));
    await store.add(makeInput({ input: 'zebra zebra' }));
    const results = await store.findSimilar({ agentId: 'triage', query: 'apple', k: 2 });
    expect(results[0].example.input).toBe('apple apple');
    expect(results[0].score).toBeGreaterThan(results[1].score);
  });

  it('embeds the input field, not the output', async () => {
    const calls: string[][] = [];
    const fn: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => [1, 0, 0]);
    };
    const store = new InMemoryExampleStore({ embeddingFn: fn });
    await store.add(makeInput({ input: 'IN', output: 'OUT' }));
    expect(calls[0]).toEqual(['IN']);
  });

  it('embedder is called on update when input changes', async () => {
    const calls: string[][] = [];
    const fn: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => [1, 0, 0]);
    };
    const store = new InMemoryExampleStore({ embeddingFn: fn });
    const ex = await store.add(makeInput({ input: 'X' }));
    await store.update(ex.id, { input: 'Y' });
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual(['Y']);
  });

  it('falls back to recency when no embedder', async () => {
    const store = new InMemoryExampleStore();
    await store.add(makeInput({ input: 'older' }));
    await new Promise((r) => setTimeout(r, 5));
    await store.add(makeInput({ input: 'newer' }));
    const results = await store.findSimilar({ agentId: 'triage', query: 'x', k: 2 });
    expect(results[0].example.input).toBe('newer');
  });

  it('respects intent + tag filters during findSimilar', async () => {
    const store = new InMemoryExampleStore({ embeddingFn: makeToyEmbedder() });
    await store.add(makeInput({ input: 'apple', intent: 'classify' }));
    await store.add(makeInput({ input: 'apple', intent: 'extract' }));
    const results = await store.findSimilar({
      agentId: 'triage',
      query: 'apple',
      intent: 'classify',
      k: 5,
    });
    expect(results).toHaveLength(1);
    expect(results[0].example.intent).toBe('classify');
  });
});

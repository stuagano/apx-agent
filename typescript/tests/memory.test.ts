import { describe, it, expect } from 'vitest';
import {
  InMemoryMemoryStore,
  cosineSimilarity,
  materializeMemory,
  applyPatch,
  tagsOverlap,
  type EmbeddingFn,
  type MemoryInput,
} from '../src/memory.js';

// Deterministic toy embedder: maps the first letter of each word to a position.
// "apple banana" → vector with bumps at the letter positions of 'a' and 'b'.
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
  return {
    principalId: 'user-1',
    content: 'apple banana',
    ...overrides,
  };
}

describe('cosineSimilarity', () => {
  it('returns 1 for identical vectors', () => {
    expect(cosineSimilarity([1, 0, 0], [1, 0, 0])).toBeCloseTo(1);
  });

  it('returns 0 for orthogonal vectors', () => {
    expect(cosineSimilarity([1, 0], [0, 1])).toBeCloseTo(0);
  });

  it('returns 0 for empty or mismatched-length vectors', () => {
    expect(cosineSimilarity([], [1])).toBe(0);
    expect(cosineSimilarity([1, 2], [1])).toBe(0);
  });

  it('returns 0 for all-zero vector', () => {
    expect(cosineSimilarity([0, 0, 0], [1, 1, 1])).toBe(0);
  });
});

describe('tagsOverlap', () => {
  it('matches when at least one tag is shared', () => {
    expect(tagsOverlap(['a', 'b'], ['b', 'c'])).toBe(true);
  });

  it('rejects when no tags overlap', () => {
    expect(tagsOverlap(['a'], ['b'])).toBe(false);
  });

  it('returns true when filter is empty or undefined', () => {
    expect(tagsOverlap(['a'], [])).toBe(true);
    expect(tagsOverlap(['a'], undefined)).toBe(true);
  });
});

describe('materializeMemory', () => {
  it('mints an id and timestamps when omitted', () => {
    const m = materializeMemory({ principalId: 'u', content: 'hi' });
    expect(m.id).toMatch(/^mem_/);
    expect(m.createdAt).toBeTypeOf('string');
    expect(m.updatedAt).toBe(m.createdAt);
    expect(m.namespace).toBe('default');
    expect(m.importance).toBe(0.5);
  });

  it('preserves caller-supplied id', () => {
    const m = materializeMemory({ principalId: 'u', content: 'hi', id: 'fixed' });
    expect(m.id).toBe('fixed');
  });

  it('freezes tags + metadata', () => {
    const m = materializeMemory({ principalId: 'u', content: 'hi', tags: ['a'] });
    expect(() => (m.tags as string[]).push('b')).toThrow();
    expect(Object.isFrozen(m.metadata)).toBe(true);
  });

  it('attaches embedding when provided', () => {
    const m = materializeMemory({ principalId: 'u', content: 'hi' }, [1, 2, 3]);
    expect(m.embedding).toEqual([1, 2, 3]);
    expect(Object.isFrozen(m.embedding)).toBe(true);
  });
});

describe('applyPatch', () => {
  it('updates only provided fields and bumps updatedAt', async () => {
    const m = materializeMemory({ principalId: 'u', content: 'old', importance: 0.3 });
    await new Promise((r) => setTimeout(r, 5));
    const patched = applyPatch(m, { content: 'new' });
    expect(patched.content).toBe('new');
    expect(patched.importance).toBe(0.3);
    expect(patched.updatedAt > m.updatedAt).toBe(true);
  });
});

describe('InMemoryMemoryStore — CRUD', () => {
  it('add returns a materialized memory and persists it', async () => {
    const store = new InMemoryMemoryStore();
    const m = await store.add(makeInput({ content: 'hello' }));
    expect(m.content).toBe('hello');
    expect(await store.get(m.id)).toEqual(m);
  });

  it('addBatch persists multiple', async () => {
    const store = new InMemoryMemoryStore();
    const ms = await store.addBatch([
      makeInput({ content: 'a' }),
      makeInput({ content: 'b' }),
    ]);
    expect(ms).toHaveLength(2);
    expect(await store.list({ principalId: 'user-1' })).toHaveLength(2);
  });

  it('update applies patch + bumps updatedAt', async () => {
    const store = new InMemoryMemoryStore();
    const m = await store.add(makeInput({ content: 'old' }));
    const updated = await store.update(m.id, { content: 'new', importance: 0.9 });
    expect(updated?.content).toBe('new');
    expect(updated?.importance).toBe(0.9);
  });

  it('update returns null for missing id', async () => {
    const store = new InMemoryMemoryStore();
    expect(await store.update('nope', { content: 'x' })).toBeNull();
  });

  it('delete returns true on hit, false on miss', async () => {
    const store = new InMemoryMemoryStore();
    const m = await store.add(makeInput());
    expect(await store.delete(m.id)).toBe(true);
    expect(await store.delete(m.id)).toBe(false);
  });

  it('list scopes by principalId', async () => {
    const store = new InMemoryMemoryStore();
    await store.add(makeInput({ principalId: 'a', content: 'a-mem' }));
    await store.add(makeInput({ principalId: 'b', content: 'b-mem' }));
    const aResults = await store.list({ principalId: 'a' });
    expect(aResults).toHaveLength(1);
    expect(aResults[0].content).toBe('a-mem');
  });

  it('list filters by namespace + tags + minImportance', async () => {
    const store = new InMemoryMemoryStore();
    await store.add(makeInput({ content: 'm1', namespace: 'profile', tags: ['x'], importance: 0.2 }));
    await store.add(makeInput({ content: 'm2', namespace: 'profile', tags: ['y'], importance: 0.9 }));
    await store.add(makeInput({ content: 'm3', namespace: 'episodic', tags: ['x'], importance: 0.5 }));

    expect(await store.list({ principalId: 'user-1', namespace: 'profile' })).toHaveLength(2);
    expect(await store.list({ principalId: 'user-1', tags: ['x'] })).toHaveLength(2);
    expect(await store.list({ principalId: 'user-1', minImportance: 0.6 })).toHaveLength(1);
  });

  it('list respects limit and sorts by updatedAt desc', async () => {
    const store = new InMemoryMemoryStore();
    await store.add(makeInput({ content: 'first' }));
    await new Promise((r) => setTimeout(r, 5));
    await store.add(makeInput({ content: 'second' }));
    const rows = await store.list({ principalId: 'user-1', limit: 1 });
    expect(rows[0].content).toBe('second');
  });
});

describe('InMemoryMemoryStore — recall', () => {
  it('uses cosine similarity when embedder is wired', async () => {
    const store = new InMemoryMemoryStore({ embeddingFn: makeToyEmbedder() });
    await store.add(makeInput({ content: 'apple apple apple' }));
    await store.add(makeInput({ content: 'zebra zebra zebra' }));

    const results = await store.recall({ principalId: 'user-1', query: 'apple', k: 2 });
    expect(results[0].memory.content).toBe('apple apple apple');
    expect(results[0].score).toBeGreaterThan(results[1].score);
  });

  it('embedder is called on add to persist embeddings', async () => {
    const calls: string[][] = [];
    const fn: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => [1, 0, 0]);
    };
    const store = new InMemoryMemoryStore({ embeddingFn: fn });
    const m = await store.add(makeInput({ content: 'X' }));
    expect(calls[0]).toEqual(['X']);
    expect(m.embedding).toEqual([1, 0, 0]);
  });

  it('embedder is called on update when content changes', async () => {
    const calls: string[][] = [];
    const fn: EmbeddingFn = async (texts) => {
      calls.push([...texts]);
      return texts.map(() => [1, 0, 0]);
    };
    const store = new InMemoryMemoryStore({ embeddingFn: fn });
    const m = await store.add(makeInput({ content: 'X' }));
    await store.update(m.id, { content: 'Y' });
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual(['Y']);
  });

  it('falls back to recency when no embedder', async () => {
    const store = new InMemoryMemoryStore();
    await store.add(makeInput({ content: 'old' }));
    await new Promise((r) => setTimeout(r, 5));
    await store.add(makeInput({ content: 'new' }));
    const results = await store.recall({ principalId: 'user-1', query: 'anything', k: 2 });
    expect(results[0].memory.content).toBe('new');
    expect(results[0].score).toBeGreaterThan(results[1].score);
  });

  it('respects namespace + tag filters during recall', async () => {
    const store = new InMemoryMemoryStore({ embeddingFn: makeToyEmbedder() });
    await store.add(makeInput({ content: 'apple', namespace: 'a' }));
    await store.add(makeInput({ content: 'apple', namespace: 'b' }));
    const results = await store.recall({
      principalId: 'user-1',
      query: 'apple',
      namespace: 'a',
      k: 5,
    });
    expect(results).toHaveLength(1);
    expect(results[0].memory.namespace).toBe('a');
  });

  it('returns empty when no candidates exist', async () => {
    const store = new InMemoryMemoryStore({ embeddingFn: makeToyEmbedder() });
    expect(await store.recall({ principalId: 'nobody', query: 'x' })).toEqual([]);
  });
});

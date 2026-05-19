import { describe, it, expect } from 'vitest';
import { consolidateMemories } from '../src/memory-consolidate.js';
import { InMemoryMemoryStore, type Memory } from '../src/memory.js';

async function seedMany(
  store: InMemoryMemoryStore,
  principalId: string,
  count: number,
  opts: Partial<{ namespace: string; importance: number; tag: string }> = {},
): Promise<Memory[]> {
  const out: Memory[] = [];
  for (let i = 0; i < count; i++) {
    out.push(
      await store.add({
        principalId,
        content: `memo-${i}`,
        namespace: opts.namespace,
        importance: opts.importance,
        tags: opts.tag ? [opts.tag] : undefined,
      }),
    );
  }
  return out;
}

describe('consolidateMemories', () => {
  it('writes a consolidated memory and deletes originals by default', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      minMemoriesForConsolidation: 5,
    });
    expect(result.candidatesFound).toBe(5);
    expect(result.consolidatedMemory).not.toBeNull();
    expect(result.consolidatedMemory!.content).toBe('summary');
    expect(result.deletedIds).toHaveLength(5);
    const remaining = await store.list({ principalId: 'user-1' });
    // Only the new consolidated memory remains.
    expect(remaining).toHaveLength(1);
    expect(remaining[0].namespace).toBe('consolidated');
  });

  it('returns early when fewer than minMemoriesForConsolidation match', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 3);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'never called',
      minMemoriesForConsolidation: 5,
    });
    expect(result.candidatesFound).toBe(3);
    expect(result.consolidatedMemory).toBeNull();
    expect(result.deletedIds).toHaveLength(0);
    const remaining = await store.list({ principalId: 'user-1' });
    expect(remaining).toHaveLength(3);
  });

  it('keepOriginals=true skips deletion', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      keepOriginals: true,
    });
    expect(result.deletedIds).toHaveLength(0);
    const remaining = await store.list({ principalId: 'user-1' });
    // 5 originals + 1 consolidated.
    expect(remaining).toHaveLength(6);
  });

  it('minImportance filters candidates', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 3, { importance: 0.2 });
    await seedMany(store, 'user-1', 5, { importance: 0.9 });
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      minImportance: 0.5,
      minMemoriesForConsolidation: 5,
    });
    expect(result.candidatesFound).toBe(5);
  });

  it('namespace filter scopes candidates', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5, { namespace: 'episodic' });
    await seedMany(store, 'user-1', 5, { namespace: 'profile' });
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      namespace: 'episodic',
      summarizeFn: () => 'summary',
    });
    expect(result.candidatesFound).toBe(5);
    expect(result.consolidatedMemory!.namespace).toBe('consolidated');
  });

  it('dryRun materializes consolidated memory without writing or deleting', async () => {
    const store = new InMemoryMemoryStore();
    const originals = await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      dryRun: true,
    });
    expect(result.consolidatedMemory).not.toBeNull();
    expect(result.deletedIds).toHaveLength(0);
    const remaining = await store.list({ principalId: 'user-1' });
    // 5 originals still present, no consolidated row.
    expect(remaining).toHaveLength(5);
    expect(remaining.map((m) => m.id).sort()).toEqual(
      originals.map((m) => m.id).sort(),
    );
  });

  it('summarizeFn receives the candidate Memory rows', async () => {
    const store = new InMemoryMemoryStore();
    const seeded = await seedMany(store, 'user-1', 5);
    let received: readonly Memory[] | null = null;
    await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: (mems) => {
        received = mems;
        return 'summary';
      },
    });
    expect(received).not.toBeNull();
    expect(received!).toHaveLength(5);
    const ids = received!.map((m) => m.id).sort();
    expect(ids).toEqual(seeded.map((m) => m.id).sort());
  });

  it('consolidated memory metadata carries source ids and count', async () => {
    const store = new InMemoryMemoryStore();
    const seeded = await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      keepOriginals: true,
    });
    const meta = result.consolidatedMemory!.metadata as Record<string, unknown>;
    expect(meta.count).toBe(5);
    expect((meta.consolidatedFrom as string[]).sort()).toEqual(
      seeded.map((m) => m.id).sort(),
    );
  });

  it('consolidatedTags / namespace / importance respected', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      consolidatedNamespace: 'rollup',
      consolidatedTags: ['rollup', 'auto'],
      consolidatedImportance: 0.4,
    });
    const m = result.consolidatedMemory!;
    expect(m.namespace).toBe('rollup');
    expect(m.tags).toEqual(['rollup', 'auto']);
    expect(m.importance).toBe(0.4);
  });

  it('async summarizeFn is awaited', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: async () => 'async-summary',
    });
    expect(result.consolidatedMemory!.content).toBe('async-summary');
  });

  it('maxAgeSeconds drops too-recent memories', async () => {
    const store = new InMemoryMemoryStore();
    // Add 5 fresh memories — they are seconds old, not minutes.
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      // Require memories to be at least 1 hour old; none qualify.
      maxAgeSeconds: 3600,
      minMemoriesForConsolidation: 1,
    });
    expect(result.candidatesFound).toBe(0);
    expect(result.consolidatedMemory).toBeNull();
  });

  it('maxAgeSeconds=0 admits all memories (they are all older than "now")', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    // Wait a moment so updatedAt < now.
    await new Promise((r) => setTimeout(r, 5));
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      maxAgeSeconds: 0,
    });
    expect(result.candidatesFound).toBe(5);
  });

  it('returns frozen ConsolidateResult', async () => {
    const store = new InMemoryMemoryStore();
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'never',
    });
    expect(Object.isFrozen(result)).toBe(true);
  });

  it('principalId scopes candidates', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    await seedMany(store, 'user-2', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
    });
    expect(result.candidatesFound).toBe(5);
    const user2Remaining = await store.list({ principalId: 'user-2' });
    expect(user2Remaining).toHaveLength(5);
  });

  it('does not delete other principals memories', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    await seedMany(store, 'user-2', 3);
    await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
    });
    const user2 = await store.list({ principalId: 'user-2' });
    expect(user2).toHaveLength(3);
  });

  it('dryRun id starts with mem_ prefix', async () => {
    const store = new InMemoryMemoryStore();
    await seedMany(store, 'user-1', 5);
    const result = await consolidateMemories({
      store,
      principalId: 'user-1',
      summarizeFn: () => 'summary',
      dryRun: true,
    });
    expect(result.consolidatedMemory!.id).toMatch(/^mem_/);
  });
});

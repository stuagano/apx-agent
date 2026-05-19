/**
 * Tests for memory-tools — agent-facing tools wrapping a MemoryStore.
 */

import { describe, it, expect, vi } from 'vitest';
import { makeMemoryTools } from '../src/memory-tools.js';
import { InMemoryMemoryStore } from '../src/memory.js';
import type { AgentTool } from '../src/agent/tools.js';

function findTool(tools: AgentTool[], name: string): AgentTool {
  const t = tools.find((x) => x.name === name);
  if (!t) throw new Error(`tool not found: ${name} (have: ${tools.map((x) => x.name).join(', ')})`);
  return t;
}

// ---------------------------------------------------------------------------
// Build / include / prefix
// ---------------------------------------------------------------------------

describe('makeMemoryTools — build', () => {
  it('returns three tools by default', () => {
    const tools = makeMemoryTools({ store: new InMemoryMemoryStore() });
    expect(tools.map((t) => t.name).sort()).toEqual(['forget', 'recall', 'remember']);
  });

  it('honors include filter', () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      include: ['recall'],
    });
    expect(tools.map((t) => t.name)).toEqual(['recall']);
  });

  it('honors include with two-tool subset', () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      include: ['recall', 'forget'],
    });
    expect(tools.map((t) => t.name).sort()).toEqual(['forget', 'recall']);
  });

  it('applies toolPrefix to every tool name', () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      toolPrefix: 'memory_',
    });
    expect(tools.map((t) => t.name).sort()).toEqual([
      'memory_forget',
      'memory_recall',
      'memory_remember',
    ]);
  });
});

// ---------------------------------------------------------------------------
// principalIdResolver
// ---------------------------------------------------------------------------

describe('makeMemoryTools — principalIdResolver', () => {
  it('uses resolver result when present', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'alice', content: 'remember me' });
    const tools = makeMemoryTools({
      store,
      principalIdResolver: () => 'alice',
    });
    const out = await findTool(tools, 'recall').handler({ query: 'me' });
    expect(out).toContain('remember me');
  });

  it('falls back to defaultPrincipalId when resolver returns undefined', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'bob', content: 'fallback works' });
    const tools = makeMemoryTools({
      store,
      principalIdResolver: () => undefined,
      defaultPrincipalId: 'bob',
    });
    const out = await findTool(tools, 'recall').handler({ query: 'fallback' });
    expect(out).toContain('fallback works');
  });

  it('returns no-principal string when neither resolver nor default produces id', async () => {
    const store = new InMemoryMemoryStore();
    const tools = makeMemoryTools({ store });
    const out = (await findTool(tools, 'recall').handler({ query: 'x' })) as string;
    expect(out).toBe('No principal_id available; cannot recall memories.');
  });

  it('remember also degrades gracefully without principal', async () => {
    const store = new InMemoryMemoryStore();
    const tools = makeMemoryTools({ store });
    const out = (await findTool(tools, 'remember').handler({ content: 'x' })) as string;
    expect(out).toBe('No principal_id available; cannot recall memories.');
  });
});

// ---------------------------------------------------------------------------
// recall — happy path + formatting
// ---------------------------------------------------------------------------

describe('memory_recall handler', () => {
  it('returns "No memories found." when store empty', async () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      defaultPrincipalId: 'u1',
    });
    expect(await findTool(tools, 'recall').handler({ query: 'x' })).toBe(
      'No memories found.',
    );
  });

  it('formats results as markdown list with score and content', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'first thought' });
    await store.add({ principalId: 'u1', content: 'second thought' });
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    const out = (await findTool(tools, 'recall').handler({ query: 'thought' })) as string;
    expect(out).toMatch(/^- \[score=\d\.\d{2}\] /m);
    expect(out).toContain('first thought');
    expect(out).toContain('second thought');
  });

  it('passes namespace filter through', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'profile fact', namespace: 'profile' });
    await store.add({ principalId: 'u1', content: 'episodic fact', namespace: 'episodic' });
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    const out = (await findTool(tools, 'recall').handler({
      query: 'fact',
      namespace: 'profile',
    })) as string;
    expect(out).toContain('profile fact');
    expect(out).not.toContain('episodic fact');
  });

  it('passes tags filter through', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'pinned', tags: ['pin'] });
    await store.add({ principalId: 'u1', content: 'unpinned', tags: ['casual'] });
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    const out = (await findTool(tools, 'recall').handler({
      query: 'x',
      tags: ['pin'],
    })) as string;
    expect(out).toContain('pinned');
    expect(out).not.toContain('unpinned');
  });

  it('rejects invalid k via Zod', async () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      defaultPrincipalId: 'u1',
    });
    await expect(
      findTool(tools, 'recall').handler({ query: 'x', k: -1 }),
    ).rejects.toThrow();
  });

  it('rejects missing query via Zod', async () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      defaultPrincipalId: 'u1',
    });
    await expect(findTool(tools, 'recall').handler({})).rejects.toThrow();
  });

  it('honors namespaceDefault when handler omits namespace', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'in profile', namespace: 'profile' });
    await store.add({ principalId: 'u1', content: 'in other', namespace: 'other' });
    const tools = makeMemoryTools({
      store,
      defaultPrincipalId: 'u1',
      namespaceDefault: 'profile',
    });
    const out = (await findTool(tools, 'recall').handler({ query: 'x' })) as string;
    expect(out).toContain('in profile');
    expect(out).not.toContain('in other');
  });
});

// ---------------------------------------------------------------------------
// remember
// ---------------------------------------------------------------------------

describe('memory_remember handler', () => {
  it('persists a memory and returns saved-id string', async () => {
    const store = new InMemoryMemoryStore();
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    const out = (await findTool(tools, 'remember').handler({
      content: 'new memory',
    })) as string;
    expect(out).toMatch(/^Saved memory mem_/);
    const list = await store.list({ principalId: 'u1' });
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe('new memory');
  });

  it('passes namespace, tags, and importance through to add()', async () => {
    const store = new InMemoryMemoryStore();
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    await findTool(tools, 'remember').handler({
      content: 'fact',
      namespace: 'profile',
      tags: ['high'],
      importance: 0.95,
    });
    const list = await store.list({ principalId: 'u1' });
    expect(list[0]!.namespace).toBe('profile');
    expect(list[0]!.tags).toEqual(['high']);
    expect(list[0]!.importance).toBeCloseTo(0.95);
  });

  it('rejects importance outside [0,1]', async () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      defaultPrincipalId: 'u1',
    });
    await expect(
      findTool(tools, 'remember').handler({ content: 'x', importance: 1.5 }),
    ).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// forget
// ---------------------------------------------------------------------------

describe('memory_forget handler', () => {
  it('deletes a memory by id and returns success message', async () => {
    const store = new InMemoryMemoryStore();
    const m = await store.add({ principalId: 'u1', content: 'doomed' });
    const tools = makeMemoryTools({ store, defaultPrincipalId: 'u1' });
    const out = (await findTool(tools, 'forget').handler({ id: m.id })) as string;
    expect(out).toBe(`Forgot memory ${m.id}.`);
    expect(await store.get(m.id)).toBeNull();
  });

  it('returns missing-id message when no such memory', async () => {
    const tools = makeMemoryTools({
      store: new InMemoryMemoryStore(),
      defaultPrincipalId: 'u1',
    });
    const out = (await findTool(tools, 'forget').handler({ id: 'mem_xyz' })) as string;
    expect(out).toBe('No memory found with id mem_xyz.');
  });

  it('forget works without principal (deletion is principal-agnostic)', async () => {
    const store = new InMemoryMemoryStore();
    const m = await store.add({ principalId: 'u1', content: 'x' });
    const tools = makeMemoryTools({ store }); // no defaultPrincipalId
    const out = (await findTool(tools, 'forget').handler({ id: m.id })) as string;
    expect(out).toContain('Forgot');
  });
});

// ---------------------------------------------------------------------------
// Resolver-vs-context call pattern
// ---------------------------------------------------------------------------

describe('makeMemoryTools — resolver is called per-handler-invocation', () => {
  it('reflects resolver state changes between calls', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'alice', content: 'alice-mem' });
    await store.add({ principalId: 'bob', content: 'bob-mem' });
    let current: string | undefined = 'alice';
    const tools = makeMemoryTools({
      store,
      principalIdResolver: () => current,
    });
    const recall = findTool(tools, 'recall');
    const out1 = (await recall.handler({ query: 'mem' })) as string;
    expect(out1).toContain('alice-mem');
    current = 'bob';
    const out2 = (await recall.handler({ query: 'mem' })) as string;
    expect(out2).toContain('bob-mem');
    current = undefined;
    const out3 = (await recall.handler({ query: 'mem' })) as string;
    expect(out3).toContain('No principal_id available');
  });

  it('resolver result takes precedence over defaultPrincipalId', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'resolved', content: 'r' });
    await store.add({ principalId: 'default', content: 'd' });
    const tools = makeMemoryTools({
      store,
      principalIdResolver: () => 'resolved',
      defaultPrincipalId: 'default',
    });
    const out = (await findTool(tools, 'recall').handler({ query: 'x' })) as string;
    expect(out).toContain('r');
    expect(out).not.toContain('d');
  });
});

/**
 * Tests for prompt-assembly — system-prompt-ready context strings.
 */

import { describe, it, expect } from 'vitest';
import {
  assembleMemoryContext,
  assembleExampleContext,
  assembleContext,
} from '../src/prompt-assembly.js';
import { InMemoryMemoryStore } from '../src/memory.js';
import { InMemoryExampleStore } from '../src/example.js';

// ---------------------------------------------------------------------------
// assembleMemoryContext
// ---------------------------------------------------------------------------

describe('assembleMemoryContext', () => {
  it('returns empty string when store empty', async () => {
    const store = new InMemoryMemoryStore();
    const out = await assembleMemoryContext(store, { principalId: 'u1', query: 'x' });
    expect(out).toBe('');
  });

  it('emits default header + one bullet for single match', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'first thought' });
    const out = await assembleMemoryContext(store, { principalId: 'u1', query: 'x' });
    expect(out).toBe('### Relevant memories\n- first thought');
  });

  it('emits header + multiple bullets for multi-match', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'one' });
    await store.add({ principalId: 'u1', content: 'two' });
    const out = await assembleMemoryContext(store, { principalId: 'u1', query: 'x', k: 5 });
    expect(out.startsWith('### Relevant memories\n')).toBe(true);
    expect(out).toContain('- one');
    expect(out).toContain('- two');
    expect(out.split('\n').length).toBe(3);
  });

  it('honors custom header', async () => {
    const store = new InMemoryMemoryStore();
    await store.add({ principalId: 'u1', content: 'item' });
    const out = await assembleMemoryContext(
      store,
      { principalId: 'u1', query: 'x' },
      '## Memories',
    );
    expect(out).toBe('## Memories\n- item');
  });

  it('respects k limit', async () => {
    const store = new InMemoryMemoryStore();
    for (let i = 0; i < 10; i++) {
      await store.add({ principalId: 'u1', content: `m${i}` });
    }
    const out = await assembleMemoryContext(store, {
      principalId: 'u1',
      query: 'x',
      k: 3,
    });
    expect(out.split('\n').length).toBe(4); // header + 3 bullets
  });
});

// ---------------------------------------------------------------------------
// assembleExampleContext
// ---------------------------------------------------------------------------

describe('assembleExampleContext', () => {
  it('returns empty string when store empty', async () => {
    const store = new InMemoryExampleStore();
    expect(
      await assembleExampleContext(store, { agentId: 'a1', query: 'x' }),
    ).toBe('');
  });

  it('emits Q/A block for single match', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'q?', output: 'a.' });
    const out = await assembleExampleContext(store, { agentId: 'a1', query: 'x' });
    expect(out).toBe('### Relevant examples\n**Q:** q?\n**A:** a.');
  });

  it('separates multiple matches with ---', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'q1', output: 'a1' });
    await store.add({ agentId: 'a1', input: 'q2', output: 'a2' });
    const out = await assembleExampleContext(store, {
      agentId: 'a1',
      query: 'x',
      k: 5,
    });
    expect(out).toContain('**Q:** q1');
    expect(out).toContain('**Q:** q2');
    expect(out).toContain('\n---\n');
  });

  it('honors custom header', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'q', output: 'a' });
    const out = await assembleExampleContext(
      store,
      { agentId: 'a1', query: 'x' },
      '## Examples',
    );
    expect(out.startsWith('## Examples\n')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// assembleContext — combined
// ---------------------------------------------------------------------------

describe('assembleContext (combined)', () => {
  it('returns empty when neither store has rows', async () => {
    const memory = new InMemoryMemoryStore();
    const examples = new InMemoryExampleStore();
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
    });
    expect(out).toBe('');
  });

  it('joins both blocks with default separator when both stores have rows', async () => {
    const memory = new InMemoryMemoryStore();
    await memory.add({ principalId: 'u1', content: 'fact-1' });
    const examples = new InMemoryExampleStore();
    await examples.add({ agentId: 'a1', input: 'q', output: 'a' });
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
    });
    expect(out).toContain('### Relevant memories');
    expect(out).toContain('- fact-1');
    expect(out).toContain('### Relevant examples');
    expect(out).toContain('**Q:** q');
    expect(out).toContain('\n\n');
  });

  it('returns only memory block when examples store empty', async () => {
    const memory = new InMemoryMemoryStore();
    await memory.add({ principalId: 'u1', content: 'fact' });
    const examples = new InMemoryExampleStore();
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
    });
    expect(out.startsWith('### Relevant memories')).toBe(true);
    expect(out).not.toContain('### Relevant examples');
  });

  it('returns only examples block when memory store empty', async () => {
    const memory = new InMemoryMemoryStore();
    const examples = new InMemoryExampleStore();
    await examples.add({ agentId: 'a1', input: 'q', output: 'a' });
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
    });
    expect(out.startsWith('### Relevant examples')).toBe(true);
    expect(out).not.toContain('### Relevant memories');
  });

  it('honors per-block custom headers', async () => {
    const memory = new InMemoryMemoryStore();
    await memory.add({ principalId: 'u1', content: 'fact' });
    const examples = new InMemoryExampleStore();
    await examples.add({ agentId: 'a1', input: 'q', output: 'a' });
    const out = await assembleContext({
      memory: {
        store: memory,
        opts: { principalId: 'u1', query: 'x' },
        header: '## M',
      },
      examples: {
        store: examples,
        opts: { agentId: 'a1', query: 'x' },
        header: '## E',
      },
    });
    expect(out).toContain('## M\n');
    expect(out).toContain('## E\n');
    expect(out).not.toContain('### Relevant');
  });

  it('honors custom separator', async () => {
    const memory = new InMemoryMemoryStore();
    await memory.add({ principalId: 'u1', content: 'a' });
    const examples = new InMemoryExampleStore();
    await examples.add({ agentId: 'a1', input: 'q', output: 'a' });
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
      separator: '\n\n===\n\n',
    });
    expect(out).toContain('\n\n===\n\n');
  });

  it('only memory passed — returns memory block only', async () => {
    const memory = new InMemoryMemoryStore();
    await memory.add({ principalId: 'u1', content: 'only-mem' });
    const out = await assembleContext({
      memory: { store: memory, opts: { principalId: 'u1', query: 'x' } },
    });
    expect(out).toBe('### Relevant memories\n- only-mem');
  });

  it('only examples passed — returns examples block only', async () => {
    const examples = new InMemoryExampleStore();
    await examples.add({ agentId: 'a1', input: 'qq', output: 'aa' });
    const out = await assembleContext({
      examples: { store: examples, opts: { agentId: 'a1', query: 'x' } },
    });
    expect(out).toBe('### Relevant examples\n**Q:** qq\n**A:** aa');
  });

  it('empty options object → empty string', async () => {
    expect(await assembleContext({})).toBe('');
  });
});

/**
 * Tests for example-tools — agent-facing tools wrapping an ExampleStore.
 */

import { describe, it, expect } from 'vitest';
import { makeExampleTools } from '../src/example-tools.js';
import { InMemoryExampleStore } from '../src/example.js';
import type { AgentTool } from '../src/agent/tools.js';

function findTool(tools: AgentTool[], name: string): AgentTool {
  const t = tools.find((x) => x.name === name);
  if (!t) throw new Error(`tool not found: ${name}`);
  return t;
}

// ---------------------------------------------------------------------------
// Build / include / prefix
// ---------------------------------------------------------------------------

describe('makeExampleTools — build', () => {
  it('returns three tools by default', () => {
    const tools = makeExampleTools({ store: new InMemoryExampleStore() });
    expect(tools.map((t) => t.name).sort()).toEqual([
      'find_examples',
      'remove_example',
      'save_example',
    ]);
  });

  it('honors include filter', () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      include: ['find_examples'],
    });
    expect(tools.map((t) => t.name)).toEqual(['find_examples']);
  });

  it('applies toolPrefix', () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      toolPrefix: 'ex_',
    });
    expect(tools.map((t) => t.name).sort()).toEqual([
      'ex_find_examples',
      'ex_remove_example',
      'ex_save_example',
    ]);
  });
});

// ---------------------------------------------------------------------------
// agentIdResolver
// ---------------------------------------------------------------------------

describe('makeExampleTools — agentIdResolver', () => {
  it('uses resolver when present', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'Q', output: 'A' });
    const tools = makeExampleTools({ store, agentIdResolver: () => 'a1' });
    const out = (await findTool(tools, 'find_examples').handler({ query: 'Q' })) as string;
    expect(out).toContain('**Q:** Q');
    expect(out).toContain('**A:** A');
  });

  it('falls back to defaultAgentId', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'Q', output: 'A' });
    const tools = makeExampleTools({
      store,
      agentIdResolver: () => undefined,
      defaultAgentId: 'a1',
    });
    const out = (await findTool(tools, 'find_examples').handler({ query: 'Q' })) as string;
    expect(out).toContain('**Q:** Q');
  });

  it('returns no-agent string when neither resolves', async () => {
    const store = new InMemoryExampleStore();
    const tools = makeExampleTools({ store });
    const out = (await findTool(tools, 'find_examples').handler({ query: 'x' })) as string;
    expect(out).toBe('No agent_id available; cannot look up examples.');
  });

  it('save_example also degrades gracefully without agent', async () => {
    const tools = makeExampleTools({ store: new InMemoryExampleStore() });
    const out = (await findTool(tools, 'save_example').handler({
      input: 'x',
      output: 'y',
    })) as string;
    expect(out).toBe('No agent_id available; cannot look up examples.');
  });
});

// ---------------------------------------------------------------------------
// find_examples
// ---------------------------------------------------------------------------

describe('find_examples handler', () => {
  it('returns "No examples found." when store empty', async () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      defaultAgentId: 'a1',
    });
    expect(await findTool(tools, 'find_examples').handler({ query: 'x' })).toBe(
      'No examples found.',
    );
  });

  it('formats as Q/A markdown blocks', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'first?', output: 'first-answer' });
    await store.add({ agentId: 'a1', input: 'second?', output: 'second-answer' });
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    const out = (await findTool(tools, 'find_examples').handler({ query: 'x' })) as string;
    expect(out).toMatch(/\*\*Q:\*\* first\?/);
    expect(out).toMatch(/\*\*A:\*\* first-answer/);
    expect(out).toMatch(/\*\*Q:\*\* second\?/);
  });

  it('passes intent filter through', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'p', output: 'a', intent: 'classify' });
    await store.add({ agentId: 'a1', input: 'q', output: 'b', intent: 'extract' });
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    const out = (await findTool(tools, 'find_examples').handler({
      query: 'x',
      intent: 'classify',
    })) as string;
    expect(out).toContain('**A:** a');
    expect(out).not.toContain('**A:** b');
  });

  it('passes tags filter through', async () => {
    const store = new InMemoryExampleStore();
    await store.add({ agentId: 'a1', input: 'p', output: 'gold-a', tags: ['gold'] });
    await store.add({ agentId: 'a1', input: 'q', output: 'bronze-a', tags: ['bronze'] });
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    const out = (await findTool(tools, 'find_examples').handler({
      query: 'x',
      tags: ['gold'],
    })) as string;
    expect(out).toContain('gold-a');
    expect(out).not.toContain('bronze-a');
  });

  it('honors intentDefault when handler omits intent', async () => {
    const store = new InMemoryExampleStore();
    await store.add({
      agentId: 'a1',
      input: 'p',
      output: 'in-classify',
      intent: 'classify',
    });
    await store.add({
      agentId: 'a1',
      input: 'q',
      output: 'in-other',
      intent: 'other',
    });
    const tools = makeExampleTools({
      store,
      defaultAgentId: 'a1',
      intentDefault: 'classify',
    });
    const out = (await findTool(tools, 'find_examples').handler({ query: 'x' })) as string;
    expect(out).toContain('in-classify');
    expect(out).not.toContain('in-other');
  });

  it('rejects missing query', async () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      defaultAgentId: 'a1',
    });
    await expect(findTool(tools, 'find_examples').handler({})).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// save_example
// ---------------------------------------------------------------------------

describe('save_example handler', () => {
  it('persists an example and returns saved-id message', async () => {
    const store = new InMemoryExampleStore();
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    const out = (await findTool(tools, 'save_example').handler({
      input: 'q',
      output: 'a',
    })) as string;
    expect(out).toMatch(/^Saved example ex_/);
    const list = await store.list({ agentId: 'a1' });
    expect(list).toHaveLength(1);
  });

  it('passes intent, score, tags through', async () => {
    const store = new InMemoryExampleStore();
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    await findTool(tools, 'save_example').handler({
      input: 'q',
      output: 'a',
      intent: 'classify',
      score: 0.9,
      tags: ['gold'],
    });
    const list = await store.list({ agentId: 'a1' });
    expect(list[0]!.intent).toBe('classify');
    expect(list[0]!.score).toBeCloseTo(0.9);
    expect(list[0]!.tags).toEqual(['gold']);
  });

  it('rejects score outside [0,1]', async () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      defaultAgentId: 'a1',
    });
    await expect(
      findTool(tools, 'save_example').handler({
        input: 'q',
        output: 'a',
        score: 1.5,
      }),
    ).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// remove_example
// ---------------------------------------------------------------------------

describe('remove_example handler', () => {
  it('deletes an example by id', async () => {
    const store = new InMemoryExampleStore();
    const ex = await store.add({ agentId: 'a1', input: 'q', output: 'a' });
    const tools = makeExampleTools({ store, defaultAgentId: 'a1' });
    const out = (await findTool(tools, 'remove_example').handler({ id: ex.id })) as string;
    expect(out).toBe(`Removed example ${ex.id}.`);
    expect(await store.get(ex.id)).toBeNull();
  });

  it('returns missing-id message when no such example', async () => {
    const tools = makeExampleTools({
      store: new InMemoryExampleStore(),
      defaultAgentId: 'a1',
    });
    const out = (await findTool(tools, 'remove_example').handler({
      id: 'ex_nope',
    })) as string;
    expect(out).toBe('No example found with id ex_nope.');
  });
});

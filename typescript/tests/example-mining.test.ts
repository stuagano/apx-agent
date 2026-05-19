import { describe, it, expect } from 'vitest';
import {
  mineExamples,
  pairTurns,
  type Turn,
} from '../src/example-mining.js';
import { InMemoryExampleStore } from '../src/example.js';
import {
  InMemorySessionStore,
  type SessionStore,
  type SessionSnapshot,
} from '../src/workflows/session.js';
import type { Message } from '../src/workflows/types.js';

function snap(id: string, messages: Message[]): SessionSnapshot {
  return {
    id,
    messages,
    state: {},
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  };
}

async function seedSessions(
  store: SessionStore,
  defs: Array<{ id: string; messages: Message[] }>,
): Promise<void> {
  for (const d of defs) {
    await store.save(d.id, snap(d.id, d.messages));
  }
}

describe('pairTurns', () => {
  it('pairs simple user→assistant', () => {
    const turns = pairTurns('s1', [
      { role: 'user', content: 'hi' },
      { role: 'assistant', content: 'hello' },
    ]);
    expect(turns).toHaveLength(1);
    expect(turns[0].turnIndex).toBe(0);
    expect(turns[0].sessionId).toBe('s1');
  });

  it('skips tool messages between user and assistant', () => {
    const turns = pairTurns('s1', [
      { role: 'user', content: 'ask' },
      { role: 'assistant', content: '' }, // tool-call only
      { role: 'tool', content: 'tool result' },
      { role: 'assistant', content: 'final answer' },
    ]);
    expect(turns).toHaveLength(1);
    expect(turns[0].assistantMessage.content).toBe('final answer');
  });

  it('emits multiple turns in one session', () => {
    const turns = pairTurns('s1', [
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'a1' },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a2' },
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0].turnIndex).toBe(0);
    expect(turns[1].turnIndex).toBe(1);
  });

  it('ignores leading assistant messages with no pending user', () => {
    const turns = pairTurns('s1', [
      { role: 'assistant', content: 'orphan' },
      { role: 'user', content: 'real' },
      { role: 'assistant', content: 'reply' },
    ]);
    expect(turns).toHaveLength(1);
    expect(turns[0].userMessage.content).toBe('real');
  });
});

describe('mineExamples', () => {
  it('mines a single (user, assistant) turn into the example store', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'hi' },
          { role: 'assistant', content: 'hello' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'triage',
    });
    expect(result.sessionsScanned).toBe(1);
    expect(result.turnsConsidered).toBe(1);
    expect(result.examplesAdded).toBe(1);
    expect(result.examples).toHaveLength(1);
    const stored = await exampleStore.list({ agentId: 'triage' });
    expect(stored).toHaveLength(1);
    expect(stored[0].input).toBe('hi');
    expect(stored[0].output).toBe('hello');
    expect(stored[0].intent).toBe('default');
  });

  it('default filterFn excludes empty assistant content', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'hi' },
          { role: 'assistant', content: '   ' }, // whitespace-only
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
    });
    // pairTurns rejects empty content; trimmed whitespace is non-empty content so
    // a turn IS emitted, but defaultFilter (which trims) rejects it.
    // Either way: nothing should be added.
    expect(result.examplesAdded).toBe(0);
  });

  it('intentFn output is forwarded to the example', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'bill' },
          { role: 'assistant', content: 'check' },
        ],
      },
    ]);
    await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      intentFn: () => 'billing',
    });
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.intent).toBe('billing');
  });

  it('async intentFn is awaited', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      intentFn: async () => 'classified',
    });
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.intent).toBe('classified');
  });

  it('scoreFn output is forwarded', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      scoreFn: () => 0.9,
    });
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.score).toBe(0.9);
  });

  it('minScore filters out low-quality turns', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'good' },
          { role: 'assistant', content: 'ok' },
          { role: 'user', content: 'bad' },
          { role: 'assistant', content: 'meh' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      scoreFn: (t: Turn) => (t.userMessage.content === 'good' ? 0.9 : 0.1),
      minScore: 0.5,
    });
    expect(result.examplesAdded).toBe(1);
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.input).toBe('good');
  });

  it('minScore does not drop turns whose scoreFn returned undefined', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      scoreFn: () => undefined,
      minScore: 0.9,
    });
    expect(result.examplesAdded).toBe(1);
  });

  it('limit caps the number of examples added', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'a1' },
          { role: 'user', content: 'q2' },
          { role: 'assistant', content: 'a2' },
          { role: 'user', content: 'q3' },
          { role: 'assistant', content: 'a3' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      limit: 2,
    });
    expect(result.examplesAdded).toBe(2);
  });

  it('dryRun returns examples without writing', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      dryRun: true,
    });
    expect(result.examples).toHaveLength(1);
    expect(result.examplesAdded).toBe(0);
    const stored = await exampleStore.list({ agentId: 'a' });
    expect(stored).toHaveLength(0);
  });

  it('mines across multiple sessions', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'a1' },
        ],
      },
      {
        id: 's2',
        messages: [
          { role: 'user', content: 'q2' },
          { role: 'assistant', content: 'a2' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
    });
    expect(result.sessionsScanned).toBe(2);
    expect(result.examplesAdded).toBe(2);
  });

  it('sessionIds subsets the scan', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'a1' },
        ],
      },
      {
        id: 's2',
        messages: [
          { role: 'user', content: 'q2' },
          { role: 'assistant', content: 'a2' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      sessionIds: ['s2'],
    });
    expect(result.sessionsScanned).toBe(1);
    expect(result.examplesAdded).toBe(1);
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.input).toBe('q2');
  });

  it('tagsFn and metadataFn are forwarded', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      tagsFn: () => ['mined', 'cold-start'],
      metadataFn: () => ({ source: 'mining' }),
    });
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.tags).toEqual(['mined', 'cold-start']);
    expect(ex.metadata.source).toBe('mining');
    expect(ex.metadata.sessionId).toBe('s1');
    expect(ex.metadata.turnIndex).toBe(0);
  });

  it('skips tool messages when forming pairs', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'ask' },
          { role: 'assistant', content: '' },
          { role: 'tool', content: '42' },
          { role: 'assistant', content: 'the answer is 42' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
    });
    expect(result.examplesAdded).toBe(1);
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.output).toBe('the answer is 42');
  });

  it('returns frozen MineResult', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await sessionStore.save('s1', snap('s1', []));
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
    });
    expect(Object.isFrozen(result)).toBe(true);
  });

  it('handles missing snapshots gracefully', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      sessionIds: ['ghost'],
    });
    expect(result.sessionsScanned).toBe(0);
    expect(result.examplesAdded).toBe(0);
  });

  it('custom filterFn overrides default empty-content filter', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'long answer' },
          { role: 'user', content: 'q2' },
          { role: 'assistant', content: 'short' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      filterFn: (t: Turn) => t.assistantMessage.content.length > 5,
    });
    expect(result.examplesAdded).toBe(1);
    const [ex] = await exampleStore.list({ agentId: 'a' });
    expect(ex.output).toBe('long answer');
  });

  it('turnsConsidered counts before filter', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'a1' },
          { role: 'user', content: 'q2' },
          { role: 'assistant', content: 'a2' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      filterFn: () => false,
    });
    expect(result.turnsConsidered).toBe(2);
    expect(result.examplesAdded).toBe(0);
  });

  it('dryRun materializes Examples with placeholder ids', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
      dryRun: true,
    });
    expect(result.examples[0].id).toMatch(/^ex_/);
  });

  it('agentId is forwarded to the example', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    await seedSessions(sessionStore, [
      {
        id: 's1',
        messages: [
          { role: 'user', content: 'q' },
          { role: 'assistant', content: 'a' },
        ],
      },
    ]);
    await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'custom-agent',
    });
    const [ex] = await exampleStore.list({ agentId: 'custom-agent' });
    expect(ex.agentId).toBe('custom-agent');
  });

  it('empty session list returns empty result', async () => {
    const sessionStore = new InMemorySessionStore();
    const exampleStore = new InMemoryExampleStore();
    const result = await mineExamples({
      sessionStore,
      exampleStore,
      agentId: 'a',
    });
    expect(result.sessionsScanned).toBe(0);
    expect(result.examples).toHaveLength(0);
  });
});

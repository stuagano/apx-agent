/**
 * Tests for `apx examples` — find / save / remove / list.
 *
 * Strategy mirrors `cli-memory.test.ts`: inject a seeded
 * `InMemoryExampleStore` through `deps.examples.loadStore` so the
 * filesystem importer never runs.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { buildProgram } from '../src/cli/index.js';
import { InMemoryExampleStore, type ExampleStore } from '../src/example.js';

let logSpy: ReturnType<typeof vi.spyOn>;
let errSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
  errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  logSpy.mockRestore();
  errSpy.mockRestore();
  vi.restoreAllMocks();
});

function stdout(): string {
  return logSpy.mock.calls.map((c) => c.join(' ')).join('\n');
}
function stderr(): string {
  return errSpy.mock.calls.map((c) => c.join(' ')).join('\n');
}

async function seededStore(): Promise<ExampleStore> {
  const store = new InMemoryExampleStore();
  await store.add({
    agentId: 'support-bot',
    intent: 'refund',
    input: 'I want my money back for order #42.',
    output: 'I have initiated a refund for order #42. Allow 3 business days.',
    tags: ['refund', 'order'],
    score: 0.9,
  });
  await store.add({
    agentId: 'support-bot',
    intent: 'shipping',
    input: 'Where is my package?',
    output: 'Your package is in transit and expected to arrive Tuesday.',
    tags: ['shipping'],
    score: 0.85,
  });
  await store.add({
    agentId: 'sales-bot',
    intent: 'pricing',
    input: 'How much is the pro plan?',
    output: 'The pro plan is $20/month.',
    tags: ['pricing'],
    score: 0.7,
  });
  return store;
}

async function runCli(
  args: string[],
  deps: Parameters<typeof buildProgram>[0] = {},
): Promise<void> {
  const program = buildProgram(deps);
  await program.parseAsync(['node', 'apx', ...args]);
}

// ---------------------------------------------------------------------------
// find
// ---------------------------------------------------------------------------

describe('apx examples find', () => {
  it('returns ranked examples as JSON by default', async () => {
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'find',
        '--agent-id',
        'support-bot',
        '--query',
        'where is my order',
        '-k',
        '5',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed.length).toBeGreaterThan(0);
    expect(parsed[0]).toHaveProperty('score');
    expect(parsed[0].example).toHaveProperty('agent_id', 'support-bot');
  });

  it('filters by intent', async () => {
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'find',
        '--agent-id',
        'support-bot',
        '--query',
        'anything',
        '--intent',
        'refund',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    for (const r of parsed) {
      expect(r.example.intent).toBe('refund');
    }
  });

  it('renders Q/A markdown blocks in text mode', async () => {
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'find',
        '--agent-id',
        'support-bot',
        '--query',
        'package',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const out = stdout();
    expect(out).toMatch(/\*\*Q:\*\* /);
    expect(out).toMatch(/\*\*A:\*\* /);
    expect(out).toContain('---');
  });

  it('exits 1 when the store fails to load', async () => {
    const exitSpy = vi.fn();
    await runCli(
      [
        'examples',
        'find',
        '--agent-id',
        'support-bot',
        '--query',
        'q',
        '--store-module',
        'bogus:store',
      ],
      {
        examples: {
          loadStore: async () => {
            throw new Error('Failed to import store module "bogus": ENOENT');
          },
          exit: exitSpy,
        },
      },
    );
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stderr()).toContain('Failed to import');
  });
});

// ---------------------------------------------------------------------------
// save
// ---------------------------------------------------------------------------

describe('apx examples save', () => {
  it('inserts an example and round-trips through JSON', async () => {
    const store = new InMemoryExampleStore();
    await runCli(
      [
        'examples',
        'save',
        '--agent-id',
        'support-bot',
        '--input',
        'reset my password',
        '--output',
        'I have sent a password reset link to your email.',
        '--intent',
        'auth',
        '--score',
        '0.88',
        '--tags',
        'auth,password',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed.agent_id).toBe('support-bot');
    expect(parsed.intent).toBe('auth');
    expect(parsed.input).toBe('reset my password');
    expect(parsed.score).toBeCloseTo(0.88);
    expect(parsed.tags).toEqual(['auth', 'password']);
  });

  it('renders "Saved example {id}" in text mode', async () => {
    const store = new InMemoryExampleStore();
    await runCli(
      [
        'examples',
        'save',
        '--agent-id',
        'support-bot',
        '--input',
        'q',
        '--output',
        'a',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    expect(stdout()).toMatch(/^Saved example ex_/);
  });

  it('rejects when required --output flag is missing', async () => {
    await expect(
      runCli(
        ['examples', 'save', '--agent-id', 'support-bot', '--input', 'q'],
        { examples: { loadStore: async () => new InMemoryExampleStore() } },
      ),
    ).rejects.toThrowError();
  });
});

// ---------------------------------------------------------------------------
// remove
// ---------------------------------------------------------------------------

describe('apx examples remove', () => {
  it('removes an example and emits {deleted: id} as JSON', async () => {
    const store = await seededStore();
    const list = await store.list({ agentId: 'support-bot' });
    const id = list[0]!.id;
    await runCli(
      ['examples', 'remove', '--id', id, '--store-module', 'fake:store'],
      { examples: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed).toEqual({ deleted: id });
    expect(await store.get(id)).toBeNull();
  });

  it('exits 1 when the example id is unknown', async () => {
    const exitSpy = vi.fn();
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'remove',
        '--id',
        'ex_missing',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store, exit: exitSpy } },
    );
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stderr()).toContain('No example with id ex_missing');
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('apx examples list', () => {
  it('lists examples scoped to an agent', async () => {
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'list',
        '--agent-id',
        'support-bot',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(2);
    for (const row of parsed) {
      expect(row.agent_id).toBe('support-bot');
    }
  });

  it('renders Q/A blocks in text mode', async () => {
    const store = await seededStore();
    await runCli(
      [
        'examples',
        'list',
        '--agent-id',
        'support-bot',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { examples: { loadStore: async () => store } },
    );
    const out = stdout();
    expect(out).toMatch(/# examples list for agent=/);
    expect(out).toMatch(/\*\*Q:\*\* /);
    expect(out).toMatch(/\*\*A:\*\* /);
  });

  it('falls back to [tool.apx.agent].example_store when --store-module is absent', async () => {
    const store = await seededStore();
    const readConfig = vi.fn(() => ({ example_store: 'configured:examples' }));
    const loadStore = vi.fn(async (spec: string) => {
      expect(spec).toBe('configured:examples');
      return store;
    });
    await runCli(['examples', 'list', '--agent-id', 'support-bot'], {
      examples: { loadStore, readConfig },
    });
    expect(loadStore).toHaveBeenCalledWith('configured:examples');
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(2);
  });
});

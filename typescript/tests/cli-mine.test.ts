/**
 * Tests for `apx examples mine` — wraps {@link mineExamples} with
 * file-injectable stores + callables.
 *
 * Strategy: build the commander program with stubbed `loadStore`,
 * `loadCallable`, and `readConfig` deps. The stubs return real
 * `InMemorySessionStore` / `InMemoryExampleStore` instances and real
 * callables, so the library code exercised under test is the actual one.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { buildProgram } from '../src/cli/index.js';
import { InMemoryExampleStore, type ExampleStore } from '../src/example.js';
import {
  InMemorySessionStore,
  type SessionStore,
  type SessionSnapshot,
} from '../src/workflows/session.js';
import type { Message } from '../src/workflows/types.js';
import type { Turn } from '../src/example-mining.js';

// ---------------------------------------------------------------------------
// console spies
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Fixture builders
// ---------------------------------------------------------------------------

function snap(id: string, messages: Message[]): SessionSnapshot {
  return {
    id,
    messages,
    state: {},
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  };
}

async function seedSessionStore(): Promise<SessionStore> {
  const store = new InMemorySessionStore();
  await store.save(
    's1',
    snap('s1', [
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi there' },
      { role: 'user', content: 'how are you?' },
      { role: 'assistant', content: 'doing well, thanks' },
    ]),
  );
  await store.save(
    's2',
    snap('s2', [
      { role: 'user', content: 'compute pi' },
      { role: 'assistant', content: '' },
      { role: 'tool', content: '3.14' },
      { role: 'assistant', content: 'pi is about 3.14' },
    ]),
  );
  await store.save(
    's3',
    snap('s3', [
      { role: 'user', content: 'noise' },
      { role: 'assistant', content: 'skip' },
    ]),
  );
  return store;
}

interface MineFixtureDeps {
  sessionStore?: SessionStore;
  exampleStore?: ExampleStore;
  config?: Record<string, unknown>;
  /** Extra callables to register by spec. */
  callables?: Record<string, (turn: Turn) => unknown>;
  exit?: (code: number) => void;
}

/**
 * Build a buildProgram() deps bag pre-wired for `examples mine`. The
 * stubbed `loadStore` / `loadCallable` look up specs in a literal map so
 * tests can pass `--session-store sess:store` etc. without touching disk.
 */
async function buildMineDeps(opts: MineFixtureDeps = {}) {
  const sessionStore = opts.sessionStore ?? (await seedSessionStore());
  const exampleStore = opts.exampleStore ?? new InMemoryExampleStore();
  const config = opts.config ?? {};
  const callables = opts.callables ?? {};

  const stores: Record<string, unknown> = {
    'sess:store': sessionStore,
    'ex:store': exampleStore,
  };

  const loadStore = vi.fn(async <T>(spec: string): Promise<T> => {
    if (!(spec in stores)) {
      throw new Error(`Failed to import store module "${spec}"`);
    }
    return stores[spec] as T;
  });

  const loadCallable = vi.fn(async (spec: string) => {
    if (!(spec in callables)) {
      throw new Error(
        `Failed to import callable module "${spec}": no such mock`,
      );
    }
    return callables[spec]!;
  });

  return {
    mine: {
      loadStore,
      loadCallable: loadCallable as <T extends (...args: never[]) => unknown>(
        spec: string,
      ) => Promise<T>,
      readConfig: () => config,
      ...(opts.exit ? { exit: opts.exit } : {}),
    },
    sessionStore,
    exampleStore,
    loadStore,
    loadCallable,
  };
}

async function runCli(
  args: string[],
  deps: Parameters<typeof buildProgram>[0] = {},
): Promise<void> {
  const program = buildProgram(deps);
  await program.parseAsync(['node', 'apx', ...args]);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('apx examples mine', () => {
  it('mines (user, assistant) examples from a SessionStore by default', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      sessions_scanned: number;
      examples_added: number;
      dry_run: boolean;
      examples: Array<{ agent_id: string }>;
    };
    expect(payload.sessions_scanned).toBe(3);
    // s1: 2 turns; s2: 1 real turn (empty-content assistant filtered);
    // s3: 1 turn ("skip" passes default filter — non-empty content).
    expect(payload.examples_added).toBeGreaterThanOrEqual(3);
    expect(payload.dry_run).toBe(false);
    expect(payload.examples.every((e) => e.agent_id === 'triage')).toBe(true);
  });

  it('dry-run does not write to the example store', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      examples_added: number;
      dry_run: boolean;
      examples: Array<{ id: string }>;
    };
    expect(payload.dry_run).toBe(true);
    expect(payload.examples_added).toBe(0);
    expect(payload.examples.length).toBeGreaterThanOrEqual(1);
    const stored = await fx.exampleStore.list({ agentId: 'triage' });
    expect(stored).toHaveLength(0);
  });

  it('resolves callables from --*-fn flags', async () => {
    const intentFn = vi.fn((turn: Turn) =>
      turn.userMessage.content.includes('hello') ? 'greet' : 'other',
    );
    const scoreFn = vi.fn(() => 0.9);
    const filterFn = vi.fn((turn: Turn) => turn.assistantMessage.content !== 'skip');
    const tagsFn = vi.fn(() => ['mined'] as readonly string[]);
    const metadataFn = vi.fn(() => ({ source: 'test' }));

    const fx = await buildMineDeps({
      callables: {
        'fns:intentFn': intentFn,
        'fns:scoreFn': scoreFn,
        'fns:filterFn': filterFn,
        'fns:tagsFn': tagsFn,
        'fns:metadataFn': metadataFn,
      },
    });
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--intent-fn',
        'fns:intentFn',
        '--score-fn',
        'fns:scoreFn',
        '--filter-fn',
        'fns:filterFn',
        '--tags-fn',
        'fns:tagsFn',
        '--metadata-fn',
        'fns:metadataFn',
        '--dry-run',
      ],
      { mine: fx.mine },
    );

    const payload = JSON.parse(stdout()) as {
      examples: Array<{
        intent: string;
        score?: number;
        tags: string[];
        metadata: Record<string, unknown>;
        output: string;
      }>;
    };
    expect(intentFn).toHaveBeenCalled();
    expect(filterFn).toHaveBeenCalled();
    const intents = new Set(payload.examples.map((e) => e.intent));
    expect(intents.has('greet')).toBe(true);
    expect(payload.examples.every((e) => e.score === 0.9)).toBe(true);
    expect(payload.examples.every((e) => e.tags.includes('mined'))).toBe(true);
    expect(
      payload.examples.every((e) => e.metadata.source === 'test'),
    ).toBe(true);
    expect(payload.examples.every((e) => e.output !== 'skip')).toBe(true);
  });

  it('resolves callables from pyproject when flags are omitted', async () => {
    const intentFn = vi.fn(() => 'from-config');
    const fx = await buildMineDeps({
      callables: { 'fns:intentFn': intentFn },
      config: {
        session_store: 'sess:store',
        example_store: 'ex:store',
        intent_fn: 'fns:intentFn',
      },
    });
    await runCli(['examples', 'mine', '--agent-id', 'triage', '--dry-run'], {
      mine: fx.mine,
    });
    const payload = JSON.parse(stdout()) as {
      examples: Array<{ intent: string }>;
    };
    expect(intentFn).toHaveBeenCalled();
    expect(payload.examples.every((e) => e.intent === 'from-config')).toBe(true);
  });

  it('filters by --session-ids when provided', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--session-ids',
        's1',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      sessions_scanned: number;
      examples: unknown[];
    };
    expect(payload.sessions_scanned).toBe(1);
    expect(payload.examples).toHaveLength(2);
  });

  it('emits the spec-defined text format', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--format',
        'text',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    const out = stdout();
    expect(out).toMatch(/^Mined \d+ examples from \d+ sessions \(\d+ turns considered\)$/);
  });

  it('respects --limit by capping examples_added', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--limit',
        '1',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      examples_added: number;
      examples: unknown[];
    };
    expect(payload.examples).toHaveLength(1);
    expect(payload.examples_added).toBe(1);
  });

  it('exits non-zero with a friendly error when --session-store is missing', async () => {
    const exit = vi.fn();
    const fx = await buildMineDeps({ exit });
    await runCli(
      [
        'examples',
        'mine',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    expect(exit).toHaveBeenCalledWith(1);
    expect(stderr()).toMatch(/--session-store|session_store/);
  });

  it('surfaces a friendly error when a callable spec is unimportable', async () => {
    const exit = vi.fn();
    const fx = await buildMineDeps({ exit });
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--intent-fn',
        'unknown:spec',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    expect(exit).toHaveBeenCalledWith(1);
    expect(stderr()).toMatch(/Failed to import|unknown:spec/);
  });

  it('drops turns under --min-score when --score-fn returns lower', async () => {
    const scoreFn = vi.fn(() => 0.1);
    const fx = await buildMineDeps({ callables: { 'fns:scoreFn': scoreFn } });
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--score-fn',
        'fns:scoreFn',
        '--min-score',
        '0.5',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      examples: unknown[];
      turns_considered: number;
    };
    expect(payload.examples).toHaveLength(0);
    expect(payload.turns_considered).toBeGreaterThan(0);
  });

  it('attaches sessionId / turnIndex metadata onto every emitted Example', async () => {
    const fx = await buildMineDeps();
    await runCli(
      [
        'examples',
        'mine',
        '--session-store',
        'sess:store',
        '--example-store',
        'ex:store',
        '--agent-id',
        'triage',
        '--session-ids',
        's1',
        '--dry-run',
      ],
      { mine: fx.mine },
    );
    const payload = JSON.parse(stdout()) as {
      examples: Array<{ metadata: { sessionId?: string; turnIndex?: number } }>;
    };
    expect(payload.examples.every((e) => e.metadata.sessionId === 's1')).toBe(
      true,
    );
    const indices = payload.examples
      .map((e) => e.metadata.turnIndex)
      .sort();
    expect(indices).toEqual([0, 1]);
  });

  it('exits non-zero when --agent-id is missing (commander validation)', async () => {
    const fx = await buildMineDeps();
    await expect(
      runCli(
        [
          'examples',
          'mine',
          '--session-store',
          'sess:store',
          '--example-store',
          'ex:store',
        ],
        { mine: fx.mine },
      ),
    ).rejects.toThrow();
  });
});

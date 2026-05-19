/**
 * Tests for `apx memory consolidate` — wraps {@link consolidateMemories} with
 * a file-injectable MemoryStore + caller-supplied summarizer callable.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { buildProgram } from '../src/cli/index.js';
import { InMemoryMemoryStore, type MemoryStore } from '../src/memory.js';
import type { SummarizeFn } from '../src/memory-consolidate.js';

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
// Fixture builder
// ---------------------------------------------------------------------------

async function seedMemoryStore(count: number): Promise<MemoryStore> {
  const store = new InMemoryMemoryStore();
  for (let i = 0; i < count; i++) {
    await store.add({
      principalId: 'alice',
      content: `memory-${i}`,
      namespace: 'profile',
    });
  }
  return store;
}

interface ConsolidateFixtureDeps {
  memoryStore?: MemoryStore;
  summarizeFn?: SummarizeFn;
  config?: Record<string, unknown>;
  /** Inject additional callable specs (e.g. for "no summarize-fn" paths). */
  extraCallables?: Record<string, (...args: never[]) => unknown>;
  exit?: (code: number) => void;
}

async function buildConsolidateDeps(opts: ConsolidateFixtureDeps = {}) {
  const store = opts.memoryStore ?? (await seedMemoryStore(6));
  const summarizeFn: SummarizeFn =
    opts.summarizeFn ?? ((mems) => `alice has ${mems.length} memories`);
  const config = opts.config ?? {};

  const stores: Record<string, unknown> = {
    'mem:store': store,
  };
  const callables: Record<string, (...args: never[]) => unknown> = {
    'fns:summarize': summarizeFn as (...args: never[]) => unknown,
    ...opts.extraCallables,
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
    consolidate: {
      loadStore,
      loadCallable: loadCallable as <T extends (...args: never[]) => unknown>(
        spec: string,
      ) => Promise<T>,
      readConfig: () => config,
      ...(opts.exit ? { exit: opts.exit } : {}),
    },
    store,
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

describe('apx memory consolidate', () => {
  it('writes a consolidated memory and deletes originals by default', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      candidates_found: number;
      consolidated_memory: { content: string; namespace: string } | null;
      deleted_ids: string[];
      dry_run: boolean;
    };
    expect(payload.candidates_found).toBe(6);
    expect(payload.consolidated_memory).not.toBeNull();
    expect(payload.consolidated_memory!.content).toBe('alice has 6 memories');
    expect(payload.consolidated_memory!.namespace).toBe('consolidated');
    expect(payload.deleted_ids).toHaveLength(6);
    expect(payload.dry_run).toBe(false);

    const remaining = await fx.store.list({ principalId: 'alice' });
    // Only the new consolidated row remains.
    expect(remaining).toHaveLength(1);
    expect(remaining[0]!.namespace).toBe('consolidated');
  });

  it('dry-run returns candidates + content without writing or deleting', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--dry-run',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      dry_run: boolean;
      consolidated_memory: { content: string } | null;
      deleted_ids: string[];
    };
    expect(payload.dry_run).toBe(true);
    expect(payload.consolidated_memory).not.toBeNull();
    expect(payload.consolidated_memory!.content).toBe('alice has 6 memories');
    expect(payload.deleted_ids).toEqual([]);

    const remaining = await fx.store.list({ principalId: 'alice' });
    expect(remaining).toHaveLength(6);
  });

  it('--keep-originals leaves the source rows in the store', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--keep-originals',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      deleted_ids: string[];
    };
    expect(payload.deleted_ids).toEqual([]);

    const remaining = await fx.store.list({ principalId: 'alice' });
    // 6 originals + 1 consolidated row.
    expect(remaining).toHaveLength(7);
  });

  it('exits non-zero when fewer than --min-memories-for-consolidation match', async () => {
    const store = await seedMemoryStore(3);
    const exit = vi.fn();
    const fx = await buildConsolidateDeps({ memoryStore: store, exit });
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
      ],
      { consolidate: fx.consolidate },
    );
    expect(exit).toHaveBeenCalledWith(1);
    const err = stderr();
    expect(err).toMatch(/below|3/);
  });

  it('emits the spec-defined text format on success', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--format',
        'text',
      ],
      { consolidate: fx.consolidate },
    );
    const out = stdout();
    expect(out).toMatch(/^Consolidated 6 memories → mem_/);
  });

  it('falls back to [tool.apx.agent] keys when --store and --summarize-fn are omitted', async () => {
    const fx = await buildConsolidateDeps({
      config: {
        memory_store: 'mem:store',
        summarize_fn: 'fns:summarize',
      },
    });
    await runCli(
      [
        'memory',
        'consolidate',
        '--principal-id',
        'alice',
        '--min-memories-for-consolidation',
        '5',
        '--dry-run',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      consolidated_memory: { content: string } | null;
    };
    expect(payload.consolidated_memory!.content).toBe('alice has 6 memories');
  });

  it('errors when --summarize-fn is missing entirely', async () => {
    const exit = vi.fn();
    const fx = await buildConsolidateDeps({ exit });
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
      ],
      { consolidate: fx.consolidate },
    );
    expect(exit).toHaveBeenCalledWith(1);
    expect(stderr()).toMatch(/--summarize-fn|summarize_fn/);
  });

  it('errors when --store cannot be resolved', async () => {
    const exit = vi.fn();
    const fx = await buildConsolidateDeps({ exit });
    await runCli(
      [
        'memory',
        'consolidate',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
      ],
      { consolidate: fx.consolidate },
    );
    expect(exit).toHaveBeenCalledWith(1);
    expect(stderr()).toMatch(/--store|memory_store/);
  });

  it('passes --consolidated-tags CSV through to the consolidated row', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--consolidated-tags',
        'auto,roll-up',
        '--keep-originals',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      consolidated_memory: { tags: string[] } | null;
    };
    expect(payload.consolidated_memory!.tags).toEqual(
      expect.arrayContaining(['auto', 'roll-up']),
    );
  });

  it('honors --consolidated-namespace + --consolidated-importance', async () => {
    const fx = await buildConsolidateDeps();
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--consolidated-namespace',
        'roll-up',
        '--consolidated-importance',
        '0.95',
        '--keep-originals',
      ],
      { consolidate: fx.consolidate },
    );
    const payload = JSON.parse(stdout()) as {
      consolidated_memory: { namespace: string; importance: number } | null;
    };
    expect(payload.consolidated_memory!.namespace).toBe('roll-up');
    expect(payload.consolidated_memory!.importance).toBeCloseTo(0.95);
  });

  it('exits non-zero when --principal-id is missing (commander validation)', async () => {
    const fx = await buildConsolidateDeps();
    await expect(
      runCli(
        [
          'memory',
          'consolidate',
          '--store',
          'mem:store',
          '--summarize-fn',
          'fns:summarize',
        ],
        { consolidate: fx.consolidate },
      ),
    ).rejects.toThrow();
  });

  it('calls summarizeFn with the candidate memories', async () => {
    const summarize = vi.fn((mems: readonly { id: string }[]) =>
      `summarized ${mems.length} memories`,
    );
    const fx = await buildConsolidateDeps({
      summarizeFn: summarize as SummarizeFn,
    });
    await runCli(
      [
        'memory',
        'consolidate',
        '--store',
        'mem:store',
        '--principal-id',
        'alice',
        '--summarize-fn',
        'fns:summarize',
        '--min-memories-for-consolidation',
        '5',
        '--dry-run',
      ],
      { consolidate: fx.consolidate },
    );
    expect(summarize).toHaveBeenCalledOnce();
    const arg = summarize.mock.calls[0]![0] as { id: string }[];
    expect(arg).toHaveLength(6);
    expect(stdout()).toContain('summarized 6 memories');
  });
});

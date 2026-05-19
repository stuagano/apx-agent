/**
 * Tests for `apx memory` — recall / remember / forget / list.
 *
 * Strategy: build the CLI with `deps.memory.loadStore` injected so the
 * file-system import is bypassed entirely. The injected loader returns
 * a seeded `InMemoryMemoryStore` (or throws to simulate import failure).
 * Console spies capture stdout/stderr for assertions.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { buildProgram } from '../src/cli/index.js';
import { parseTags } from '../src/cli/load-store.js';
import { InMemoryMemoryStore, type MemoryStore } from '../src/memory.js';

// ---------------------------------------------------------------------------
// Console spy plumbing
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

async function seededStore(): Promise<MemoryStore> {
  const store = new InMemoryMemoryStore();
  await store.add({
    principalId: 'user-1',
    content: 'likes oat milk',
    namespace: 'profile',
    tags: ['preference', 'beverage'],
    importance: 0.7,
  });
  await store.add({
    principalId: 'user-1',
    content: 'allergic to peanuts',
    namespace: 'profile',
    tags: ['allergy', 'health'],
    importance: 0.9,
  });
  await store.add({
    principalId: 'user-2',
    content: 'prefers dark mode',
    tags: ['preference'],
    importance: 0.5,
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
// parseTags helper — comma-separated to string[]
// ---------------------------------------------------------------------------

describe('parseTags', () => {
  it('returns undefined for empty / whitespace input', () => {
    expect(parseTags(undefined)).toBeUndefined();
    expect(parseTags('')).toBeUndefined();
    expect(parseTags('   ')).toBeUndefined();
    expect(parseTags(',,, ,')).toBeUndefined();
  });

  it('splits CSV and trims whitespace', () => {
    expect(parseTags('a,b,c')).toEqual(['a', 'b', 'c']);
    expect(parseTags(' a , b ,c ')).toEqual(['a', 'b', 'c']);
  });

  it('drops empty segments rather than including them', () => {
    expect(parseTags('a,,b')).toEqual(['a', 'b']);
  });
});

// ---------------------------------------------------------------------------
// recall
// ---------------------------------------------------------------------------

describe('apx memory recall', () => {
  it('emits valid JSON by default', async () => {
    const store = await seededStore();
    await runCli(
      [
        'memory',
        'recall',
        '--principal-id',
        'user-1',
        '--query',
        'milk',
        '-k',
        '5',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed.length).toBeGreaterThan(0);
    expect(parsed[0]).toHaveProperty('score');
    expect(parsed[0].memory).toHaveProperty('principal_id', 'user-1');
    // embedding should never leak into the dto
    expect(parsed[0].memory.embedding).toBeUndefined();
  });

  it('renders markdown when --format text', async () => {
    const store = await seededStore();
    await runCli(
      [
        'memory',
        'recall',
        '--principal-id',
        'user-1',
        '--query',
        'milk',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    const out = stdout();
    expect(out).toMatch(/# memory recall for principal=/);
    expect(out).toMatch(/- \[score=\d+\.\d{3}\] .+/);
  });

  it('exits 1 when the store fails to load', async () => {
    const exitSpy = vi.fn();
    await runCli(
      [
        'memory',
        'recall',
        '--principal-id',
        'user-1',
        '--query',
        'milk',
        '--store-module',
        'bogus:store',
      ],
      {
        memory: {
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

  it('surfaces a usage error when --principal-id is missing', async () => {
    await expect(
      runCli(['memory', 'recall', '--query', 'milk'], {
        memory: { loadStore: async () => new InMemoryMemoryStore() },
      }),
    ).rejects.toThrowError();
  });
});

// ---------------------------------------------------------------------------
// remember
// ---------------------------------------------------------------------------

describe('apx memory remember', () => {
  it('inserts a memory and returns its DTO as JSON', async () => {
    const store = new InMemoryMemoryStore();
    await runCli(
      [
        'memory',
        'remember',
        '--principal-id',
        'user-1',
        '--content',
        'enjoys pour-over coffee',
        '--namespace',
        'profile',
        '--tags',
        'preference,beverage',
        '--importance',
        '0.8',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed.principal_id).toBe('user-1');
    expect(parsed.content).toBe('enjoys pour-over coffee');
    expect(parsed.namespace).toBe('profile');
    expect(parsed.tags).toEqual(['preference', 'beverage']);
    expect(parsed.importance).toBeCloseTo(0.8);
    expect(typeof parsed.id).toBe('string');
  });

  it('renders "Saved memory {id}" in text mode', async () => {
    const store = new InMemoryMemoryStore();
    await runCli(
      [
        'memory',
        'remember',
        '--principal-id',
        'user-1',
        '--content',
        'hello',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    expect(stdout()).toMatch(/^Saved memory mem_/);
  });
});

// ---------------------------------------------------------------------------
// forget
// ---------------------------------------------------------------------------

describe('apx memory forget', () => {
  it('deletes an existing memory and emits the deleted id as JSON', async () => {
    const store = await seededStore();
    // grab an id
    const list = await store.list({ principalId: 'user-1' });
    const id = list[0]!.id;

    await runCli(
      ['memory', 'forget', '--id', id, '--store-module', 'fake:store'],
      { memory: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed).toEqual({ deleted: id });
    expect(await store.get(id)).toBeNull();
  });

  it('exits 1 with a "No memory" message when id is unknown', async () => {
    const exitSpy = vi.fn();
    const store = await seededStore();
    await runCli(
      [
        'memory',
        'forget',
        '--id',
        'mem_does-not-exist',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store, exit: exitSpy } },
    );
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stderr()).toContain('No memory with id mem_does-not-exist');
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('apx memory list', () => {
  it('lists memories scoped to a principal', async () => {
    const store = await seededStore();
    await runCli(
      ['memory', 'list', '--principal-id', 'user-1', '--store-module', 'fake:store'],
      { memory: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(2);
    for (const row of parsed) {
      expect(row.principal_id).toBe('user-1');
    }
  });

  it('filters by tags via the CSV --tags flag', async () => {
    const store = await seededStore();
    await runCli(
      [
        'memory',
        'list',
        '--principal-id',
        'user-1',
        '--tags',
        'allergy',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(1);
    expect(parsed[0].content).toBe('allergic to peanuts');
  });

  it('renders markdown bullets in text mode', async () => {
    const store = await seededStore();
    await runCli(
      [
        'memory',
        'list',
        '--principal-id',
        'user-1',
        '--format',
        'text',
        '--store-module',
        'fake:store',
      ],
      { memory: { loadStore: async () => store } },
    );
    const out = stdout();
    expect(out).toMatch(/# memory list for principal=/);
    expect(out).toMatch(/- likes oat milk|- allergic to peanuts/);
  });
});

// ---------------------------------------------------------------------------
// pyproject fallback — memory_store key
// ---------------------------------------------------------------------------

describe('memory_store pyproject fallback', () => {
  it('reads [tool.apx.agent].memory_store when --store-module is omitted', async () => {
    const store = await seededStore();
    const readConfig = vi.fn(() => ({ memory_store: 'configured:store' }));
    const loadStore = vi.fn(async (spec: string) => {
      expect(spec).toBe('configured:store');
      return store;
    });
    await runCli(['memory', 'list', '--principal-id', 'user-1'], {
      memory: { loadStore, readConfig },
    });
    expect(readConfig).toHaveBeenCalled();
    expect(loadStore).toHaveBeenCalledWith('configured:store');
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(2);
  });

  it('exits 1 with a friendly error when no store is configured anywhere', async () => {
    const exitSpy = vi.fn();
    await runCli(['memory', 'list', '--principal-id', 'user-1'], {
      memory: {
        readConfig: () => ({}),
        loadStore: async () => {
          throw new Error('should not be called');
        },
        exit: exitSpy,
      },
    });
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stderr()).toMatch(/Pass --store-module/);
    expect(stderr()).toContain('memory_store');
  });
});

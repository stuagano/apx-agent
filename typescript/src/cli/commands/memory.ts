/**
 * apx memory — CRUD + semantic recall against a {@link MemoryStore}.
 *
 * Four subcommands:
 *
 *   - `recall`   — top-k similarity recall (vector if store has an embedder,
 *                  recency fallback otherwise).
 *   - `remember` — insert a memory.
 *   - `forget`   — delete a memory by id. Exits 1 if the id wasn't present.
 *   - `list`     — paginated tag-filtered listing.
 *
 * Mirrors `python/src/apx_agent/cli.py::memory`. The store is loaded via
 * {@link resolveStore} which honors `--store-module` → pyproject fallback.
 *
 * # Output
 *
 * JSON (default) for all subcommands. `--format text` renders markdown:
 *   - `recall`: `- [score=0.873] {content}` bullets.
 *   - `list`:   `- {content}` bullets.
 *   - `remember`: `Saved memory {id}` one-liner.
 *   - `forget` hit: `Forgot memory {id}` / miss: `No memory with id {id}`.
 *
 * # Error model
 *
 * Store load failures (unimportable module, missing export, no store
 * configured) write a friendly message to stderr and call `deps.exit(1)`.
 * Missing required flags throw `CommanderError` synchronously, exactly
 * like the rest of the CLI — the command's body never runs in that case.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import type {
  Memory,
  MemoryFilter,
  MemoryStore,
  RecallOptions,
  RecallResult,
} from '../../memory.js';
import { readApxAgentConfig } from '../pyproject.js';
import {
  loadStore as defaultLoadStore,
  parseTags,
  resolveStore,
} from '../load-store.js';

// ---------------------------------------------------------------------------
// Serialization — drop the embedding when emitting JSON; it's an
// implementation detail and bloats the payload.
// ---------------------------------------------------------------------------

interface MemoryDto {
  id: string;
  principal_id: string;
  namespace: string;
  content: string;
  tags: readonly string[];
  importance: number;
  metadata: Readonly<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
}

function memoryToDto(m: Memory): MemoryDto {
  return {
    id: m.id,
    principal_id: m.principalId,
    namespace: m.namespace,
    content: m.content,
    tags: [...m.tags],
    importance: m.importance,
    metadata: { ...m.metadata },
    created_at: m.createdAt,
    updated_at: m.updatedAt,
  };
}

// ---------------------------------------------------------------------------
// Shared option schemas
// ---------------------------------------------------------------------------

const FormatSchema = z.enum(['json', 'text']).default('json');

const RecallSchema = z.object({
  principalId: z.string().min(1),
  query: z.string().min(1),
  namespace: z.string().optional(),
  tags: z.string().optional(),
  k: z.coerce.number().int().positive().default(5),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const RememberSchema = z.object({
  principalId: z.string().min(1),
  content: z.string().min(1),
  namespace: z.string().optional(),
  tags: z.string().optional(),
  importance: z.coerce.number().min(0).max(1).default(0.5),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const ForgetSchema = z.object({
  id: z.string().min(1),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const ListSchema = z.object({
  principalId: z.string().min(1),
  namespace: z.string().optional(),
  tags: z.string().optional(),
  limit: z.coerce.number().int().positive().default(100),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

// ---------------------------------------------------------------------------
// Deps — tests inject these to stub IO
// ---------------------------------------------------------------------------

export interface MemoryCommandDeps {
  /** Override the store loader. Defaults to {@link defaultLoadStore}. */
  loadStore?: <T>(spec: string) => Promise<T>;
  /** Override config reader. Defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
  /** Exit hook — tests pass a recorder; production passes `process.exit`. */
  exit?: (code: number) => void;
}

interface ResolvedDeps {
  loadStore: <T>(spec: string) => Promise<T>;
  readConfig: () => Record<string, unknown>;
  exit: (code: number) => void;
}

function resolveDeps(deps: MemoryCommandDeps): ResolvedDeps {
  return {
    loadStore: deps.loadStore ?? (defaultLoadStore as <T>(spec: string) => Promise<T>),
    readConfig: deps.readConfig ?? readApxAgentConfig,
    exit: deps.exit ?? ((c: number) => process.exit(c)),
  };
}

async function loadMemoryStore(
  storeModule: string | undefined,
  deps: ResolvedDeps,
): Promise<MemoryStore | null> {
  try {
    return await resolveStore<MemoryStore>({ storeModule }, 'memory', {
      loadStore: deps.loadStore,
      readConfig: deps.readConfig,
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(err instanceof Error ? err.message : String(err));
    deps.exit(1);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Renderers — text fallback for each subcommand.
// ---------------------------------------------------------------------------

function renderRecallText(results: RecallResult[], principalId: string): string {
  if (results.length === 0) {
    return `# memory recall for principal=${JSON.stringify(principalId)}: no hits.`;
  }
  const lines: string[] = [
    `# memory recall for principal=${JSON.stringify(principalId)} (top ${results.length})`,
  ];
  for (const r of results) {
    lines.push(`- [score=${r.score.toFixed(3)}] ${r.memory.content}`);
  }
  return lines.join('\n');
}

function renderListText(rows: Memory[], principalId: string): string {
  if (rows.length === 0) {
    return `# memory list for principal=${JSON.stringify(principalId)}: empty.`;
  }
  const lines: string[] = [
    `# memory list for principal=${JSON.stringify(principalId)} (${rows.length} rows)`,
  ];
  for (const m of rows) {
    lines.push(`- ${m.content}`);
  }
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Registrar
// ---------------------------------------------------------------------------

export function registerMemoryCommand(
  program: Command,
  deps: MemoryCommandDeps = {},
): void {
  const resolved = resolveDeps(deps);

  const group = program
    .command('memory')
    .description(
      'Operate on a MemoryStore — recall, remember, forget, list. ' +
        'Configure via --store-module MODULE:VAR or [tool.apx.agent].memory_store.',
    );

  // --- recall -------------------------------------------------------------
  group
    .command('recall')
    .description('Recall top-k memories matching --query for --principal-id.')
    .requiredOption('--principal-id <id>', 'Scopes recall to one principal.')
    .requiredOption('--query <text>', 'Natural-language query.')
    .option('--namespace <ns>', 'Optional namespace filter.')
    .option('--tags <csv>', 'Comma-separated tag list (any-of filter).')
    .option('-k, --k <n>', 'Top-k. Default 5.', '5')
    .option(
      '--store-module <spec>',
      'MODULE:VAR pointing at a MemoryStore instance. ' +
        'Falls back to [tool.apx.agent].memory_store.',
    )
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = RecallSchema.parse(rawOpts);
      const store = await loadMemoryStore(opts.storeModule, resolved);
      if (!store) return;

      const recallOpts: RecallOptions = {
        principalId: opts.principalId,
        query: opts.query,
        ...(opts.namespace !== undefined ? { namespace: opts.namespace } : {}),
        ...(parseTags(opts.tags) !== undefined ? { tags: parseTags(opts.tags)! } : {}),
        k: opts.k,
      };
      const results = await store.recall(recallOpts);

      if (opts.format === 'json') {
        const payload = results.map((r) => ({
          score: r.score,
          memory: memoryToDto(r.memory),
        }));
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(payload, null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(renderRecallText(results, opts.principalId));
      }
    });

  // --- remember -----------------------------------------------------------
  group
    .command('remember')
    .description('Insert a memory into the store.')
    .requiredOption('--principal-id <id>', 'Scopes the memory to a principal.')
    .requiredOption('--content <text>', 'The memory text to remember.')
    .option('--namespace <ns>', "Optional namespace (defaults to 'default').")
    .option('--tags <csv>', 'Comma-separated tag list.')
    .option('--importance <n>', 'Importance 0..1. Default 0.5.', '0.5')
    .option('--store-module <spec>', 'MODULE:VAR pointing at a MemoryStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = RememberSchema.parse(rawOpts);
      const store = await loadMemoryStore(opts.storeModule, resolved);
      if (!store) return;

      const tags = parseTags(opts.tags);
      const memory = await store.add({
        principalId: opts.principalId,
        content: opts.content,
        importance: opts.importance,
        ...(opts.namespace !== undefined ? { namespace: opts.namespace } : {}),
        ...(tags !== undefined ? { tags } : {}),
      });

      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(memoryToDto(memory), null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(`Saved memory ${memory.id}`);
      }
    });

  // --- forget -------------------------------------------------------------
  group
    .command('forget')
    .description('Delete a memory by id. Exits non-zero if no row matched.')
    .requiredOption('--id <id>', 'Memory id to delete.')
    .option('--store-module <spec>', 'MODULE:VAR pointing at a MemoryStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = ForgetSchema.parse(rawOpts);
      const store = await loadMemoryStore(opts.storeModule, resolved);
      if (!store) return;

      const ok = await store.delete(opts.id);
      if (!ok) {
        if (opts.format === 'json') {
          // eslint-disable-next-line no-console
          console.error(JSON.stringify({ error: `No memory with id ${opts.id}` }));
        } else {
          // eslint-disable-next-line no-console
          console.error(`No memory with id ${opts.id}`);
        }
        resolved.exit(1);
        return;
      }
      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify({ deleted: opts.id }));
      } else {
        // eslint-disable-next-line no-console
        console.log(`Forgot memory ${opts.id}`);
      }
    });

  // --- list ---------------------------------------------------------------
  group
    .command('list')
    .description('List memories for --principal-id.')
    .requiredOption('--principal-id <id>', 'Scopes the listing to one principal.')
    .option('--namespace <ns>', 'Optional namespace filter.')
    .option('--tags <csv>', 'Comma-separated tag list.')
    .option('--limit <n>', 'Max rows. Default 100.', '100')
    .option('--store-module <spec>', 'MODULE:VAR pointing at a MemoryStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = ListSchema.parse(rawOpts);
      const store = await loadMemoryStore(opts.storeModule, resolved);
      if (!store) return;

      const filter: MemoryFilter = {
        principalId: opts.principalId,
        ...(opts.namespace !== undefined ? { namespace: opts.namespace } : {}),
        ...(parseTags(opts.tags) !== undefined ? { tags: parseTags(opts.tags)! } : {}),
        limit: opts.limit,
      };
      const rows = await store.list(filter);

      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(rows.map(memoryToDto), null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(renderListText(rows, opts.principalId));
      }
    });
}

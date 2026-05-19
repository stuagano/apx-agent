/**
 * apx examples — CRUD + semantic recall against an {@link ExampleStore}.
 *
 * Four subcommands:
 *
 *   - `find`   — top-k similarity recall by input text.
 *   - `save`   — insert an example.
 *   - `remove` — delete an example by id. Exits 1 if missing.
 *   - `list`   — paginated tag-filtered listing.
 *
 * Mirrors `python/src/apx_agent/cli.py::examples`. Store loaded via
 * {@link resolveStore} (`--store-module` → pyproject fallback).
 *
 * # Text format
 *
 * `find` / `list` render markdown `**Q:** ... \n **A:** ... \n ---` blocks
 * so the input/output pair stays scannable at the terminal. `find` prepends
 * a `score=` header; `list` omits it.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import type {
  Example,
  ExampleFilter,
  ExampleResult,
  ExampleStore,
  FindSimilarOptions,
} from '../../example.js';
import { readApxAgentConfig } from '../pyproject.js';
import {
  loadStore as defaultLoadStore,
  parseTags,
  resolveStore,
} from '../load-store.js';

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

interface ExampleDto {
  id: string;
  agent_id: string;
  intent: string;
  input: string;
  output: string;
  score?: number;
  tags: readonly string[];
  metadata: Readonly<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
}

function exampleToDto(e: Example): ExampleDto {
  return {
    id: e.id,
    agent_id: e.agentId,
    intent: e.intent,
    input: e.input,
    output: e.output,
    score: e.score,
    tags: [...e.tags],
    metadata: { ...e.metadata },
    created_at: e.createdAt,
    updated_at: e.updatedAt,
  };
}

// ---------------------------------------------------------------------------
// Option schemas
// ---------------------------------------------------------------------------

const FormatSchema = z.enum(['json', 'text']).default('json');

const FindSchema = z.object({
  agentId: z.string().min(1),
  query: z.string().min(1),
  intent: z.string().optional(),
  tags: z.string().optional(),
  k: z.coerce.number().int().positive().default(5),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const SaveSchema = z.object({
  agentId: z.string().min(1),
  input: z.string().min(1),
  output: z.string().min(1),
  intent: z.string().optional(),
  score: z.coerce.number().min(0).max(1).optional(),
  tags: z.string().optional(),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const RemoveSchema = z.object({
  id: z.string().min(1),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

const ListSchema = z.object({
  agentId: z.string().min(1),
  intent: z.string().optional(),
  tags: z.string().optional(),
  limit: z.coerce.number().int().positive().default(100),
  storeModule: z.string().optional(),
  format: FormatSchema,
});

// ---------------------------------------------------------------------------
// Deps
// ---------------------------------------------------------------------------

export interface ExamplesCommandDeps {
  loadStore?: <T>(spec: string) => Promise<T>;
  readConfig?: () => Record<string, unknown>;
  exit?: (code: number) => void;
}

interface ResolvedDeps {
  loadStore: <T>(spec: string) => Promise<T>;
  readConfig: () => Record<string, unknown>;
  exit: (code: number) => void;
}

function resolveDeps(deps: ExamplesCommandDeps): ResolvedDeps {
  return {
    loadStore: deps.loadStore ?? (defaultLoadStore as <T>(spec: string) => Promise<T>),
    readConfig: deps.readConfig ?? readApxAgentConfig,
    exit: deps.exit ?? ((c: number) => process.exit(c)),
  };
}

async function loadExampleStore(
  storeModule: string | undefined,
  deps: ResolvedDeps,
): Promise<ExampleStore | null> {
  try {
    return await resolveStore<ExampleStore>({ storeModule }, 'example', {
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
// Renderers
// ---------------------------------------------------------------------------

function renderFindText(results: ExampleResult[], agentId: string): string {
  if (results.length === 0) {
    return `# examples find for agent=${JSON.stringify(agentId)}: no hits.`;
  }
  const lines: string[] = [
    `# examples find for agent=${JSON.stringify(agentId)} (top ${results.length})`,
  ];
  for (const r of results) {
    lines.push(`score=${r.score.toFixed(3)}`);
    lines.push(`**Q:** ${r.example.input}`);
    lines.push(`**A:** ${r.example.output}`);
    lines.push('---');
  }
  return lines.join('\n');
}

function renderListText(rows: Example[], agentId: string): string {
  if (rows.length === 0) {
    return `# examples list for agent=${JSON.stringify(agentId)}: empty.`;
  }
  const lines: string[] = [
    `# examples list for agent=${JSON.stringify(agentId)} (${rows.length} rows)`,
  ];
  for (const ex of rows) {
    lines.push(`**Q:** ${ex.input}`);
    lines.push(`**A:** ${ex.output}`);
    lines.push('---');
  }
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Registrar
// ---------------------------------------------------------------------------

export function registerExamplesCommand(
  program: Command,
  deps: ExamplesCommandDeps = {},
): void {
  const resolved = resolveDeps(deps);

  const group = program
    .command('examples')
    .description(
      'Operate on an ExampleStore — find, save, remove, list. ' +
        'Configure via --store-module MODULE:VAR or [tool.apx.agent].example_store.',
    );

  // --- find ---------------------------------------------------------------
  group
    .command('find')
    .description('Find top-k similar examples for --query.')
    .requiredOption('--agent-id <id>', 'Scopes the search to one agent.')
    .requiredOption('--query <text>', 'Natural-language query.')
    .option('--intent <name>', 'Optional intent filter.')
    .option('--tags <csv>', 'Comma-separated tag list.')
    .option('-k, --k <n>', 'Top-k. Default 5.', '5')
    .option(
      '--store-module <spec>',
      'MODULE:VAR pointing at an ExampleStore instance. ' +
        'Falls back to [tool.apx.agent].example_store.',
    )
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = FindSchema.parse(rawOpts);
      const store = await loadExampleStore(opts.storeModule, resolved);
      if (!store) return;

      const findOpts: FindSimilarOptions = {
        agentId: opts.agentId,
        query: opts.query,
        ...(opts.intent !== undefined ? { intent: opts.intent } : {}),
        ...(parseTags(opts.tags) !== undefined ? { tags: parseTags(opts.tags)! } : {}),
        k: opts.k,
      };
      const results = await store.findSimilar(findOpts);

      if (opts.format === 'json') {
        const payload = results.map((r) => ({
          score: r.score,
          example: exampleToDto(r.example),
        }));
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(payload, null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(renderFindText(results, opts.agentId));
      }
    });

  // --- save ---------------------------------------------------------------
  group
    .command('save')
    .description('Insert an example into the store.')
    .requiredOption('--agent-id <id>', 'Scopes the example to an agent.')
    .requiredOption('--input <text>', 'The example input.')
    .requiredOption('--output <text>', 'The example output.')
    .option('--intent <name>', 'Optional intent bucket.')
    .option('--score <n>', 'Optional 0..1 quality score.')
    .option('--tags <csv>', 'Comma-separated tag list.')
    .option('--store-module <spec>', 'MODULE:VAR pointing at an ExampleStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = SaveSchema.parse(rawOpts);
      const store = await loadExampleStore(opts.storeModule, resolved);
      if (!store) return;

      const tags = parseTags(opts.tags);
      const example = await store.add({
        agentId: opts.agentId,
        input: opts.input,
        output: opts.output,
        ...(opts.intent !== undefined ? { intent: opts.intent } : {}),
        ...(opts.score !== undefined ? { score: opts.score } : {}),
        ...(tags !== undefined ? { tags } : {}),
      });

      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(exampleToDto(example), null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(`Saved example ${example.id}`);
      }
    });

  // --- remove -------------------------------------------------------------
  group
    .command('remove')
    .description('Delete an example by id. Exits non-zero if no row matched.')
    .requiredOption('--id <id>', 'Example id to delete.')
    .option('--store-module <spec>', 'MODULE:VAR pointing at an ExampleStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = RemoveSchema.parse(rawOpts);
      const store = await loadExampleStore(opts.storeModule, resolved);
      if (!store) return;

      const ok = await store.delete(opts.id);
      if (!ok) {
        if (opts.format === 'json') {
          // eslint-disable-next-line no-console
          console.error(JSON.stringify({ error: `No example with id ${opts.id}` }));
        } else {
          // eslint-disable-next-line no-console
          console.error(`No example with id ${opts.id}`);
        }
        resolved.exit(1);
        return;
      }
      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify({ deleted: opts.id }));
      } else {
        // eslint-disable-next-line no-console
        console.log(`Removed example ${opts.id}`);
      }
    });

  // --- list ---------------------------------------------------------------
  group
    .command('list')
    .description('List examples for --agent-id.')
    .requiredOption('--agent-id <id>', 'Scopes the listing to one agent.')
    .option('--intent <name>', 'Optional intent filter.')
    .option('--tags <csv>', 'Comma-separated tag list.')
    .option('--limit <n>', 'Max rows. Default 100.', '100')
    .option('--store-module <spec>', 'MODULE:VAR pointing at an ExampleStore instance.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = ListSchema.parse(rawOpts);
      const store = await loadExampleStore(opts.storeModule, resolved);
      if (!store) return;

      const filter: ExampleFilter = {
        agentId: opts.agentId,
        ...(opts.intent !== undefined ? { intent: opts.intent } : {}),
        ...(parseTags(opts.tags) !== undefined ? { tags: parseTags(opts.tags)! } : {}),
        limit: opts.limit,
      };
      const rows = await store.list(filter);

      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(rows.map(exampleToDto), null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(renderListText(rows, opts.agentId));
      }
    });
}

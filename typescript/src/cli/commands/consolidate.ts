/**
 * apx memory consolidate — wrap {@link consolidateMemories} with a
 * file-injectable MemoryStore + caller-supplied summarizer callable.
 *
 * Mirrors `python/src/apx_agent/cli.py::memory_consolidate_cmd`. Pyproject
 * fallback keys (under `[tool.apx.agent]`):
 *
 *   * `--store`         → `memory_store`
 *   * `--summarize-fn`  → `summarize_fn` (REQUIRED)
 *
 * # Group composition
 *
 * The `memory` group is created by {@link registerMemoryCommand}.
 * To stay composition-safe regardless of registration order, this
 * registrar looks for an existing `memory` command and attaches to it,
 * or creates one if missing.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import {
  consolidateMemories,
  type SummarizeFn,
} from '../../memory-consolidate.js';
import type { Memory, MemoryStore } from '../../memory.js';
import { readApxAgentConfig } from '../pyproject.js';
import {
  loadStore as defaultLoadStore,
  parseTags,
} from '../load-store.js';
import {
  loadCallable as defaultLoadCallable,
  resolveCallable,
  type AnyCallable,
} from '../load-callable.js';

// ---------------------------------------------------------------------------
// Option schema
// ---------------------------------------------------------------------------

const FormatSchema = z.enum(['json', 'text']).default('json');

const ConsolidateSchema = z.object({
  store: z.string().optional(),
  principalId: z.string().min(1),
  summarizeFn: z.string().optional(),
  namespace: z.string().optional(),
  maxAgeSeconds: z.coerce.number().optional(),
  minImportance: z.coerce.number().optional(),
  keepOriginals: z.boolean().default(false),
  consolidatedNamespace: z.string().default('consolidated'),
  consolidatedTags: z.string().optional(),
  consolidatedImportance: z.coerce.number().default(0.7),
  minMemoriesForConsolidation: z.coerce.number().int().positive().default(5),
  dryRun: z.boolean().default(false),
  format: FormatSchema,
});

// ---------------------------------------------------------------------------
// Deps
// ---------------------------------------------------------------------------

export interface ConsolidateCommandDeps {
  loadStore?: <T>(spec: string) => Promise<T>;
  loadCallable?: <T extends AnyCallable>(spec: string) => Promise<T>;
  readConfig?: () => Record<string, unknown>;
  exit?: (code: number) => void;
}

interface ResolvedDeps {
  loadStore: <T>(spec: string) => Promise<T>;
  loadCallable: <T extends AnyCallable>(spec: string) => Promise<T>;
  readConfig: () => Record<string, unknown>;
  exit: (code: number) => void;
}

function resolveDeps(deps: ConsolidateCommandDeps): ResolvedDeps {
  return {
    loadStore:
      deps.loadStore ?? (defaultLoadStore as <T>(spec: string) => Promise<T>),
    loadCallable:
      deps.loadCallable ??
      (defaultLoadCallable as <T extends AnyCallable>(spec: string) => Promise<T>),
    readConfig: deps.readConfig ?? readApxAgentConfig,
    exit: deps.exit ?? ((c: number) => process.exit(c)),
  };
}

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

function memoryToDto(m: Memory): Record<string, unknown> {
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
// Registrar
// ---------------------------------------------------------------------------

function ensureMemoryGroup(program: Command): Command {
  const existing = program.commands.find((c) => c.name() === 'memory');
  if (existing) return existing;
  return program
    .command('memory')
    .description(
      'Operate on a MemoryStore — recall, remember, forget, list, consolidate. ' +
        'Configure via --store-module MODULE:VAR or [tool.apx.agent].memory_store.',
    );
}

export function registerConsolidateCommand(
  program: Command,
  deps: ConsolidateCommandDeps = {},
): void {
  const resolved = resolveDeps(deps);
  const group = ensureMemoryGroup(program);

  group
    .command('consolidate')
    .description('Summarize older memories into a single consolidated row.')
    .option(
      '--store <spec>',
      'MODULE:VAR pointing at a MemoryStore instance. ' +
        'Falls back to [tool.apx.agent].memory_store.',
    )
    .requiredOption(
      '--principal-id <id>',
      'Scope consolidation to a single principal.',
    )
    .option(
      '--summarize-fn <spec>',
      'MODULE:VAR of a (memories) => string summarizer. ' +
        'Falls back to [tool.apx.agent].summarize_fn.',
    )
    .option('--namespace <ns>', 'Optional namespace filter for candidates.')
    .option(
      '--max-age-seconds <n>',
      'Only consolidate memories older than this many seconds.',
    )
    .option('--min-importance <n>', 'Importance floor for candidate selection.')
    .option(
      '--keep-originals',
      'Skip deletion of the source memories after write.',
    )
    .option(
      '--consolidated-namespace <ns>',
      "Namespace for the consolidated row. Default: 'consolidated'.",
      'consolidated',
    )
    .option(
      '--consolidated-tags <csv>',
      "Comma-separated tag list. Default: 'consolidated'.",
    )
    .option(
      '--consolidated-importance <n>',
      'Importance for the consolidated row. Default 0.7.',
      '0.7',
    )
    .option(
      '--min-memories-for-consolidation <n>',
      'Skip when fewer than this many candidates match. Default 5.',
      '5',
    )
    .option(
      '--dry-run',
      'Materialize the summary without writing or deleting.',
    )
    .option(
      '--format <fmt>',
      'Output format (json|text). Default: json.',
      'json',
    )
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = ConsolidateSchema.parse(rawOpts);

      let store: MemoryStore;
      let summarizeFn: SummarizeFn;
      try {
        // --store with pyproject fallback `memory_store`.
        let storeSpec = opts.store;
        if (!storeSpec) {
          const cfg = resolved.readConfig();
          const v = cfg['memory_store'];
          if (typeof v === 'string' && v.length > 0) storeSpec = v;
        }
        if (!storeSpec) {
          throw new Error(
            'Pass --store MODULE:VAR or set [tool.apx.agent].memory_store ' +
              'in pyproject.toml. The variable must be a MemoryStore instance.',
          );
        }
        store = await resolved.loadStore<MemoryStore>(storeSpec);

        const sf = await resolveCallable<SummarizeFn>(
          {
            spec: opts.summarizeFn,
            pyprojectKey: 'summarize_fn',
            label: '--summarize-fn',
            required: true,
          },
          {
            loadCallable: resolved.loadCallable,
            readConfig: resolved.readConfig,
          },
        );
        // `required: true` guarantees sf is defined.
        summarizeFn = sf!;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(err instanceof Error ? err.message : String(err));
        resolved.exit(1);
        return;
      }

      const tags = parseTags(opts.consolidatedTags) ?? ['consolidated'];

      const result = await consolidateMemories({
        store,
        principalId: opts.principalId,
        summarizeFn,
        ...(opts.namespace !== undefined ? { namespace: opts.namespace } : {}),
        ...(opts.maxAgeSeconds !== undefined
          ? { maxAgeSeconds: opts.maxAgeSeconds }
          : {}),
        ...(opts.minImportance !== undefined
          ? { minImportance: opts.minImportance }
          : {}),
        keepOriginals: opts.keepOriginals,
        consolidatedNamespace: opts.consolidatedNamespace,
        consolidatedTags: tags,
        consolidatedImportance: opts.consolidatedImportance,
        minMemoriesForConsolidation: opts.minMemoriesForConsolidation,
        dryRun: opts.dryRun,
      });

      if (result.consolidatedMemory === null) {
        const msg =
          `# only ${result.candidatesFound} candidate memories ` +
          `(need >= ${opts.minMemoriesForConsolidation})`;
        if (opts.format === 'text') {
          // eslint-disable-next-line no-console
          console.error(msg);
        } else {
          // eslint-disable-next-line no-console
          console.error(
            JSON.stringify(
              {
                candidates_found: result.candidatesFound,
                consolidated_memory: null,
                deleted_ids: [],
                dry_run: opts.dryRun,
                reason: 'below min_memories_for_consolidation',
              },
              null,
              2,
            ),
          );
        }
        resolved.exit(1);
        return;
      }

      if (opts.format === 'text') {
        // eslint-disable-next-line no-console
        console.log(
          `Consolidated ${result.candidatesFound} memories ` +
            `→ ${result.consolidatedMemory.id}`,
        );
        return;
      }

      const payload = {
        candidates_found: result.candidatesFound,
        consolidated_memory: memoryToDto(result.consolidatedMemory),
        deleted_ids: [...result.deletedIds],
        dry_run: opts.dryRun,
      };
      // eslint-disable-next-line no-console
      console.log(JSON.stringify(payload, null, 2));
    });
}

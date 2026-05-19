/**
 * apx examples mine — wrap {@link mineExamples} with file-injectable
 * SessionStore + ExampleStore + caller-supplied callables.
 *
 * Mirrors `python/src/apx_agent/cli.py::examples_mine_cmd`. The store + each
 * `--*-fn` callable resolves via MODULE:VAR with pyproject fallback:
 *
 *   * `--session-store` → `[tool.apx.agent].session_store`
 *   * `--example-store` → `[tool.apx.agent].example_store`
 *   * `--intent-fn`     → `[tool.apx.agent].intent_fn`
 *   * `--score-fn`      → `[tool.apx.agent].score_fn`
 *   * `--filter-fn`     → `[tool.apx.agent].filter_fn`
 *   * `--tags-fn`       → `[tool.apx.agent].tags_fn`
 *   * `--metadata-fn`   → `[tool.apx.agent].metadata_fn`
 *
 * # Group composition
 *
 * The `examples` group is created by {@link registerExamplesCommand}.
 * To stay composition-safe regardless of registration order, this
 * registrar looks for an existing `examples` command and attaches to it,
 * or creates one if missing.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import type { ExampleStore } from '../../example.js';
import { mineExamples, type Turn } from '../../example-mining.js';
import type { SessionStore } from '../../workflows/session.js';
import { readApxAgentConfig } from '../pyproject.js';
import {
  loadStore as defaultLoadStore,
  resolveStore,
} from '../load-store.js';
import {
  loadCallable as defaultLoadCallable,
  resolveCallable,
  type AnyCallable,
} from '../load-callable.js';

// ---------------------------------------------------------------------------
// Option schema — Zod normalizes the raw commander option bag.
// ---------------------------------------------------------------------------

const FormatSchema = z.enum(['json', 'text']).default('json');

const MineSchema = z.object({
  sessionStore: z.string().optional(),
  exampleStore: z.string().optional(),
  agentId: z.string().min(1),
  sessionIds: z.string().optional(),
  intentFn: z.string().optional(),
  scoreFn: z.string().optional(),
  filterFn: z.string().optional(),
  tagsFn: z.string().optional(),
  metadataFn: z.string().optional(),
  limit: z.coerce.number().int().positive().optional(),
  minScore: z.coerce.number().optional(),
  dryRun: z.boolean().default(false),
  format: FormatSchema,
});

// ---------------------------------------------------------------------------
// Deps — tests inject these to stub IO
// ---------------------------------------------------------------------------

export interface MineCommandDeps {
  /** Override the store loader. Defaults to {@link defaultLoadStore}. */
  loadStore?: <T>(spec: string) => Promise<T>;
  /** Override the callable loader. Defaults to {@link defaultLoadCallable}. */
  loadCallable?: <T extends AnyCallable>(spec: string) => Promise<T>;
  /** Override the pyproject reader. Defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
  /** Exit hook — tests pass a recorder; prod passes `process.exit`. */
  exit?: (code: number) => void;
}

interface ResolvedDeps {
  loadStore: <T>(spec: string) => Promise<T>;
  loadCallable: <T extends AnyCallable>(spec: string) => Promise<T>;
  readConfig: () => Record<string, unknown>;
  exit: (code: number) => void;
}

function resolveDeps(deps: MineCommandDeps): ResolvedDeps {
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

function exampleToDto(e: {
  id: string;
  agentId: string;
  intent: string;
  input: string;
  output: string;
  score?: number;
  tags: readonly string[];
  metadata: Readonly<Record<string, unknown>>;
  createdAt: string;
  updatedAt: string;
}): Record<string, unknown> {
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
// Registrar
// ---------------------------------------------------------------------------

/** Find the `examples` group or create one. Idempotent — safe to call
 *  before or after {@link registerExamplesCommand}. */
function ensureExamplesGroup(program: Command): Command {
  const existing = program.commands.find((c) => c.name() === 'examples');
  if (existing) return existing;
  return program
    .command('examples')
    .description(
      'Operate on an ExampleStore — find, save, remove, list, mine. ' +
        'Configure via --store-module MODULE:VAR or [tool.apx.agent].example_store.',
    );
}

export function registerMineCommand(
  program: Command,
  deps: MineCommandDeps = {},
): void {
  const resolved = resolveDeps(deps);
  const group = ensureExamplesGroup(program);

  group
    .command('mine')
    .description(
      'Mine (user, assistant) Examples from a SessionStore\'s history.',
    )
    .option(
      '--session-store <spec>',
      'MODULE:VAR pointing at a SessionStore instance. ' +
        'Falls back to [tool.apx.agent].session_store.',
    )
    .option(
      '--example-store <spec>',
      'MODULE:VAR pointing at an ExampleStore instance. ' +
        'Falls back to [tool.apx.agent].example_store.',
    )
    .requiredOption('--agent-id <id>', 'Agent id stamped on every Example.')
    .option(
      '--session-ids <csv>',
      'Comma-separated session ids. Default: every session in the store.',
    )
    .option(
      '--intent-fn <spec>',
      'MODULE:VAR of a Turn -> string intent classifier.',
    )
    .option(
      '--score-fn <spec>',
      'MODULE:VAR of a Turn -> number|undefined scorer.',
    )
    .option(
      '--filter-fn <spec>',
      'MODULE:VAR of a Turn -> boolean include filter.',
    )
    .option(
      '--tags-fn <spec>',
      'MODULE:VAR of a Turn -> readonly string[] tag extractor.',
    )
    .option(
      '--metadata-fn <spec>',
      'MODULE:VAR of a Turn -> Record<string, unknown> metadata fn.',
    )
    .option('--limit <n>', 'Max examples to write.')
    .option('--min-score <n>', 'Discard turns whose --score-fn returned <n>.')
    .option('--dry-run', 'Compute Examples client-side without writing.')
    .option('--format <fmt>', 'Output format (json|text). Default: json.', 'json')
    .action(async (rawOpts: Record<string, unknown>) => {
      const opts = MineSchema.parse(rawOpts);

      // Resolve stores + callables.
      let sessionStore: SessionStore;
      let exampleStore: ExampleStore;
      let intentFn: ((turn: Turn) => Promise<string> | string) | undefined;
      let scoreFn:
        | ((turn: Turn) => Promise<number | undefined> | number | undefined)
        | undefined;
      let filterFn: ((turn: Turn) => boolean) | undefined;
      let tagsFn: ((turn: Turn) => readonly string[]) | undefined;
      let metadataFn: ((turn: Turn) => Record<string, unknown>) | undefined;
      try {
        sessionStore = await resolveStoreWithKey<SessionStore>(
          opts.sessionStore,
          'session_store',
          '--session-store',
          'a SessionStore instance',
          resolved,
        );
        exampleStore = await resolveStore<ExampleStore>(
          { storeModule: opts.exampleStore },
          'example',
          { loadStore: resolved.loadStore, readConfig: resolved.readConfig },
        );
        intentFn = await resolveCallable<
          (turn: Turn) => Promise<string> | string
        >(
          {
            spec: opts.intentFn,
            pyprojectKey: 'intent_fn',
            label: '--intent-fn',
          },
          { loadCallable: resolved.loadCallable, readConfig: resolved.readConfig },
        );
        scoreFn = await resolveCallable<
          (turn: Turn) => Promise<number | undefined> | number | undefined
        >(
          {
            spec: opts.scoreFn,
            pyprojectKey: 'score_fn',
            label: '--score-fn',
          },
          { loadCallable: resolved.loadCallable, readConfig: resolved.readConfig },
        );
        filterFn = await resolveCallable<(turn: Turn) => boolean>(
          {
            spec: opts.filterFn,
            pyprojectKey: 'filter_fn',
            label: '--filter-fn',
          },
          { loadCallable: resolved.loadCallable, readConfig: resolved.readConfig },
        );
        tagsFn = await resolveCallable<(turn: Turn) => readonly string[]>(
          {
            spec: opts.tagsFn,
            pyprojectKey: 'tags_fn',
            label: '--tags-fn',
          },
          { loadCallable: resolved.loadCallable, readConfig: resolved.readConfig },
        );
        metadataFn = await resolveCallable<
          (turn: Turn) => Record<string, unknown>
        >(
          {
            spec: opts.metadataFn,
            pyprojectKey: 'metadata_fn',
            label: '--metadata-fn',
          },
          { loadCallable: resolved.loadCallable, readConfig: resolved.readConfig },
        );
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(err instanceof Error ? err.message : String(err));
        resolved.exit(1);
        return;
      }

      const sessionIds = opts.sessionIds
        ? opts.sessionIds
            .split(',')
            .map((s) => s.trim())
            .filter((s) => s.length > 0)
        : undefined;

      const result = await mineExamples({
        sessionStore,
        exampleStore,
        agentId: opts.agentId,
        ...(sessionIds !== undefined ? { sessionIds } : {}),
        ...(intentFn !== undefined ? { intentFn } : {}),
        ...(scoreFn !== undefined ? { scoreFn } : {}),
        ...(filterFn !== undefined ? { filterFn } : {}),
        ...(tagsFn !== undefined ? { tagsFn } : {}),
        ...(metadataFn !== undefined ? { metadataFn } : {}),
        ...(opts.limit !== undefined ? { limit: opts.limit } : {}),
        ...(opts.minScore !== undefined ? { minScore: opts.minScore } : {}),
        dryRun: opts.dryRun,
      });

      if (opts.format === 'text') {
        // eslint-disable-next-line no-console
        console.log(
          `Mined ${result.examples.length} examples from ` +
            `${result.sessionsScanned} sessions ` +
            `(${result.turnsConsidered} turns considered)`,
        );
        return;
      }

      const payload = {
        sessions_scanned: result.sessionsScanned,
        turns_considered: result.turnsConsidered,
        examples_added: result.examplesAdded,
        dry_run: opts.dryRun,
        examples: result.examples.map(exampleToDto),
      };
      // eslint-disable-next-line no-console
      console.log(JSON.stringify(payload, null, 2));
    });
}

/**
 * Local twin of {@link resolveStore} that picks an arbitrary pyproject key —
 * the session store's fallback key is `session_store`, which doesn't fit
 * the canonical `memory_store` / `example_store` shape baked into
 * {@link resolveStore}.
 */
async function resolveStoreWithKey<T>(
  spec: string | undefined,
  pyprojectKey: string,
  label: string,
  description: string,
  deps: ResolvedDeps,
): Promise<T> {
  let s = spec;
  if (!s) {
    const cfg = deps.readConfig();
    const v = cfg[pyprojectKey];
    if (typeof v === 'string' && v.length > 0) s = v;
  }
  if (!s) {
    throw new Error(
      `Pass ${label} MODULE:VAR or set [tool.apx.agent].${pyprojectKey} ` +
        `in pyproject.toml. The variable must be ${description}.`,
    );
  }
  return await deps.loadStore<T>(s);
}

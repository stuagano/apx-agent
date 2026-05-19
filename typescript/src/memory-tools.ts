/**
 * memory-tools — agent-facing tools wrapping a `MemoryStore`.
 *
 * Builds three `AgentTool` instances (recall / remember / forget) so an
 * LLM can read and write durable memory through standard tool-calling.
 *
 * `principalId` is resolved per-call: `principalIdResolver()` is tried
 * first (typically reads request-scoped context — `getRequestContext`,
 * OBO headers, etc.), falling back to `defaultPrincipalId`. When neither
 * resolves, the tool degrades gracefully by returning a no-principal
 * string instead of throwing — agents should fail soft on missing
 * identity, not crash the turn.
 *
 * No I/O lives in this module itself — it just wires `defineTool` against
 * the injected store.
 */

import { z } from 'zod';
import { defineTool, type AgentTool } from './agent/tools.js';
import type {
  MemoryStore,
  RecallOptions,
  RecallResult,
} from './memory.js';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export type MemoryToolName = 'recall' | 'remember' | 'forget';

export interface MakeMemoryToolsOptions {
  store: MemoryStore;
  /** Resolve principalId at call time. Return undefined to fall through to default. */
  principalIdResolver?: () => string | undefined;
  defaultPrincipalId?: string;
  namespaceDefault?: string;
  /** Optional name prefix — e.g. 'memory_' produces 'memory_recall'. Default ''. */
  toolPrefix?: string;
  /** Which tools to include. Default: all three. */
  include?: readonly MemoryToolName[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NO_PRINCIPAL = 'No principal_id available; cannot recall memories.';

function resolvePrincipal(opts: MakeMemoryToolsOptions): string | undefined {
  const fromResolver = opts.principalIdResolver?.();
  return fromResolver ?? opts.defaultPrincipalId;
}

function formatRecallResults(results: readonly RecallResult[]): string {
  if (results.length === 0) return 'No memories found.';
  return results
    .map((r) => `- [score=${r.score.toFixed(2)}] ${r.memory.content}`)
    .join('\n');
}

// ---------------------------------------------------------------------------
// makeMemoryTools
// ---------------------------------------------------------------------------

export function makeMemoryTools(opts: MakeMemoryToolsOptions): AgentTool[] {
  const prefix = opts.toolPrefix ?? '';
  const include = new Set<MemoryToolName>(opts.include ?? ['recall', 'remember', 'forget']);
  const namespaceDefault = opts.namespaceDefault;

  const tools: AgentTool[] = [];

  if (include.has('recall')) {
    tools.push(
      defineTool({
        name: `${prefix}recall`,
        description:
          'Recall durable memories relevant to a query. Returns top-k matches as a markdown list.',
        parameters: z.object({
          query: z.string().describe('What to search memory for.'),
          k: z.number().int().positive().optional().describe('Top-k results (default 5).'),
          namespace: z
            .string()
            .optional()
            .describe('Optional namespace filter (e.g. "profile", "episodic").'),
          tags: z.array(z.string()).optional().describe('Optional tag filter (any-of).'),
        }),
        handler: async ({ query, k, namespace, tags }) => {
          const principalId = resolvePrincipal(opts);
          if (!principalId) return NO_PRINCIPAL;
          const recallOpts: RecallOptions = {
            principalId,
            query,
            k: k ?? 5,
            namespace: namespace ?? namespaceDefault,
            tags,
          };
          const results = await opts.store.recall(recallOpts);
          return formatRecallResults(results);
        },
      }),
    );
  }

  if (include.has('remember')) {
    tools.push(
      defineTool({
        name: `${prefix}remember`,
        description:
          'Save a durable memory for later recall. Returns the new memory id.',
        parameters: z.object({
          content: z.string().describe('The memory content to persist.'),
          namespace: z
            .string()
            .optional()
            .describe('Optional namespace bucket (e.g. "profile", "episodic").'),
          tags: z.array(z.string()).optional().describe('Optional tags for filtering.'),
          importance: z
            .number()
            .min(0)
            .max(1)
            .optional()
            .describe('Optional 0..1 importance for ranking / pruning.'),
        }),
        handler: async ({ content, namespace, tags, importance }) => {
          const principalId = resolvePrincipal(opts);
          if (!principalId) return NO_PRINCIPAL;
          const memory = await opts.store.add({
            principalId,
            content,
            namespace: namespace ?? namespaceDefault,
            tags,
            importance,
          });
          return `Saved memory ${memory.id}.`;
        },
      }),
    );
  }

  if (include.has('forget')) {
    tools.push(
      defineTool({
        name: `${prefix}forget`,
        description: 'Delete a memory by id.',
        parameters: z.object({
          id: z.string().describe('Memory id to delete.'),
        }),
        handler: async ({ id }) => {
          const deleted = await opts.store.delete(id);
          return deleted ? `Forgot memory ${id}.` : `No memory found with id ${id}.`;
        },
      }),
    );
  }

  return tools;
}

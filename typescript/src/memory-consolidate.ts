/**
 * memory-consolidate — LLM-backed summarization of older memories into a
 * single durable memory row, optionally deleting the originals.
 *
 * Useful for keeping a principal's memory bank tractable as it grows.
 * Typical recipe:
 *   1. Cron once a day per principal.
 *   2. Pull memories older than N days (`maxAgeSeconds`).
 *   3. Optionally floor on `minImportance` to consolidate noise only.
 *   4. Hand to an LLM via `summarizeFn` — caller wires the call.
 *   5. Write the summary back as a single memory in a `consolidated`
 *      namespace; delete originals unless `keepOriginals`.
 *
 * Same injectable-dependency philosophy as the rest of the framework:
 * `summarizeFn` is the LLM hook. The package imports no LLM SDK.
 *
 * `summarizeFn` contract:
 *   - Input: the list of `Memory` rows the caller chose to consolidate,
 *     in `store.list()` order (newest first).
 *   - Output: a single string — the consolidated content. The function
 *     is responsible for the prompt, the model choice, and any
 *     truncation / chunking.
 *   - May be async.
 *
 * `dryRun` materializes the consolidated `Memory` client-side (via
 * `materializeMemory`) without writing or deleting. The returned
 * Memory's id is a placeholder minted client-side and will NOT match
 * what the store would mint on a real `add()` — treat as opaque.
 */

import {
  materializeMemory,
  type Memory,
  type MemoryStore,
} from './memory.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Result returned by `consolidateMemories`. Frozen. */
export interface ConsolidateResult {
  readonly candidatesFound: number;
  readonly consolidatedMemory: Memory | null;
  readonly deletedIds: readonly string[];
}

export type SummarizeFn = (
  memories: readonly Memory[],
) => Promise<string> | string;

/** Options for `consolidateMemories`. */
export interface ConsolidateMemoriesOptions {
  store: MemoryStore;
  principalId: string;
  namespace?: string;

  /** REQUIRED — caller wires the LLM call. */
  summarizeFn: SummarizeFn;

  /** Only consolidate memories older than `now - maxAgeSeconds`. */
  maxAgeSeconds?: number;

  /** Floor — typically used to consolidate low-importance noise. */
  minImportance?: number;

  /**
   * Default `false`. When `false`, originals are deleted after the
   * consolidated memory is written. When `true`, originals are left in
   * place — useful for read-only audit runs.
   */
  keepOriginals?: boolean;

  /** Default `'consolidated'`. */
  consolidatedNamespace?: string;

  /** Default `['consolidated']`. */
  consolidatedTags?: readonly string[];

  /** Default `0.7`. */
  consolidatedImportance?: number;

  /**
   * Default `5` — return without writing if fewer candidates match.
   * Keeps small banks from being prematurely consolidated.
   */
  minMemoriesForConsolidation?: number;

  /**
   * When `true`, materialize the consolidated memory client-side and
   * skip the writes/deletes. The returned `consolidatedMemory.id` is a
   * placeholder — see top-of-file note.
   */
  dryRun?: boolean;
}

// ---------------------------------------------------------------------------
// Consolidation
// ---------------------------------------------------------------------------

export async function consolidateMemories(
  opts: ConsolidateMemoriesOptions,
): Promise<ConsolidateResult> {
  const {
    store,
    principalId,
    namespace,
    summarizeFn,
    maxAgeSeconds,
    minImportance,
    keepOriginals = false,
    consolidatedNamespace = 'consolidated',
    consolidatedTags = ['consolidated'],
    consolidatedImportance = 0.7,
    minMemoriesForConsolidation = 5,
    dryRun = false,
  } = opts;

  // Pull the candidate pool (newest first by store contract).
  const raw = await store.list({
    principalId,
    namespace,
    minImportance,
    limit: 10_000,
  });

  // Client-side age filter.
  let candidates: Memory[];
  if (maxAgeSeconds !== undefined) {
    const cutoff = Date.now() - maxAgeSeconds * 1000;
    candidates = raw.filter((m) => Date.parse(m.updatedAt) < cutoff);
  } else {
    candidates = [...raw];
  }

  const candidatesFound = candidates.length;

  if (candidatesFound < minMemoriesForConsolidation) {
    return Object.freeze({
      candidatesFound,
      consolidatedMemory: null,
      deletedIds: Object.freeze([] as string[]),
    });
  }

  const content = await summarizeFn(candidates);
  const sourceIds = candidates.map((m) => m.id);

  const input = {
    principalId,
    content,
    namespace: consolidatedNamespace,
    tags: consolidatedTags,
    importance: consolidatedImportance,
    metadata: {
      consolidatedFrom: sourceIds,
      count: candidates.length,
    },
  };

  if (dryRun) {
    return Object.freeze({
      candidatesFound,
      consolidatedMemory: materializeMemory(input),
      deletedIds: Object.freeze([] as string[]),
    });
  }

  const consolidatedMemory = await store.add(input);

  const deletedIds: string[] = [];
  if (!keepOriginals) {
    for (const id of sourceIds) {
      const ok = await store.delete(id);
      if (ok) deletedIds.push(id);
    }
  }

  return Object.freeze({
    candidatesFound,
    consolidatedMemory,
    deletedIds: Object.freeze([...deletedIds]),
  });
}

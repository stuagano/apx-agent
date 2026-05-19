/**
 * prompt-assembly — pure helpers for stitching MemoryStore + ExampleStore
 * recall output into a system-prompt-ready string.
 *
 * No I/O of its own; takes the stores as arguments and calls their
 * recall paths. Returns markdown-formatted strings the caller can splice
 * directly into a system message.
 *
 * Empty stores → empty strings, so callers can unconditionally concat
 * without worrying about stray headers or whitespace.
 */

import type { MemoryStore, RecallOptions } from './memory.js';
import type { ExampleStore, FindSimilarOptions } from './example.js';

// ---------------------------------------------------------------------------
// Memory context
// ---------------------------------------------------------------------------

const DEFAULT_MEMORY_HEADER = '### Relevant memories';

export async function assembleMemoryContext(
  store: MemoryStore,
  opts: RecallOptions,
  header: string = DEFAULT_MEMORY_HEADER,
): Promise<string> {
  const results = await store.recall(opts);
  if (results.length === 0) return '';
  const lines = results.map((r) => `- ${r.memory.content}`);
  return `${header}\n${lines.join('\n')}`;
}

// ---------------------------------------------------------------------------
// Example context
// ---------------------------------------------------------------------------

const DEFAULT_EXAMPLE_HEADER = '### Relevant examples';

export async function assembleExampleContext(
  store: ExampleStore,
  opts: FindSimilarOptions,
  header: string = DEFAULT_EXAMPLE_HEADER,
): Promise<string> {
  const results = await store.findSimilar(opts);
  if (results.length === 0) return '';
  const blocks = results
    .map((r) => `**Q:** ${r.example.input}\n**A:** ${r.example.output}`)
    .join('\n---\n');
  return `${header}\n${blocks}`;
}

// ---------------------------------------------------------------------------
// Combined
// ---------------------------------------------------------------------------

export interface AssembleContextOptions {
  memory?: {
    store: MemoryStore;
    opts: RecallOptions;
    header?: string;
  };
  examples?: {
    store: ExampleStore;
    opts: FindSimilarOptions;
    header?: string;
  };
  /** Joiner placed between the two non-empty blocks. Default '\n\n'. */
  separator?: string;
}

export async function assembleContext(opts: AssembleContextOptions): Promise<string> {
  const separator = opts.separator ?? '\n\n';
  const blocks: string[] = [];
  if (opts.memory) {
    const block = await assembleMemoryContext(
      opts.memory.store,
      opts.memory.opts,
      opts.memory.header,
    );
    if (block) blocks.push(block);
  }
  if (opts.examples) {
    const block = await assembleExampleContext(
      opts.examples.store,
      opts.examples.opts,
      opts.examples.header,
    );
    if (block) blocks.push(block);
  }
  return blocks.join(separator);
}

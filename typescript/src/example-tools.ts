/**
 * example-tools — agent-facing tools wrapping an `ExampleStore`.
 *
 * Builds three `AgentTool` instances (find_examples / save_example /
 * remove_example) so an LLM can read and write few-shot examples through
 * standard tool-calling.
 *
 * `agentId` is resolved per-call via `agentIdResolver()` first (typically
 * reads request-scoped context), falling back to `defaultAgentId`. When
 * neither resolves, the tool degrades gracefully by returning a
 * no-agent string rather than throwing.
 */

import { z } from 'zod';
import { defineTool, type AgentTool } from './agent/tools.js';
import type {
  ExampleResult,
  ExampleStore,
  FindSimilarOptions,
} from './example.js';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export type ExampleToolName = 'find_examples' | 'save_example' | 'remove_example';

export interface MakeExampleToolsOptions {
  store: ExampleStore;
  /** Resolve agentId at call time. Return undefined to fall through to default. */
  agentIdResolver?: () => string | undefined;
  defaultAgentId?: string;
  intentDefault?: string;
  toolPrefix?: string;
  include?: readonly ExampleToolName[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NO_AGENT = 'No agent_id available; cannot look up examples.';

function resolveAgent(opts: MakeExampleToolsOptions): string | undefined {
  const fromResolver = opts.agentIdResolver?.();
  return fromResolver ?? opts.defaultAgentId;
}

function formatExampleResults(results: readonly ExampleResult[]): string {
  if (results.length === 0) return 'No examples found.';
  return results
    .map(
      (r) =>
        `- [score=${r.score.toFixed(2)}] **Q:** ${r.example.input}\n  **A:** ${r.example.output}`,
    )
    .join('\n');
}

// ---------------------------------------------------------------------------
// makeExampleTools
// ---------------------------------------------------------------------------

export function makeExampleTools(opts: MakeExampleToolsOptions): AgentTool[] {
  const prefix = opts.toolPrefix ?? '';
  const include = new Set<ExampleToolName>(
    opts.include ?? ['find_examples', 'save_example', 'remove_example'],
  );
  const intentDefault = opts.intentDefault;

  const tools: AgentTool[] = [];

  if (include.has('find_examples')) {
    tools.push(
      defineTool({
        name: `${prefix}find_examples`,
        description:
          'Find few-shot examples whose inputs are similar to a query. Returns top-k matches.',
        parameters: z.object({
          query: z.string().describe('The query to match against example inputs.'),
          k: z.number().int().positive().optional().describe('Top-k results (default 5).'),
          intent: z.string().optional().describe('Optional intent bucket filter.'),
          tags: z.array(z.string()).optional().describe('Optional tag filter (any-of).'),
        }),
        handler: async ({ query, k, intent, tags }) => {
          const agentId = resolveAgent(opts);
          if (!agentId) return NO_AGENT;
          const findOpts: FindSimilarOptions = {
            agentId,
            query,
            k: k ?? 5,
            intent: intent ?? intentDefault,
            tags,
          };
          const results = await opts.store.findSimilar(findOpts);
          return formatExampleResults(results);
        },
      }),
    );
  }

  if (include.has('save_example')) {
    tools.push(
      defineTool({
        name: `${prefix}save_example`,
        description: 'Save a few-shot example for later retrieval.',
        parameters: z.object({
          input: z.string().describe('The example input/query.'),
          output: z.string().describe('The demonstration response.'),
          intent: z.string().optional().describe('Optional intent bucket.'),
          score: z
            .number()
            .min(0)
            .max(1)
            .optional()
            .describe('Optional 0..1 quality score.'),
          tags: z.array(z.string()).optional().describe('Optional tags.'),
        }),
        handler: async ({ input, output, intent, score, tags }) => {
          const agentId = resolveAgent(opts);
          if (!agentId) return NO_AGENT;
          const example = await opts.store.add({
            agentId,
            input,
            output,
            intent: intent ?? intentDefault,
            score,
            tags,
          });
          return `Saved example ${example.id}.`;
        },
      }),
    );
  }

  if (include.has('remove_example')) {
    tools.push(
      defineTool({
        name: `${prefix}remove_example`,
        description: 'Delete a few-shot example by id.',
        parameters: z.object({
          id: z.string().describe('Example id to delete.'),
        }),
        handler: async ({ id }) => {
          const deleted = await opts.store.delete(id);
          return deleted
            ? `Removed example ${id}.`
            : `No example found with id ${id}.`;
        },
      }),
    );
  }

  return tools;
}

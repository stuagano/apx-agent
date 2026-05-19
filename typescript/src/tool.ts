/**
 * `defineUcTool` — declarative UC-syncable tool factory.
 *
 * TypeScript counterpart to Python's ``@tool(uc=..., grant=[...])`` decorator
 * (see ``python/src/apx_agent/_tool.py``). The Python version is a decorator
 * because Python tools are plain functions; here, tools are already
 * constructed via the {@link defineTool} factory in ``agent/tools.ts``, so we
 * follow the same factory pattern and add a complementary
 * {@link defineUcTool} for the UC-syncable case.
 *
 * Design choice — factory, not extension of `defineTool`:
 *
 *   We chose to ship `defineUcTool` as a separate factory rather than
 *   extending `defineTool` with optional `uc`/`grant` fields. Two reasons:
 *
 *     1. `defineTool` is provider-agnostic — it produces an `AgentTool` that
 *        any LLM provider can call. UC sync is Databricks-specific.
 *        Threading optional UC fields into the base factory leaks platform
 *        concerns into the generic tool shape.
 *     2. Validation of the three-part UC name should fail loudly at tool
 *        construction time. Making `uc` required on the dedicated factory
 *        keeps that contract explicit; making it optional on the base
 *        factory would dilute it.
 *
 *   `defineUcTool` wraps `defineTool` directly and attaches `ToolMetadata`
 *   plus a `uc_function` {@link ResourceSpec} to the returned `AgentTool`
 *   via the WeakMap registries in `resources.ts` and this file.
 *
 * Python semantics that did NOT translate:
 *
 *   - **Dependencies rejection.** Python rejects ``@tool(uc=...)`` on
 *     functions with ``Dependencies.*`` parameters because UC functions
 *     execute server-side under the function owner's identity and can't host
 *     a user-scoped `WorkspaceClient`. TypeScript has no equivalent
 *     `Dependencies` injection mechanism — tools are plain async closures
 *     that capture whatever they need lexically. There's nothing to inspect
 *     at construction time, so the check is omitted. If/when a TS
 *     `Dependencies`-style injection layer ships, this is the spot to add
 *     the same gate.
 *   - **`name` override mutates `__name__` / `__qualname__`.** Python rebinds
 *     the function's identity attributes. The TS equivalent — overriding the
 *     `AgentTool.name` field — happens naturally through the factory because
 *     the caller passes `name` directly to `defineTool`. The metadata
 *     records the override so callers that care can introspect it.
 */

import { z } from 'zod';

import { defineTool, type AgentTool } from './agent/tools.js';
import { attachResources, makeResourceSpec } from './resources.js';

// ---------------------------------------------------------------------------
// Metadata type
// ---------------------------------------------------------------------------

/**
 * Metadata attached to an {@link AgentTool} produced by {@link defineUcTool}.
 *
 * Stored in a module-private WeakMap keyed by the tool object — no
 * enumerable property is added to the returned tool. Read via
 * {@link getToolMetadata}.
 *
 * Frozen at construction.
 */
export interface ToolMetadata {
  /**
   * Fully-qualified UC function name (`catalog.schema.fn`). Always set on
   * tools produced by `defineUcTool`; the `| null` branch exists so future
   * Python-only-style tooling can share the same shape.
   */
  readonly ucName: string | null;
  /**
   * UC principals (users, groups, service principals) that should have
   * `EXECUTE` on the synced function. Empty when grants are managed
   * out-of-band or no UC sync is requested.
   */
  readonly grants: readonly string[];
  /**
   * Tool description supplied to the factory, mirrored here for
   * introspection. Set whenever `defineUcTool` is called (the description
   * is required on the underlying `defineTool`).
   */
  readonly descriptionOverride: string | null;
  /**
   * Tool name supplied to the factory, mirrored here. Always non-null on
   * tools produced by `defineUcTool`.
   */
  readonly nameOverride: string | null;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function validateUcName(uc: string): void {
  const segments = uc.split('.');
  if (segments.length !== 3) {
    throw new Error(
      `defineUcTool({ uc: ${JSON.stringify(uc)} }) must be a fully-qualified ` +
        `three-part UC name of the form catalog.schema.function. ` +
        `Got: ${JSON.stringify(uc)}`,
    );
  }
  for (const part of segments) {
    if (part.length === 0) {
      throw new Error(
        `defineUcTool({ uc: ${JSON.stringify(uc)} }) has an empty segment. ` +
          `Each of catalog, schema, and function must be a non-empty identifier.`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Metadata registry
// ---------------------------------------------------------------------------

/**
 * WeakMap from tool object → ToolMetadata. Separate from the resources
 * registry in `resources.ts` so the two concerns can evolve independently.
 */
const metadataRegistry = new WeakMap<object, ToolMetadata>();

function attachMetadata<T extends object>(tool: T, meta: ToolMetadata): T {
  metadataRegistry.set(tool, meta);
  return tool;
}

/**
 * Return the {@link ToolMetadata} attached to `tool` by {@link defineUcTool},
 * or `null` if none.
 */
export function getToolMetadata(tool: object | null | undefined): ToolMetadata | null {
  if (tool === null || tool === undefined) return null;
  return metadataRegistry.get(tool) ?? null;
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Options accepted by {@link defineUcTool}. Mirrors `defineTool` plus the
 * UC-sync fields. Run-time validation of `uc` (and the lightweight shape of
 * `grant`) happens through a Zod schema; the tool shape itself is checked
 * structurally by TypeScript.
 */
const ucOptionsSchema = z.object({
  uc: z.string().min(1, 'defineUcTool: uc is required'),
  grant: z.array(z.string().min(1)).optional(),
});

export interface DefineUcToolOptions<T extends z.ZodType> {
  /** Tool name shown to the LLM. */
  name: string;
  /** Tool description shown to the LLM. */
  description: string;
  /** Zod schema for tool arguments. */
  parameters: T;
  /** Async tool handler. Receives parsed arguments. */
  handler: (args: z.infer<T>) => Promise<unknown>;
  /**
   * Fully-qualified UC function name (`catalog.schema.function`) to sync
   * this tool into. Required. Validated as exactly three non-empty
   * dot-separated segments.
   */
  uc: string;
  /**
   * UC principals that should have `EXECUTE` on the synced function. Empty
   * by default.
   */
  grant?: string[];
}

/**
 * Define a UC-syncable agent tool.
 *
 * Returns the same {@link AgentTool} shape as {@link defineTool}, with
 * {@link ToolMetadata} attached (queryable via {@link getToolMetadata}) and
 * a `uc_function` {@link makeResourceSpec} attached via `attachResources`
 * so `collectResourceSpecs` picks it up automatically at compile/log time.
 *
 * @example
 *   const classifyIntent = defineUcTool({
 *     name: 'classify_intent',
 *     description: 'Classify a customer query as billing/technical/other.',
 *     parameters: z.object({ query: z.string() }),
 *     handler: async ({ query }) => (query.includes('bill') ? 'billing' : 'other'),
 *     uc: 'main.agents.tools.classify_intent',
 *     grant: ['agent_consumers'],
 *   });
 */
export function defineUcTool<T extends z.ZodType>(opts: DefineUcToolOptions<T>): AgentTool {
  // Validate the UC-specific options. Zod first (catches missing/empty uc),
  // then the structural three-part check.
  const parsed = ucOptionsSchema.parse({ uc: opts.uc, grant: opts.grant });
  validateUcName(parsed.uc);

  const grants: readonly string[] = Object.freeze([...(parsed.grant ?? [])]);

  const tool = defineTool({
    name: opts.name,
    description: opts.description,
    parameters: opts.parameters,
    handler: opts.handler,
  });

  const metadata: ToolMetadata = Object.freeze({
    ucName: parsed.uc,
    grants,
    descriptionOverride: opts.description,
    nameOverride: opts.name,
  });
  attachMetadata(tool, metadata);

  // Auto-declare the UC function as a Mosaic AI resource so log_agent /
  // collectResourceSpecs picks it up without explicit `extra` listing.
  attachResources(tool, [makeResourceSpec('uc_function', parsed.uc)]);

  return tool;
}

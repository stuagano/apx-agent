/**
 * Resource declaration — collect Mosaic AI resource specs from an agent.
 *
 * When an apx-agent is compiled to an MLflow `ChatAgent` and logged to Unity
 * Catalog, the platform requires a declared `resources=[...]` list so it can
 * mint a scoped token at serving time. The agent can only access the
 * resources it declared; everything outside the list is denied.
 *
 * This module provides the mlflow-free spec layer:
 *
 *   - {@link ResourceSpec} — frozen `{kind, identifier}` with validation.
 *   - {@link attachResources} / {@link getResources} — annotate a tool
 *     callable with its declared resources via a WeakMap-keyed registry
 *     (avoids enumerable property pollution and `any` casts).
 *   - {@link subAgentToEndpoint} — translate a sub-agent URL/name to a
 *     `serving_endpoint` spec (returns `null` for Apps URLs).
 *   - {@link collectResourceSpecs} — walk an agent's declared tools and
 *     sub-agents to build a deduplicated spec list, preserving first-
 *     occurrence order.
 *
 * Design note — agent walking:
 *
 *   The Python module reaches into each agent type's internal state to
 *   enumerate tool callables and sub-agent URLs. TypeScript has no
 *   equivalent "walk the framework's agent classes" mechanic available to
 *   this module without a hard dependency on the agent class hierarchy.
 *
 *   Instead, {@link collectResourceSpecs} takes `toolIterator` and
 *   `subAgentIterator` callbacks. The caller (typically the agent
 *   compilation/logging layer) supplies functions that yield the tools and
 *   sub-agent URLs for a given agent — including any recursion into
 *   composite agents like Sequential/Parallel/Loop/Router/Handoff. This
 *   keeps `resources.ts` decoupled from the concrete agent classes while
 *   preserving the exact ordering/dedup semantics of the Python version.
 *
 *   - {@link mlflowResourcesFor} — materialise specs into MLflow's serialized
 *     resource shape (the mlflow-SDK-free port of Python's
 *     `mlflow_resources_for`). Plugs into `logAgent`'s `materializeResources`.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Supported Databricks resource kinds. Strict union — passing anything else
 * to {@link makeResourceSpec} throws.
 */
export type ResourceKind =
  | 'uc_function'
  | 'genie_space'
  | 'serving_endpoint'
  | 'sql_warehouse'
  | 'vector_search_index'
  | 'uc_table';

const VALID_KINDS: ReadonlySet<string> = new Set<ResourceKind>([
  'uc_function',
  'genie_space',
  'serving_endpoint',
  'sql_warehouse',
  'vector_search_index',
  'uc_table',
]);

/**
 * Lightweight, mlflow-free description of a Databricks resource.
 *
 * Frozen at construction. Two specs with the same `(kind, identifier)`
 * compare equal by value (use the dedup helper or stringify on a sentinel).
 */
export interface ResourceSpec {
  readonly kind: ResourceKind;
  readonly identifier: string;
}

const ResourceSpecSchema = z.object({
  kind: z.string(),
  identifier: z.string(),
});

/**
 * Construct a validated {@link ResourceSpec}. Throws if `kind` is not one
 * of the supported kinds, or if `identifier` is empty.
 */
export function makeResourceSpec(kind: string, identifier: string): ResourceSpec {
  const parsed = ResourceSpecSchema.parse({ kind, identifier });
  if (!VALID_KINDS.has(parsed.kind)) {
    const valid = [...VALID_KINDS].sort().map((k) => JSON.stringify(k)).join(', ');
    throw new Error(`Unknown resource kind ${JSON.stringify(parsed.kind)}. Valid kinds: ${valid}`);
  }
  if (parsed.identifier.length === 0) {
    throw new Error(`ResourceSpec(${JSON.stringify(parsed.kind)}) requires a non-empty identifier`);
  }
  return Object.freeze({
    kind: parsed.kind as ResourceKind,
    identifier: parsed.identifier,
  });
}

// ---------------------------------------------------------------------------
// Tool-side annotation helpers (WeakMap-keyed registry)
// ---------------------------------------------------------------------------

/**
 * Registry mapping a tool callable (or any object) to its declared resource
 * list. WeakMap keys are GC'd with the callable; no enumerable surface is
 * added to user-supplied functions.
 */
const resourceRegistry = new WeakMap<object, ResourceSpec[]>();

/**
 * Annotate `fn` with a list of declared {@link ResourceSpec}s, appending to
 * any existing list (matches the Python helper's behaviour). Returns `fn`
 * for chaining.
 *
 * @example
 *   const myTool = defineTool({ ... });
 *   attachResources(myTool, [makeResourceSpec('uc_table', 'main.sales.orders')]);
 */
export function attachResources<T extends object>(fn: T, specs: Iterable<ResourceSpec>): T {
  const existing = resourceRegistry.get(fn) ?? [];
  const next = [...existing, ...specs];
  resourceRegistry.set(fn, next);
  return fn;
}

/**
 * Return the declared resource list for `fn`, or `[]` if none were
 * attached. The returned array is a defensive copy.
 */
export function getResources(fn: object | null | undefined): ResourceSpec[] {
  if (fn === null || fn === undefined) return [];
  const specs = resourceRegistry.get(fn);
  return specs ? [...specs] : [];
}

// ---------------------------------------------------------------------------
// sub_agent URL → serving_endpoint translation
// ---------------------------------------------------------------------------

/**
 * Translate a sub-agent entry (URL, bare name, or `$VAR` env reference) to
 * a `serving_endpoint` {@link ResourceSpec}. Returns `null` when the entry
 * is not a Model Serving endpoint — for instance, Databricks Apps URLs are
 * declared via app-to-app `CAN_USE` permissions, not via MLflow resources.
 *
 * Accepted forms (after `$VAR` / `${VAR}` expansion via `process.env`):
 *
 *   - `endpoints/<name>`                       → serving_endpoint(<name>)
 *   - `serving-endpoints/<name>`               → serving_endpoint(<name>)
 *   - `<name>` (bare, no scheme, no path)      → serving_endpoint(<name>)
 *   - `https://<host>/serving-endpoints/<name>/invocations`
 *                                              → serving_endpoint(<name>)
 *   - `https://<app>.databricksapps.com/...`   → null
 */
export function subAgentToEndpoint(raw: string): ResourceSpec | null {
  let value = raw;

  // Expand $VAR / ${VAR}
  if (value.startsWith('$')) {
    const trimmed = value.replace(/^\$+/, '');
    const varName = trimmed.replace(/^\{/, '').replace(/\}$/, '');
    const expanded = process.env[varName] ?? '';
    if (expanded.length === 0) {
      // Unset / empty — caller should treat as no endpoint.
      return null;
    }
    value = expanded;
  }

  if (value.length === 0) return null;

  // Apps URLs are not Model Serving endpoints.
  if (value.includes('databricksapps.com')) return null;

  // Strip scheme + host if present.
  let name = value;
  const schemeIdx = name.indexOf('://');
  if (schemeIdx !== -1) {
    const rest = name.slice(schemeIdx + 3);
    const slashIdx = rest.indexOf('/');
    name = slashIdx === -1 ? '' : rest.slice(slashIdx + 1);
  }

  if (name.length === 0) return null;

  // Normalise known prefixes (strip the first match only).
  for (const prefix of ['serving-endpoints/', 'endpoints/']) {
    if (name.startsWith(prefix)) {
      name = name.slice(prefix.length);
      break;
    }
  }

  // Trim trailing path components (e.g. "/invocations").
  const trailIdx = name.indexOf('/');
  if (trailIdx !== -1) {
    name = name.slice(0, trailIdx);
  }

  name = name.trim();
  if (name.length === 0) return null;

  return makeResourceSpec('serving_endpoint', name);
}

// ---------------------------------------------------------------------------
// Spec collection
// ---------------------------------------------------------------------------

const CollectOptionsSchema = z.object({
  model: z.string().optional(),
  extra: z.array(ResourceSpecSchema).optional(),
});

export interface CollectResourceSpecsOptions<Agent> {
  /** The agent (or composite agent) to walk. */
  agent: Agent;
  /**
   * Optional LLM serving endpoint name. When supplied, a
   * `serving_endpoint` spec is prepended to the output.
   */
  model?: string;
  /**
   * Escape hatch for resources the iterators can't infer — SQL warehouses,
   * UC tables, vector indices, etc.
   */
  extra?: Iterable<ResourceSpec>;
  /**
   * Yields every raw tool callable registered anywhere in the agent tree,
   * in registration order. The caller owns the recursion into composite
   * agents (Sequential / Parallel / Loop / Router / Handoff).
   */
  toolIterator: (agent: Agent) => Iterable<unknown>;
  /**
   * Yields every raw sub-agent URL or env-reference string registered
   * anywhere in the agent tree, in registration order.
   */
  subAgentIterator: (agent: Agent) => Iterable<string>;
}

/**
 * Walk an agent and return a deduplicated list of declared
 * {@link ResourceSpec}s. Order:
 *
 *   1. `model` (if provided) → one `serving_endpoint` spec.
 *   2. Every spec attached to a tool yielded by `toolIterator`, in order.
 *   3. Every sub-agent endpoint yielded by `subAgentIterator` that resolves
 *      to a Model Serving endpoint (Apps URLs are skipped).
 *   4. `extra` specs, in iteration order.
 *
 * Duplicates (same `kind` + `identifier`) are dropped, preserving the
 * first occurrence.
 */
export function collectResourceSpecs<Agent>(
  options: CollectResourceSpecsOptions<Agent>,
): ResourceSpec[] {
  // Validate the data-only options; iterators are functions, not schema'd.
  CollectOptionsSchema.parse({
    model: options.model,
    extra: options.extra ? [...options.extra] : undefined,
  });

  const seen = new Set<string>();
  const out: ResourceSpec[] = [];

  const add = (spec: ResourceSpec): void => {
    const key = `${spec.kind} ${spec.identifier}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(spec);
  };

  if (options.model) {
    add(makeResourceSpec('serving_endpoint', options.model));
  }

  for (const fn of options.toolIterator(options.agent)) {
    if (typeof fn !== 'object' && typeof fn !== 'function') continue;
    if (fn === null) continue;
    for (const spec of getResources(fn as object)) {
      add(spec);
    }
  }

  for (const subUrl of options.subAgentIterator(options.agent)) {
    const ep = subAgentToEndpoint(subUrl);
    if (ep !== null) add(ep);
  }

  if (options.extra) {
    for (const spec of options.extra) {
      // Re-validate to catch malformed user input.
      add(makeResourceSpec(spec.kind, spec.identifier));
    }
  }

  return out;
}

// ---------------------------------------------------------------------------
// mlflow resource materialisation (mlflow-SDK-free)
// ---------------------------------------------------------------------------

/**
 * One serialized MLflow resource entry, e.g. `{ serving_endpoint: [{ name: 'ep' }] }`.
 * Mirrors the shape `mlflow.models.resources.Databricks*.to_dict()` produces.
 */
export interface MlflowResource {
  readonly [resourceKey: string]: ReadonlyArray<{ readonly name: string }>;
}

/**
 * Map each apx {@link ResourceKind} to the key MLflow uses in its serialized
 * resource dict. Four match 1:1; `uc_function`→`function` and `uc_table`→`table`
 * are the two renames. Total over the union, so a new kind is a compile error.
 */
const KIND_TO_MLFLOW_KEY: Record<ResourceKind, string> = {
  uc_function: 'function',
  genie_space: 'genie_space',
  serving_endpoint: 'serving_endpoint',
  sql_warehouse: 'sql_warehouse',
  vector_search_index: 'vector_search_index',
  uc_table: 'table',
};

/**
 * Materialise {@link ResourceSpec}s into MLflow's serialized resource shape —
 * the mlflow-SDK-free port of Python's `mlflow_resources_for`.
 *
 * Returns one per-resource dict per spec, in order, mirroring Python's
 * `list[mlflow.models.resources.Databricks*]` (each element ≈ one object's
 * `.to_dict()`). Pass it straight into `logAgent`'s `materializeResources`.
 *
 * There is no MLflow JS SDK, so this emits plain dicts, not resource objects.
 * The consuming `mlflowLogModel` shim is responsible for AGGREGATING these
 * per-resource entries into MLflow's grouped
 * `{ databricks: { … }, api_version: '1' }` form — exactly what mlflow's
 * `_ResourceBuilder` does internally on the Python side — before POSTing to
 * the log-model REST endpoint.
 */
export function mlflowResourcesFor(specs: readonly ResourceSpec[]): MlflowResource[] {
  return specs.map((spec) => ({ [KIND_TO_MLFLOW_KEY[spec.kind]]: [{ name: spec.identifier }] }));
}

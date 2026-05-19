/**
 * load-callable.ts — dynamic-import an arbitrary callable from a
 * `module:variable` spec.
 *
 * Mirrors {@link loadStore} in `load-store.ts`, generalized: callables don't
 * have a canonical pyproject key, so each `--*-fn` flag picks its own
 * fallback key (`intent_fn`, `score_fn`, `summarize_fn`, etc.).
 *
 * Two layers:
 *
 *   * `loadCallable(spec)` — pure import-and-return. Throws on bad spec or
 *     missing export. Mirrors `loadStore`.
 *   * `resolveCallable({ spec }, opts, deps?)` — flag-first → pyproject
 *     fallback. Returns `undefined` when nothing is configured and
 *     `opts.required` is `false`. Throws when `required` is `true`.
 *
 * # Why a separate file (vs. reusing `loadStore`)
 *
 * `loadStore` is shaped around two known store kinds with hard-coded
 * pyproject keys (`memory_store` / `example_store`). The mining + consolidate
 * commands need ≥6 distinct callable slots, each with a different fallback
 * key. Reusing `loadStore` would force a switch on every call site;
 * a tiny dedicated helper keeps each command's wiring legible.
 */

import { parseModuleSpec, resolveModuleSpecifier } from './load-agent.js';
import { readApxAgentConfig } from './pyproject.js';

/** A callable resolved from MODULE:VAR. Type-erased on purpose — the caller
 *  asserts the shape after import. */
export type AnyCallable = (...args: never[]) => unknown;

/**
 * Import `module:variable` and return the named export as a callable.
 *
 * @throws Error when the underlying `import()` rejects, the module has no
 *   such export, or the export is not callable.
 */
export async function loadCallable<T extends AnyCallable = AnyCallable>(
  spec: string,
): Promise<T> {
  const { module: moduleStr, variable } = parseModuleSpec(spec);
  const specifier = resolveModuleSpecifier(moduleStr);

  let mod: Record<string, unknown>;
  try {
    mod = (await import(specifier)) as Record<string, unknown>;
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Failed to import callable module ${JSON.stringify(moduleStr)}: ${reason}. ` +
        `Make sure the module resolves from the current directory and is ` +
        `compiled JavaScript (Node's import() cannot load .ts files directly).`,
    );
  }

  if (!(variable in mod)) {
    throw new Error(
      `Module ${JSON.stringify(moduleStr)} has no export ${JSON.stringify(variable)}.`,
    );
  }
  const value = mod[variable];
  if (typeof value !== 'function') {
    throw new Error(
      `Export ${JSON.stringify(variable)} from ${JSON.stringify(moduleStr)} ` +
        `is not callable.`,
    );
  }
  return value as T;
}

export interface ResolveCallableOptions {
  /** The `--*-fn` flag value. */
  spec?: string;
  /** Pyproject `[tool.apx.agent].<key>` to read when `spec` is empty. */
  pyprojectKey: string;
  /** Human label used in error messages (e.g. `--summarize-fn`). */
  label: string;
  /** When `true`, throw if neither flag nor pyproject is set. */
  required?: boolean;
}

export interface ResolveCallableDeps {
  /** Override for tests — defaults to {@link loadCallable}. */
  loadCallable?: <T extends AnyCallable>(spec: string) => Promise<T>;
  /** Override for tests — defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
}

/**
 * Resolve a callable from a CLI flag → pyproject fallback.
 *
 * Returns `undefined` when neither source is set and `opts.required` is
 * `false`. Throws when `required` is `true`.
 */
export async function resolveCallable<T extends AnyCallable = AnyCallable>(
  opts: ResolveCallableOptions,
  deps: ResolveCallableDeps = {},
): Promise<T | undefined> {
  const load =
    deps.loadCallable ??
    (loadCallable as <U extends AnyCallable>(spec: string) => Promise<U>);
  const readConfig = deps.readConfig ?? readApxAgentConfig;

  let spec = opts.spec;
  if (!spec) {
    const cfg = readConfig();
    const v = cfg[opts.pyprojectKey];
    if (typeof v === 'string' && v.length > 0) spec = v;
  }
  if (!spec) {
    if (opts.required) {
      throw new Error(
        `Pass the ${opts.label} MODULE:VAR flag or set ` +
          `[tool.apx.agent].${opts.pyprojectKey} in pyproject.toml.`,
      );
    }
    return undefined;
  }
  return await load<T>(spec);
}

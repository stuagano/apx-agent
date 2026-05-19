/**
 * load-store.ts — dynamic-import a {@link MemoryStore} or {@link ExampleStore}
 * instance from a `module:variable` spec.
 *
 * Mirrors {@link loadAgent} in `load-agent.ts`. The CLI's `apx memory ...`
 * and `apx examples ...` commands both accept a `--store-module MODULE:VAR`
 * flag; when absent, the store spec falls back to
 * `[tool.apx.agent].memory_store` / `.example_store` in `pyproject.toml`.
 *
 * # Why a separate file
 *
 * `loadAgent` is shaped for agents (returns "any object the agent commands
 * introspect"); stores have a stronger contract (a concrete `MemoryStore` /
 * `ExampleStore` instance) and resolve through two configuration sources
 * (flag, then pyproject). Splitting the loader keeps each concern small.
 *
 * # Test-friendly factories
 *
 * `registerMemoryCommand` / `registerExamplesCommand` accept a `loadStore`
 * dep so tests inject a pre-built `InMemoryMemoryStore` / `InMemoryExampleStore`
 * directly, bypassing the filesystem import that would force tests to spin
 * up scratch modules on disk.
 */

import { parseModuleSpec, resolveModuleSpecifier } from './load-agent.js';
import { readApxAgentConfig } from './pyproject.js';

/**
 * Import `module:variable` and return the named export.
 *
 * @throws Error with a "Failed to import X" message when the underlying
 *   `import()` rejects, or when the module has no such export. Callers
 *   re-emit this on stderr and exit non-zero (see the command files).
 */
export async function loadStore<T = unknown>(spec: string): Promise<T> {
  const { module: moduleStr, variable } = parseModuleSpec(spec);
  const specifier = resolveModuleSpecifier(moduleStr);

  let mod: Record<string, unknown>;
  try {
    mod = (await import(specifier)) as Record<string, unknown>;
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Failed to import store module ${JSON.stringify(moduleStr)}: ${reason}. ` +
        `Make sure the module resolves from the current directory and is ` +
        `compiled JavaScript (Node's import() cannot load .ts files directly; ` +
        `compile first or run the CLI via tsx).`,
    );
  }

  if (!(variable in mod)) {
    throw new Error(
      `Module ${JSON.stringify(moduleStr)} has no export ${JSON.stringify(variable)}.`,
    );
  }
  return mod[variable] as T;
}

/**
 * Parse a `--tags` CSV value. Mirrors the Python `_parse_tags` semantics:
 *
 *   - `undefined` / empty / whitespace-only → `undefined` (any-tag wildcard).
 *   - Comma-separated tags → trimmed array of non-empty segments.
 *   - All-empty segments (`",,"`) → `undefined` (not an empty array).
 *
 * The "empty → undefined" rule matches the store filter semantics: tags
 * filter is any-of, so an empty filter must mean "match all," not "match
 * memories with no tags."
 */
export function parseTags(raw?: string | null): string[] | undefined {
  if (!raw) return undefined;
  const parts = raw
    .split(',')
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
  return parts.length > 0 ? parts : undefined;
}

export type StoreKind = 'memory' | 'example';

export interface ResolveStoreOptions {
  storeModule?: string;
}

export interface ResolveStoreDeps {
  /** Override for tests — defaults to {@link loadStore}. */
  loadStore?: <T>(spec: string) => Promise<T>;
  /** Override for tests — defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
}

/**
 * Resolve a store from CLI opts + pyproject fallback.
 *
 * Resolution order:
 *   1. `opts.storeModule` — the `--store-module MODULE:VAR` flag value.
 *   2. `[tool.apx.agent].{kind}_store` in `pyproject.toml`.
 *
 * @throws Error when no spec is configured anywhere. Callers re-emit to
 *   stderr and exit 1.
 */
export async function resolveStore<T = unknown>(
  opts: ResolveStoreOptions,
  kind: StoreKind,
  deps: ResolveStoreDeps = {},
): Promise<T> {
  const load = deps.loadStore ?? (loadStore as <U>(spec: string) => Promise<U>);
  const readConfig = deps.readConfig ?? readApxAgentConfig;

  let spec = opts.storeModule;
  if (!spec) {
    const cfg = readConfig();
    const cfgKey = `${kind}_store`;
    const v = cfg[cfgKey];
    if (typeof v === 'string' && v.length > 0) spec = v;
  }
  if (!spec) {
    throw new Error(
      `Pass --store-module MODULE:VAR or set ` +
        `[tool.apx.agent].${kind}_store in pyproject.toml. ` +
        `The variable must be a ${kind === 'memory' ? 'MemoryStore' : 'ExampleStore'} instance.`,
    );
  }
  return await load<T>(spec);
}

/**
 * load-agent.ts — dynamic-import an agent from a `module:variable` spec.
 *
 * Mirrors the Python `_load_agent` helper. The CLI accepts `--module ...`
 * everywhere; this module turns that string into an object the commands can
 * introspect.
 *
 * # Important compile-time gotcha
 *
 * Node's dynamic `import()` only loads JS — it has no TypeScript runtime.
 * To run a TS agent, the user must either:
 *
 *   1. Compile their TS to JS first (e.g. `tsc`, `tsdown`) and point the
 *      CLI at the compiled output: `apx info --module dist/agent.js:agent`.
 *   2. Run the CLI via a TS runner that registers a loader hook, e.g.:
 *      `tsx node_modules/.bin/apx info --module src/agent.ts:agent`.
 *
 * Without one of those, `import('./src/agent.ts')` will throw — Node has no
 * built-in `.ts` loader. The error message surfaces this rather than letting
 * the user guess.
 *
 * # Spec grammar
 *
 *   "<module>:<variable>"
 *
 * Both halves must be non-empty. `<module>` can be:
 *
 *   - A bare specifier  — `my-pkg/agent`, `apx-internal-runtime`. Resolved via
 *     Node's normal resolution from the cwd.
 *   - A relative path   — `./agent.js`, `../foo/agent.mjs`. Resolved
 *     against `process.cwd()` and converted to a `file://` URL.
 *   - An absolute path  — `/path/to/agent.js`. Same as above.
 */

import { pathToFileURL } from 'node:url';
import { isAbsolute, resolve } from 'node:path';

export interface ParsedModuleSpec {
  readonly module: string;
  readonly variable: string;
}

/**
 * Split `"<module>:<variable>"` into its two halves.
 *
 * @throws Error when the spec is missing `:` or either side is empty.
 */
export function parseModuleSpec(spec: string): ParsedModuleSpec {
  if (!spec.includes(':')) {
    throw new Error(
      `Expected MODULE:VARIABLE, got ${JSON.stringify(spec)}. ` +
        `Example: 'my-agent:agent'.`,
    );
  }
  // Split on the LAST `:` so `file:///abs/path.mjs:agent` parses correctly
  // (the URL form contains its own colons). Variable names can't contain
  // colons in any reasonable JS module export, so the rightmost wins.
  const idx = spec.lastIndexOf(':');
  const moduleStr = spec.slice(0, idx);
  const variable = spec.slice(idx + 1);
  if (!moduleStr || !variable) {
    throw new Error(
      `Both MODULE and VARIABLE must be non-empty, got ${JSON.stringify(spec)}.`,
    );
  }
  return { module: moduleStr, variable };
}

/**
 * Resolve the module string to something `import()` can load.
 *
 * Path-ish strings (`./foo`, `../foo`, `/abs/foo`) become `file://` URLs
 * anchored at the current working directory. Bare specifiers pass through
 * unchanged for Node's normal resolution.
 */
export function resolveModuleSpecifier(moduleStr: string): string {
  // Pre-formed URLs (file://, node:, etc.) pass through to import() unchanged.
  if (/^[a-z][a-z0-9+\-.]*:\/\//i.test(moduleStr) || moduleStr.startsWith('node:')) {
    return moduleStr;
  }
  if (moduleStr.startsWith('./') || moduleStr.startsWith('../') || isAbsolute(moduleStr)) {
    return pathToFileURL(resolve(process.cwd(), moduleStr)).href;
  }
  return moduleStr;
}

/**
 * Import `module:variable` and return the named export.
 *
 * @throws Error with a Python-style "Failed to import X" message when the
 *   underlying `import()` rejects, or when the module has no such export.
 */
export async function loadAgent(spec: string): Promise<unknown> {
  const { module: moduleStr, variable } = parseModuleSpec(spec);
  const specifier = resolveModuleSpecifier(moduleStr);

  let mod: Record<string, unknown>;
  try {
    mod = (await import(specifier)) as Record<string, unknown>;
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Failed to import ${JSON.stringify(moduleStr)}: ${reason}. ` +
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
  return mod[variable];
}

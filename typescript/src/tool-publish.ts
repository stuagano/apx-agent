/**
 * `publishToolsToUc` — sync `defineUcTool` functions into Unity Catalog.
 *
 * TypeScript counterpart to ``python/src/apx_agent/_tool_publish.py``. Walks
 * a list of {@link AgentTool}s, finds every tool annotated with
 * {@link ToolMetadata.ucName}, and registers each as a Unity Catalog Python
 * function via the injected `UcFunctionClient`. Applies declarative grants
 * through the injected `WsClient`.
 *
 * Design differences from the Python version:
 *
 *   - **No agent walking.** Python walks a `BaseAgent` tree via
 *     `_iter_tool_fns(agent)` because Python tools are bare callables held
 *     inside agent objects. In TS, tools are `AgentTool` records constructed
 *     up front; the caller already has a list of them. `publishToolsToUc`
 *     takes `tools: AgentTool[]` directly. If/when a TS agent class hierarchy
 *     materialises and you need tree-walking, build a `collectTools(agent)`
 *     helper next to `collectResourceSpecs` in `resources.ts` and call it
 *     before `publishToolsToUc`.
 *   - **Clients are injected, not constructed.** Python falls back to a
 *     default `DatabricksFunctionClient()` and `WorkspaceClient()`. TS has
 *     no canonical "default Databricks SDK in this process" pattern, so the
 *     caller passes minimal client objects. The interfaces below describe
 *     the exact surface used; any adapter that conforms is acceptable.
 *
 * Python semantics preserved:
 *
 *   - Idempotent enumeration — same `ucName` across multiple tool entries
 *     yields one publish call.
 *   - `dryRun: true` skips all writes and returns results with
 *     `skipped: true, skipReason: 'dry_run'`.
 *   - `replace: true` (default) forwards to `createPythonFunction`.
 *   - `createPythonFunction` failure raises (TS throws an Error wrapping
 *     the underlying cause; Python raised `RuntimeError`).
 *   - Grant failures log-and-continue — the function is published, the
 *     failed principal is dropped from `grantsApplied`.
 */

import { z } from 'zod';

import type { AgentTool } from './agent/tools.js';
import { getToolMetadata } from './tool.js';

// ---------------------------------------------------------------------------
// Client interfaces (injection points)
// ---------------------------------------------------------------------------

/**
 * Minimal surface required from a Unity Catalog function client. Mirrors the
 * `create_python_function` call in `unitycatalog-ai`.
 *
 * Implementations are expected to register `func` as a UC Python function
 * under `catalog`.`schema`; the function name is implementation-defined
 * (typically `func.name`).
 */
export interface UcFunctionClient {
  createPythonFunction(opts: {
    func: unknown;
    catalog: string;
    schema: string;
    replace?: boolean;
  }): Promise<void>;
}

/**
 * Minimal surface required from a Databricks workspace client for the
 * GRANT step. `apiRequest` issues an authenticated REST call against the
 * workspace.
 *
 * Compared to Python's `WorkspaceClient.api_client.do(method, path, body)`,
 * this is a thin async wrapper — any adapter that posts to the Databricks
 * API conforms.
 */
export interface WsClient {
  apiRequest(method: string, path: string, body?: object): Promise<void>;
}

// ---------------------------------------------------------------------------
// Result type
// ---------------------------------------------------------------------------

/**
 * Outcome of publishing a single `defineUcTool` to Unity Catalog.
 * Frozen at construction.
 */
export interface PublishResult {
  readonly ucName: string;
  readonly functionName: string;
  readonly grantsApplied: readonly string[];
  readonly skipped: boolean;
  readonly skipReason?: string;
}

function makePublishResult(opts: {
  ucName: string;
  functionName: string;
  grantsApplied: readonly string[];
  skipped?: boolean;
  skipReason?: string;
}): PublishResult {
  return Object.freeze({
    ucName: opts.ucName,
    functionName: opts.functionName,
    grantsApplied: Object.freeze([...opts.grantsApplied]),
    skipped: opts.skipped ?? false,
    skipReason: opts.skipReason,
  });
}

// ---------------------------------------------------------------------------
// Options validation
// ---------------------------------------------------------------------------

const PublishOptionsSchema = z.object({
  dryRun: z.boolean().optional(),
  replace: z.boolean().optional(),
});

export interface PublishToolsToUcOptions {
  /**
   * The tools to consider for UC publication. Tools without UC metadata
   * (i.e. not produced by `defineUcTool`) are silently skipped.
   */
  tools: readonly AgentTool[];
  /**
   * Client used to register the Python function in UC.
   */
  ucClient: UcFunctionClient;
  /**
   * Workspace client used for the GRANT step. Required only when at least
   * one tool declares `grant`; omit otherwise.
   */
  workspaceClient?: WsClient;
  /**
   * When `true`, walks the list and reports what would be published without
   * making any UC writes.
   */
  dryRun?: boolean;
  /**
   * When `true` (default), forwards `replace: true` to
   * `createPythonFunction` so existing functions with the same name are
   * overwritten. Set to `false` for a "fail if exists" mode useful in CI.
   */
  replace?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function grantExecute(ws: WsClient, ucName: string, principal: string): Promise<void> {
  await ws.apiRequest('PATCH', `/api/2.1/unity-catalog/permissions/function/${ucName}`, {
    changes: [
      {
        principal,
        add: ['EXECUTE'],
      },
    ],
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Publish every tool with UC metadata to Unity Catalog.
 *
 * Returns one {@link PublishResult} per processed tool. Tools whose
 * `ucName` was already published in this call are deduplicated (first
 * occurrence wins). Grant failures are logged via `console.warn` and the
 * principal is dropped from `grantsApplied`; the publish itself proceeds.
 *
 * Throws if any `createPythonFunction` call fails — that's a hard failure
 * (matches Python's `RuntimeError` semantics).
 */
export async function publishToolsToUc(opts: PublishToolsToUcOptions): Promise<PublishResult[]> {
  PublishOptionsSchema.parse({ dryRun: opts.dryRun, replace: opts.replace });

  const replace = opts.replace ?? true;
  const dryRun = opts.dryRun ?? false;

  const results: PublishResult[] = [];

  // Idempotent enumeration: same ucName seen twice → publish once.
  const seen = new Set<string>();
  const targets: Array<{ tool: AgentTool; ucName: string; grants: readonly string[] }> = [];
  for (const tool of opts.tools) {
    const meta = getToolMetadata(tool);
    if (meta === null || meta.ucName === null) continue;
    if (seen.has(meta.ucName)) continue;
    seen.add(meta.ucName);
    targets.push({ tool, ucName: meta.ucName, grants: meta.grants });
  }

  if (targets.length === 0) return results;

  if (dryRun) {
    for (const t of targets) {
      results.push(
        makePublishResult({
          ucName: t.ucName,
          functionName: t.tool.name,
          grantsApplied: t.grants,
          skipped: true,
          skipReason: 'dry_run',
        }),
      );
    }
    return results;
  }

  const needsGrants = targets.some((t) => t.grants.length > 0);
  if (needsGrants && opts.workspaceClient === undefined) {
    throw new Error(
      'publishToolsToUc: at least one tool declares grants but no workspaceClient was supplied. ' +
        'Pass `workspaceClient` to apply UC permissions.',
    );
  }

  for (const t of targets) {
    const segments = t.ucName.split('.');
    // `defineUcTool` validates this at construction, but guard here too
    // for callers that bypass the factory.
    if (segments.length !== 3) {
      throw new Error(
        `publishToolsToUc: invalid UC name ${JSON.stringify(t.ucName)} — expected catalog.schema.function`,
      );
    }
    const [catalog, schema] = segments as [string, string, string];

    try {
      await opts.ucClient.createPythonFunction({
        func: t.tool,
        catalog,
        schema,
        replace,
      });
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new Error(`Failed to publish ${JSON.stringify(t.ucName)} to UC: ${reason}`);
    }

    const grantsApplied: string[] = [];
    for (const principal of t.grants) {
      try {
        // `workspaceClient` is guaranteed defined when we reach this branch
        // because we threw above if grants were requested without one.
        await grantExecute(opts.workspaceClient!, t.ucName, principal);
        grantsApplied.push(principal);
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        // eslint-disable-next-line no-console
        console.warn(
          `GRANT EXECUTE on ${t.ucName} to ${principal} failed: ${reason} — ` +
            `function is published but ungranted.`,
        );
      }
    }

    results.push(
      makePublishResult({
        ucName: t.ucName,
        functionName: t.tool.name,
        grantsApplied,
      }),
    );
  }

  return results;
}

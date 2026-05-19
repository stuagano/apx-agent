/**
 * Databricks Managed MCP integration — generate URLs and client configs.
 *
 * Databricks Managed MCP (Public Preview, Jan 2026) exposes Unity Catalog
 * functions, Genie spaces, and Vector Search indexes as MCP servers, hosted
 * by the platform. Anything an apx-agent declares as a Mosaic AI resource
 * becomes a Managed MCP endpoint automatically — there's no per-agent MCP
 * server to deploy, no app.yaml to maintain, no naming conventions for
 * discovery.
 *
 * This module provides two helpers:
 *
 *   - {@link managedMcpUrls} — takes an explicit array of declared
 *     `ResourceSpec`s and emits the Managed MCP URL for each resource,
 *     paired with the OAuth scope it needs.
 *
 *   - {@link managedMcpClientConfig} — produces the MCP server config
 *     snippet ready to drop into Claude Desktop, Cursor, or any client
 *     that speaks the standard `mcpServers` config shape.
 *
 * URL patterns (from the Databricks managed MCP docs):
 *   - UC function:   `/api/2.0/mcp/functions/{catalog}/{schema}/{function_name}`
 *   - Genie space:   `/api/2.0/mcp/genie/{genie_space_id}`
 *   - Vector Search: `/api/2.0/mcp/vector-search/{catalog}/{schema}/{index_name}`
 *
 * Resources outside those three kinds (serving endpoints, SQL warehouses,
 * UC tables) don't have a corresponding Managed MCP server today — those
 * are reported with `url=null` and `unsupported=true` so the caller can
 * decide how to surface them.
 */

import { z } from 'zod';
import type { ResourceKind, ResourceSpec } from './resources.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Re-export the canonical resource types from `./resources.js`. The strict
 * `ResourceKind` union and `ResourceSpec` interface live there; this module
 * re-exports them so existing callers importing from `managed-mcp.js`
 * continue to work.
 */
export type { ResourceKind, ResourceSpec } from './resources.js';

/**
 * A single Databricks Managed MCP endpoint.
 *
 * - `kind` — resource kind from the underlying `ResourceSpec`.
 * - `identifier` — the resource identifier (full UC name, space id, etc.).
 * - `url` — fully-qualified Managed MCP URL. `null` for unsupported kinds.
 * - `oauthScope` — OAuth scope a client must request. `null` for unsupported.
 * - `unsupported` — `true` when this kind has no Managed MCP equivalent.
 *
 * Instances are frozen so callers can rely on referential immutability.
 */
export interface ManagedMCPEndpoint {
  readonly kind: ResourceKind;
  readonly identifier: string;
  readonly url: string | null;
  readonly oauthScope: string | null;
  readonly unsupported: boolean;
}

// ---------------------------------------------------------------------------
// Mapping table
// ---------------------------------------------------------------------------

/**
 * Kind → (path template, oauth scope). The template uses `{0}`, `{1}`,
 * `{2}` placeholders filled from the dotted identifier (or just `{0}` for
 * single-part identifiers like genie spaces).
 */
const KIND_TO_PATH: Record<string, { template: string; scope: string }> = {
  uc_function: {
    template: '/api/2.0/mcp/functions/{0}/{1}/{2}',
    scope: 'unity-catalog',
  },
  genie_space: {
    template: '/api/2.0/mcp/genie/{0}',
    scope: 'genie',
  },
  vector_search_index: {
    template: '/api/2.0/mcp/vector-search/{0}/{1}/{2}',
    scope: 'vector-search',
  },
};

function fillTemplate(template: string, parts: string[]): string {
  return template.replace(/\{(\d+)\}/g, (_, idx) => parts[Number(idx)] ?? '');
}

function normaliseHost(workspaceHost: string): string {
  let host = workspaceHost.replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(host)) {
    host = `https://${host}`;
  }
  return host;
}

function buildUrl(
  kind: string,
  identifier: string,
  workspaceHost: string,
): { url: string | null; oauthScope: string | null } {
  const mapping = KIND_TO_PATH[kind];
  if (!mapping) {
    return { url: null, oauthScope: null };
  }

  if (kind === 'genie_space') {
    const path = fillTemplate(mapping.template, [identifier]);
    return { url: `${workspaceHost}${path}`, oauthScope: mapping.scope };
  }

  // UC function / vector search use three-part identifiers
  const parts = identifier.split('.');
  if (parts.length !== 3) {
    console.warn(
      `[managed-mcp] Managed MCP URL for ${kind} expects a three-part identifier; got ${JSON.stringify(identifier)} — skipping.`,
    );
    return { url: null, oauthScope: null };
  }
  const path = fillTemplate(mapping.template, parts);
  return { url: `${workspaceHost}${path}`, oauthScope: mapping.scope };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

const ManagedMcpUrlsOptionsSchema = z.object({
  resourceSpecs: z.array(
    z.object({
      kind: z.string(),
      identifier: z.string(),
    }),
  ),
  workspaceHost: z.string().min(1),
  model: z.string().optional(),
});

export interface ManagedMcpUrlsOptions {
  /** Resource specs declared by the agent (or assembled by the caller). */
  resourceSpecs: ResourceSpec[];
  /**
   * Databricks workspace host (e.g. `https://my-workspace.cloud.databricks.com`).
   * Scheme and trailing slash are optional — normalised internally.
   */
  workspaceHost: string;
  /**
   * Optional model serving endpoint name. When supplied, a
   * `{kind: 'serving_endpoint', identifier: model}` spec is prepended to
   * the input — mirrors the Python helper's `model=` kwarg.
   */
  model?: string;
}

/**
 * Return the Managed MCP endpoints for an explicit list of `ResourceSpec`s.
 *
 * Endpoints whose kind has no Managed MCP equivalent are returned with
 * `unsupported=true` and `url=null` so the caller can choose to filter or
 * surface them separately. Ordering preserves input order (with the model
 * spec prepended, when supplied).
 */
export function managedMcpUrls(options: ManagedMcpUrlsOptions): ManagedMCPEndpoint[] {
  const parsed = ManagedMcpUrlsOptionsSchema.parse(options);
  const host = normaliseHost(parsed.workspaceHost);

  // Use the loose shape internally — buildUrl tolerates unknown kinds and
  // returns null/null when no Managed MCP path applies, so unsupported
  // kinds flow through naturally.
  const specs: { kind: string; identifier: string }[] = parsed.model
    ? [{ kind: 'serving_endpoint', identifier: parsed.model }, ...parsed.resourceSpecs]
    : [...parsed.resourceSpecs];

  return specs.map((spec) => {
    const { url, oauthScope } = buildUrl(spec.kind, spec.identifier, host);
    if (url === null) {
      return Object.freeze({
        kind: spec.kind as ResourceKind,
        identifier: spec.identifier,
        url: null,
        oauthScope: null,
        unsupported: true,
      }) as ManagedMCPEndpoint;
    }
    return Object.freeze({
      kind: spec.kind as ResourceKind,
      identifier: spec.identifier,
      url,
      oauthScope,
      unsupported: false,
    }) as ManagedMCPEndpoint;
  });
}

const ManagedMcpClientConfigOptionsSchema = z.object({
  endpoints: z.array(
    z.object({
      kind: z.string(),
      identifier: z.string(),
      url: z.string().nullable(),
      oauthScope: z.string().nullable(),
      unsupported: z.boolean(),
    }),
  ),
  name: z.string().min(1).optional(),
  includeUnsupported: z.boolean().optional(),
});

export interface ManagedMcpClientConfigOptions {
  /** Output of {@link managedMcpUrls}. */
  endpoints: ManagedMCPEndpoint[];
  /**
   * Prefix applied to every server key. Defaults to `"databricks"`.
   * Override to namespace multiple agents in the same client config.
   */
  name?: string;
  /**
   * When `false` (default), endpoints with `unsupported=true` are skipped.
   * Set `true` to include them anyway (e.g. as a TODO list).
   */
  includeUnsupported?: boolean;
}

export interface McpServerEntry {
  type: 'http';
  url: string | null;
  oauth_scope?: string;
  unsupported?: boolean;
}

export interface ManagedMcpClientConfig {
  mcpServers: Record<string, McpServerEntry>;
}

/**
 * Produce an MCP client config snippet from a list of endpoints.
 *
 * Returns a dict in the standard `mcpServers` shape understood by Claude
 * Desktop, Cursor, and other MCP-aware clients. Each endpoint becomes one
 * server entry keyed `${name}.${kind}.${identifier}`.
 */
export function managedMcpClientConfig(
  options: ManagedMcpClientConfigOptions,
): ManagedMcpClientConfig {
  const parsed = ManagedMcpClientConfigOptionsSchema.parse(options);
  const name = parsed.name ?? 'databricks';
  const includeUnsupported = parsed.includeUnsupported ?? false;

  const servers: Record<string, McpServerEntry> = {};
  for (const ep of parsed.endpoints as ManagedMCPEndpoint[]) {
    if (ep.unsupported && !includeUnsupported) {
      continue;
    }
    const key = `${name}.${ep.kind}.${ep.identifier}`;
    const entry: McpServerEntry = { type: 'http', url: ep.url };
    if (ep.oauthScope) {
      entry.oauth_scope = ep.oauthScope;
    }
    if (ep.unsupported) {
      entry.unsupported = true;
    }
    servers[key] = entry;
  }
  return { mcpServers: servers };
}

/**
 * catalogTool, lineageTool, schemaTool — Unity Catalog tool factories.
 *
 * @example
 * import { catalogTool, lineageTool, schemaTool } from 'appkit-agent';
 *
 * createAgentPlugin({
 *   tools: [
 *     catalogTool('main', 'sales'),
 *     lineageTool(),
 *     schemaTool(),
 *   ],
 * });
 */

import { z } from 'zod';
import { defineTool } from './agent/tools.js';
import type { AgentTool } from './agent/tools.js';
import { resolveHost, dbFetch } from './connectors/types.js';
import { getRequestContext } from './agent/request-context.js';

// ---------------------------------------------------------------------------
// Shared auth helper (same pattern as genie.ts)
// ---------------------------------------------------------------------------

function resolveToken(oboHeaders?: Record<string, string>): string {
  if (oboHeaders) {
    const auth = oboHeaders['authorization'] ?? oboHeaders['Authorization'];
    if (auth?.startsWith('Bearer ')) return auth.slice(7);
  }
  const ctx = getRequestContext();
  if (ctx) {
    const token =
      ctx.oboHeaders['x-forwarded-access-token'] ||
      (ctx.oboHeaders['authorization'] ?? '').replace(/^Bearer\s+/i, '');
    if (token) return token;
  }
  const envToken = process.env.DATABRICKS_TOKEN;
  if (envToken) return envToken;
  throw new Error(
    'No Databricks token: pass oboHeaders, set DATABRICKS_TOKEN, or call from within a request context',
  );
}

// ---------------------------------------------------------------------------
// Shared options type
// ---------------------------------------------------------------------------

export interface CatalogToolOptions {
  /** Tool name shown to the LLM. */
  name?: string;
  /** Tool description shown to the LLM. */
  description?: string;
  /** Databricks workspace host. Falls back to DATABRICKS_HOST env var. */
  host?: string;
  /** OBO headers forwarded from the incoming request. Falls back to request context or DATABRICKS_TOKEN. */
  oboHeaders?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// UC API response types
// ---------------------------------------------------------------------------

interface UcTableInfo {
  name: string;
  full_name: string;
  table_type?: string;
  comment?: string;
}

interface UcTableListResponse {
  tables?: UcTableInfo[];
}

interface UcColumnInfo {
  name: string;
  type_name?: string;
  type_text?: string;
  comment?: string;
  nullable?: boolean;
  position?: number;
}

interface UcTableDetailResponse {
  columns?: UcColumnInfo[];
}

interface UcLineageEntry {
  tableInfo?: { name?: string; table_type?: string };
}

interface UcLineageResponse {
  upstreams?: UcLineageEntry[];
  downstreams?: UcLineageEntry[];
}

// ---------------------------------------------------------------------------
// catalogTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that lists tables in a Unity Catalog schema.
 *
 * The LLM calls this tool with no arguments — the catalog and schema are
 * baked in at construction time.
 *
 * @param catalog - UC catalog name.
 * @param schema  - Schema name within the catalog.
 * @param opts    - Optional name, description, host, and auth overrides.
 */
export function catalogTool(catalog: string, schema: string, opts: CatalogToolOptions = {}): AgentTool {
  const name = opts.name ?? 'list_tables';
  const description =
    opts.description ?? `List all tables in ${catalog}.${schema} with their names and descriptions.`;

  return defineTool({
    name,
    description,
    parameters: z.object({}),
    handler: async () => {
      const host = resolveHost(opts.host);
      const token = resolveToken(opts.oboHeaders);
      const data = await dbFetch<UcTableListResponse>(
        `${host}/api/2.1/unity-catalog/tables?catalog_name=${encodeURIComponent(catalog)}&schema_name=${encodeURIComponent(schema)}`,
        { token, method: 'GET' },
      );
      return (data.tables ?? []).map((t) => ({
        name: t.name,
        full_name: t.full_name,
        table_type: t.table_type ?? '',
        comment: t.comment ?? '',
      }));
    },
  });
}

// ---------------------------------------------------------------------------
// lineageTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that fetches upstream/downstream lineage for a UC table.
 *
 * The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
 *
 * @param opts - Optional name, description, host, and auth overrides.
 */
export function lineageTool(opts: CatalogToolOptions = {}): AgentTool {
  const name = opts.name ?? 'get_table_lineage';
  const description =
    opts.description ??
    'Get the upstream sources and downstream consumers for a Unity Catalog table. ' +
      'Pass the full table name as catalog.schema.table_name.';

  return defineTool({
    name,
    description,
    parameters: z.object({
      table_name: z.string().describe('Full table name: catalog.schema.table'),
    }),
    handler: async ({ table_name }) => {
      const host = resolveHost(opts.host);
      const token = resolveToken(opts.oboHeaders);
      const data = await dbFetch<UcLineageResponse>(
        `${host}/api/2.1/unity-catalog/lineage-tracking/table-lineage?table_name=${encodeURIComponent(table_name)}`,
        { token, method: 'GET' },
      );
      return {
        table: table_name,
        upstreams: (data.upstreams ?? [])
          .filter((u) => u.tableInfo?.name)
          .map((u) => ({ full_name: u.tableInfo!.name!, table_type: u.tableInfo!.table_type ?? '' })),
        downstreams: (data.downstreams ?? [])
          .filter((d) => d.tableInfo?.name)
          .map((d) => ({ full_name: d.tableInfo!.name!, table_type: d.tableInfo!.table_type ?? '' })),
      };
    },
  });
}

// ---------------------------------------------------------------------------
// schemaTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that describes the columns of a Unity Catalog table.
 *
 * The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
 *
 * @param opts - Optional name, description, host, and auth overrides.
 */
export function schemaTool(opts: CatalogToolOptions = {}): AgentTool {
  const name = opts.name ?? 'describe_table';
  const description =
    opts.description ??
    'Describe the columns of a Unity Catalog table — names, types, and descriptions. ' +
      'Pass the full table name as catalog.schema.table_name.';

  return defineTool({
    name,
    description,
    parameters: z.object({
      table_name: z.string().describe('Full table name: catalog.schema.table'),
    }),
    handler: async ({ table_name }) => {
      const host = resolveHost(opts.host);
      const token = resolveToken(opts.oboHeaders);
      const data = await dbFetch<UcTableDetailResponse>(
        `${host}/api/2.1/unity-catalog/tables/${encodeURIComponent(table_name)}`,
        { token, method: 'GET' },
      );
      return (data.columns ?? []).map((col) => ({
        name: col.name,
        type: col.type_name ?? '',
        type_text: col.type_text ?? '',
        comment: col.comment ?? '',
        nullable: col.nullable ?? true,
        position: col.position ?? 0,
      }));
    },
  });
}

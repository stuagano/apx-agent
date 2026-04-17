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
import { resolveHost, resolveToken, dbFetch } from './connectors/types.js';

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

export interface UcFunctionToolOptions extends CatalogToolOptions {
  /** SQL warehouse ID. Auto-discovered (prefers serverless) if not provided. */
  warehouseId?: string;
}

// ---------------------------------------------------------------------------
// SQL literal helper (used by ucFunctionTool)
// ---------------------------------------------------------------------------

function toSqlLiteral(value: unknown, typeName: string): string {
  if (value === null || value === undefined) return 'NULL';
  const t = typeName.toUpperCase();
  if (t === 'BOOLEAN') return value ? 'TRUE' : 'FALSE';
  if (['STRING', 'CHAR', 'VARCHAR', 'TEXT'].includes(t)) {
    const escaped = String(value).replace(/'/g, "''");
    return `'${escaped}'`;
  }
  // Numeric — validate, then pass raw
  const n = Number(value);
  if (!isNaN(n)) return String(value);
  // Fallback: quote as string
  const escaped = String(value).replace(/'/g, "''");
  return `'${escaped}'`;
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

// ---------------------------------------------------------------------------
// ucFunctionTool
// ---------------------------------------------------------------------------

interface UcFunctionParam {
  name: string;
  position: number;
  type_name: string;
}

interface UcFunctionDef {
  parameters: UcFunctionParam[];
  data_type: string;
}

interface SqlStatementResponse {
  status: { state: string; error?: { message?: string } };
  manifest?: { schema?: { columns?: Array<{ name: string }> } };
  result?: { data_array?: Array<Array<string | null>> };
}

interface WarehouseListResponse {
  warehouses?: Array<{ id: string; warehouse_type?: string }>;
}

async function resolveWarehouseId(host: string, token: string, warehouseId?: string): Promise<string> {
  if (warehouseId) return warehouseId;
  const data = await dbFetch<WarehouseListResponse>(`${host}/api/2.0/sql/warehouses`, { token, method: 'GET' });
  const warehouses = data.warehouses ?? [];
  // Prefer serverless
  const serverless = warehouses.find((w) => w.warehouse_type?.toLowerCase().includes('serverless'));
  const first = warehouses.find((w) => w.id);
  const id = (serverless ?? first)?.id;
  if (!id) throw new Error('No SQL warehouse available in this workspace');
  return id;
}

async function executeSqlStatement(
  host: string,
  token: string,
  warehouseId: string,
  statement: string,
): Promise<Array<Record<string, unknown>>> {
  const data = await dbFetch<SqlStatementResponse>(`${host}/api/2.0/sql/statements/`, {
    token,
    method: 'POST',
    body: { statement, warehouse_id: warehouseId, wait_timeout: '30s', disposition: 'INLINE', format: 'JSON_ARRAY' },
  });

  if (data.status.state !== 'SUCCEEDED') {
    throw new Error(`SQL failed: ${data.status.error?.message ?? data.status.state}`);
  }

  const cols = data.manifest?.schema?.columns ?? [];
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => Object.fromEntries(cols.map((col, i) => [col.name, row[i] ?? null])));
}

/**
 * Create a tool that executes a Unity Catalog function via SQL.
 *
 * The function definition is fetched from UC on the first call and cached —
 * parameter names, types, and order are derived automatically.
 *
 * @param functionName - Fully qualified UC function name: `catalog.schema.function`.
 * @param opts         - Optional overrides for name, description, host, warehouseId, and auth.
 *
 * @example
 * ucFunctionTool('main.tools.classify_intent', {
 *   description: 'Classify user intent. params: {text, min_confidence}',
 * })
 */
export function ucFunctionTool(functionName: string, opts: UcFunctionToolOptions = {}): AgentTool {
  const shortName = functionName.split('.').pop() ?? functionName;
  const name = opts.name ?? shortName;
  const description =
    opts.description ??
    `Execute the Unity Catalog function \`${functionName}\`. ` +
      `Pass parameters as a JSON object with parameter names as keys, e.g. {"param1": "value1", "param2": 42}.`;

  // Cached function definition — populated on first handler call
  let funcDef: UcFunctionDef | null = null;

  return defineTool({
    name,
    description,
    parameters: z.object({
      params: z
        .record(z.string(), z.unknown())
        .describe('Function parameters as {param_name: value} pairs'),
    }),
    handler: async ({ params }) => {
      const host = resolveHost(opts.host);
      const token = resolveToken(opts.oboHeaders);

      // Fetch and cache function definition on first call
      if (!funcDef) {
        const info = await dbFetch<any>(
          `${host}/api/2.1/unity-catalog/functions/${encodeURIComponent(functionName)}`,
          { token, method: 'GET' },
        );
        funcDef = {
          data_type: info.data_type ?? '',
          parameters: ((info.input_params?.parameters ?? []) as any[])
            .sort((a: any, b: any) => (a.position ?? 0) - (b.position ?? 0))
            .map((p: any) => ({
              name: p.name as string,
              position: (p.position ?? 0) as number,
              type_name: (p.type_name ?? 'STRING') as string,
            })),
        };
      }

      // Build SQL call with positional args
      const sqlArgs = funcDef.parameters.map((p) => toSqlLiteral(params[p.name], p.type_name));
      const sql =
        sqlArgs.length === 0
          ? `SELECT ${functionName}()`
          : `SELECT ${functionName}(${sqlArgs.join(', ')})`;

      const warehouseId = await resolveWarehouseId(host, token, opts.warehouseId);
      const rows = await executeSqlStatement(host, token, warehouseId, sql);

      // Scalar: unwrap single cell
      if (rows.length === 1 && Object.keys(rows[0]).length === 1) {
        return Object.values(rows[0])[0];
      }
      return rows;
    },
  });
}

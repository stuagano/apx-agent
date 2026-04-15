/**
 * Lakebase connector — typed tools for the SQL Statement Execution API.
 *
 * Provides three tool factories:
 *   - createLakebaseQueryTool       SELECT with parameterized filters
 *   - createLakebaseMutateTool      INSERT / UPDATE / DELETE
 *   - createLakebaseSchemaInspectTool  information_schema.columns query
 */

import { z } from 'zod';
import { defineTool } from '../agent/tools.js';
import { resolveHost, buildSqlParams, type ConnectorConfig, type SqlParam } from '../connectors/types.js';

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

interface SqlStatementResponse {
  statement_id: string;
  status: { state: string };
  manifest?: {
    schema?: {
      columns?: Array<{ name: string }>;
    };
  };
  result?: {
    data_array?: Array<Array<string | null>>;
  };
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Extract a Databricks token from OBO headers or the environment.
 */
function resolveToken(oboHeaders?: Record<string, string>): string {
  if (oboHeaders) {
    const auth = oboHeaders['authorization'] ?? oboHeaders['Authorization'];
    if (auth?.startsWith('Bearer ')) return auth.slice(7);
  }
  const envToken = process.env.DATABRICKS_TOKEN;
  if (envToken) return envToken;
  throw new Error('No Databricks token: pass OBO headers or set DATABRICKS_TOKEN env var');
}

/**
 * POST a SQL statement to the Databricks SQL Statement Execution API and
 * return the response.
 */
async function executeSql(
  host: string,
  token: string,
  catalog: string,
  schema: string,
  statement: string,
  params?: SqlParam[],
): Promise<SqlStatementResponse> {
  const url = `${host}/api/2.0/sql/statements/`;

  const body: Record<string, unknown> = {
    statement,
    catalog,
    schema,
    wait_timeout: '30s',
    on_wait_timeout: 'CANCEL',
    disposition: 'INLINE',
    format: 'JSON_ARRAY',
  };

  if (params && params.length > 0) {
    body.parameters = params.map((p) => ({
      name: p.name,
      value: p.value,
      type: p.type,
    }));
  }

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Databricks SQL API ${res.status}: ${text}`);
  }

  return res.json() as Promise<SqlStatementResponse>;
}

/**
 * Convert a SQL Statements API response (column names + data_array) into an
 * array of plain row objects.
 */
function rowsToObjects(response: SqlStatementResponse): Array<Record<string, unknown>> {
  const columns = response.manifest?.schema?.columns ?? [];
  const dataArray = response.result?.data_array ?? [];
  return dataArray.map((row) => {
    const obj: Record<string, unknown> = {};
    columns.forEach((col, i) => {
      obj[col.name] = row[i] ?? null;
    });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Tool factories
// ---------------------------------------------------------------------------

/**
 * Create a Lakebase query tool that executes SELECT statements with
 * optional parameterized filters.
 */
export function createLakebaseQueryTool(config: ConnectorConfig) {
  const host = resolveHost(config.host);
  const { catalog, schema } = config;

  return defineTool({
    name: 'lakebase_query',
    description: `Query rows from a table in the ${catalog}.${schema} schema using parameterized SELECT statements.`,
    parameters: z.object({
      table: z.string().describe('Table name (unqualified — catalog.schema are taken from config)'),
      columns: z.array(z.string()).optional().describe('Columns to select; defaults to *'),
      filters: z.record(z.string(), z.unknown()).optional().describe('Key-value filter pairs for WHERE clause'),
      limit: z.number().int().min(1).optional().describe('Maximum rows to return (default 100)'),
    }),
    handler: async ({ table, columns, filters, limit }) => {
      const token = resolveToken();
      const fqn = `${catalog}.${schema}.${table}`;
      const cols = columns && columns.length > 0 ? columns.join(', ') : '*';
      const effectiveLimit = limit ?? 100;

      const { clause, params } = filters && Object.keys(filters).length > 0
        ? buildSqlParams(filters as Record<string, unknown>)
        : { clause: '', params: [] };

      const where = clause ? ` WHERE ${clause}` : '';
      const statement = `SELECT ${cols} FROM ${fqn}${where} LIMIT ${effectiveLimit}`;

      const response = await executeSql(host, token, catalog, schema, statement, params);
      return rowsToObjects(response);
    },
  });
}

/**
 * Create a Lakebase mutate tool that executes INSERT, UPDATE, or DELETE
 * statements.
 */
export function createLakebaseMutateTool(config: ConnectorConfig) {
  const host = resolveHost(config.host);
  const { catalog, schema } = config;

  return defineTool({
    name: 'lakebase_mutate',
    description: `Insert, update, or delete rows in a table in the ${catalog}.${schema} schema.`,
    parameters: z.object({
      table: z.string().describe('Table name (unqualified)'),
      operation: z.enum(['INSERT', 'UPDATE', 'DELETE']).describe('DML operation to perform'),
      values: z.record(z.string(), z.unknown()).optional().describe('Column-value pairs for INSERT or UPDATE SET'),
      filters: z.record(z.string(), z.unknown()).optional().describe('Key-value filter pairs for WHERE clause (required for UPDATE and DELETE)'),
    }),
    handler: async ({ table, operation, values, filters }) => {
      const token = resolveToken();
      const fqn = `${catalog}.${schema}.${table}`;

      let statement: string;
      let params: SqlParam[] = [];

      if (operation === 'INSERT') {
        if (!values || Object.keys(values).length === 0) {
          throw new Error('INSERT requires values');
        }
        const cols = Object.keys(values).join(', ');
        const placeholders = Object.keys(values).map((k) => `:${k}`).join(', ');
        const { params: insertParams } = buildSqlParams(values as Record<string, unknown>);
        params = insertParams;
        statement = `INSERT INTO ${fqn} (${cols}) VALUES (${placeholders})`;
      } else if (operation === 'UPDATE') {
        if (!values || Object.keys(values).length === 0) {
          throw new Error('UPDATE requires values');
        }
        if (!filters || Object.keys(filters).length === 0) {
          throw new Error('UPDATE requires filters to avoid updating all rows');
        }

        // Prefix set params with "set_" to avoid collisions with filter params
        const setCols = Object.keys(values).map((k) => `${k} = :set_${k}`).join(', ');
        const setPrefixed: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(values)) {
          setPrefixed[`set_${k}`] = v;
        }
        const { params: setParams } = buildSqlParams(setPrefixed);
        const { clause: whereClause, params: filterParams } = buildSqlParams(filters as Record<string, unknown>);

        params = [...setParams, ...filterParams];
        statement = `UPDATE ${fqn} SET ${setCols} WHERE ${whereClause}`;
      } else {
        // DELETE
        if (!filters || Object.keys(filters).length === 0) {
          throw new Error('DELETE requires filters to avoid deleting all rows');
        }
        const { clause: whereClause, params: filterParams } = buildSqlParams(filters as Record<string, unknown>);
        params = filterParams;
        statement = `DELETE FROM ${fqn} WHERE ${whereClause}`;
      }

      const response = await executeSql(host, token, catalog, schema, statement, params);
      return { success: true, statement_id: response.statement_id };
    },
  });
}

/**
 * Create a Lakebase schema inspect tool that queries information_schema.columns
 * for the configured catalog.schema.
 */
export function createLakebaseSchemaInspectTool(config: ConnectorConfig) {
  const host = resolveHost(config.host);
  const { catalog, schema } = config;

  return defineTool({
    name: 'lakebase_schema_inspect',
    description: `Inspect column definitions in ${catalog}.${schema} via information_schema.columns.`,
    parameters: z.object({
      table_filter: z.string().optional().describe('Optional table name to filter results to a single table'),
    }),
    handler: async ({ table_filter }) => {
      const token = resolveToken();

      const params: SqlParam[] = [
        { name: 'cat', value: catalog, type: 'STRING' },
        { name: 'sch', value: schema, type: 'STRING' },
      ];

      let statement = `SELECT * FROM information_schema.columns WHERE table_catalog = :cat AND table_schema = :sch`;

      if (table_filter) {
        params.push({ name: 'tbl', value: table_filter, type: 'STRING' });
        statement += ` AND table_name = :tbl`;
      }

      statement += ` ORDER BY table_name, ordinal_position`;

      const response = await executeSql(host, token, catalog, schema, statement, params);
      return rowsToObjects(response);
    },
  });
}

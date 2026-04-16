/**
 * Agent tools — define your tools here.
 *
 * Each tool has a Zod schema for type-safe parameters and an async handler.
 * Tools are registered with the FMAPI runner and exposed via the /responses
 * endpoint, individual tool endpoints, and the A2A discovery card.
 *
 * Add your domain-specific tools below. The examples show common patterns
 * for Databricks workspace operations.
 */

import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { runSql } from './databricks.js';
import type { AgentTool } from './fmapi.js';

// ---------------------------------------------------------------------------
// Helper: define a tool with Zod schema
// ---------------------------------------------------------------------------

function defineTool<T extends z.ZodType>(opts: {
  name: string;
  description: string;
  parameters: T;
  handler: (args: z.infer<T>) => Promise<unknown>;
}): AgentTool {
  const params = zodToJsonSchema(opts.parameters as any) as Record<string, unknown>;
  delete params['$schema'];
  return {
    name: opts.name,
    description: opts.description,
    parameters: params,
    handler: async (raw: unknown) => {
      const parsed = opts.parameters.parse(raw);
      return opts.handler(parsed);
    },
  };
}

// ---------------------------------------------------------------------------
// Example tools — replace/extend with your domain tools
// ---------------------------------------------------------------------------

export const runSqlQuery = defineTool({
  name: 'run_sql_query',
  description: 'Execute a read-only SQL query against any Databricks table.',
  parameters: z.object({
    sql: z.string().describe('A SELECT query (read-only)'),
  }),
  handler: async ({ sql }) => {
    const rows = await runSql(sql);
    return { row_count: rows.length, rows: rows.slice(0, 50) };
  },
});

export const getTableInfo = defineTool({
  name: 'get_table_info',
  description: 'Get schema, row count, and freshness for a Unity Catalog table.',
  parameters: z.object({
    table_full_name: z.string().describe('Fully qualified table name (catalog.schema.table)'),
  }),
  handler: async ({ table_full_name }) => {
    let schemaRows: Array<Record<string, any>>;
    try {
      schemaRows = await runSql(`DESCRIBE TABLE ${table_full_name}`);
    } catch (e) {
      return { error: `Table not found or not accessible: ${e}` };
    }
    let rowCount: string | number = 'unknown';
    try {
      const countRows = await runSql(`SELECT COUNT(*) as cnt FROM ${table_full_name}`);
      rowCount = countRows[0]?.cnt ?? 'unknown';
    } catch { /* row count unavailable */ }
    return { table: table_full_name, row_count: rowCount, columns: schemaRows.slice(0, 30) };
  },
});

// ---------------------------------------------------------------------------
// Export all tools
// ---------------------------------------------------------------------------

export const ALL_TOOLS: AgentTool[] = [
  runSqlQuery,
  getTableInfo,
  // Add your tools here
];

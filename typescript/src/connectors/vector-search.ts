/**
 * Vector Search connector tools.
 *
 * Provides three agent tools backed by the Databricks Vector Search REST API:
 *  - vs_query   — similarity search over an index
 *  - vs_upsert  — add or update a vector record
 *  - vs_delete  — delete records by primary key
 */

import { z } from 'zod';
import { defineTool, type AgentTool } from '../agent/tools.js';
import { resolveHost, dbFetch, type ConnectorConfig } from '../connectors/types.js';
import { getRequestContext } from '../agent/request-context.js';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function resolveToken(): string {
  const ctx = getRequestContext();
  if (ctx) {
    const token =
      ctx.oboHeaders['x-forwarded-access-token'] ||
      (ctx.oboHeaders['authorization'] ?? '').replace(/^Bearer\s+/i, '');
    if (token) return token;
  }
  return process.env.DATABRICKS_TOKEN ?? '';
}

// ---------------------------------------------------------------------------
// VS Query Tool
// ---------------------------------------------------------------------------

/**
 * Create a similarity-search tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
export function createVSQueryTool(config: ConnectorConfig): AgentTool {
  if (!config.vectorSearchIndex) {
    throw new Error('vectorSearchIndex is required in ConnectorConfig for createVSQueryTool');
  }

  const indexName = config.vectorSearchIndex;

  return defineTool({
    name: 'vs_query',
    description: 'Run a similarity search against a Databricks Vector Search index.',
    parameters: z.object({
      query_text: z.string().describe('The text to search for'),
      filters: z.record(z.string(), z.unknown()).optional().describe('Optional key-value filters'),
      num_results: z.number().int().min(1).optional().describe('Number of results to return (default 10)'),
    }),
    handler: async ({ query_text, filters, num_results }) => {
      const host = resolveHost(config.host);
      const token = resolveToken();

      const body: Record<string, unknown> = {
        query_text,
        num_results: num_results ?? 10,
        columns: [],
      };

      if (filters !== undefined) {
        body.filters_json = JSON.stringify(filters);
      }

      const response = await dbFetch<{
        manifest: { column_count: number; columns: Array<{ name: string }> };
        result: { row_count: number; data_array: unknown[][] };
      }>(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/query`, {
        token,
        method: 'POST',
        body,
      });

      const columnNames = response.manifest.columns.map((c) => c.name);
      const rows = response.result.data_array.map((row) => {
        const obj: Record<string, unknown> = {};
        columnNames.forEach((col, i) => {
          obj[col] = row[i];
        });
        return obj;
      });

      return rows;
    },
  });
}

// ---------------------------------------------------------------------------
// VS Upsert Tool
// ---------------------------------------------------------------------------

/**
 * Create an upsert tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
export function createVSUpsertTool(config: ConnectorConfig): AgentTool {
  if (!config.vectorSearchIndex) {
    throw new Error('vectorSearchIndex is required in ConnectorConfig for createVSUpsertTool');
  }

  const indexName = config.vectorSearchIndex;

  return defineTool({
    name: 'vs_upsert',
    description: 'Add or update a vector record in a Databricks Vector Search index.',
    parameters: z.object({
      id: z.string().describe('Primary key for the record'),
      text: z.string().describe('Text content for the vector embedding'),
      metadata: z.record(z.string(), z.unknown()).optional().describe('Optional additional metadata fields'),
    }),
    handler: async ({ id, text, metadata }) => {
      const host = resolveHost(config.host);
      const token = resolveToken();

      const record: Record<string, unknown> = { id, text, ...metadata };

      await dbFetch(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/upsert-data`, {
        token,
        method: 'POST',
        body: { inputs_json: JSON.stringify([record]) },
      });

      return { success: true, id };
    },
  });
}

// ---------------------------------------------------------------------------
// VS Delete Tool
// ---------------------------------------------------------------------------

/**
 * Create a delete tool for a Vector Search index.
 * Requires `config.vectorSearchIndex` to be set.
 */
export function createVSDeleteTool(config: ConnectorConfig): AgentTool {
  if (!config.vectorSearchIndex) {
    throw new Error('vectorSearchIndex is required in ConnectorConfig for createVSDeleteTool');
  }

  const indexName = config.vectorSearchIndex;

  return defineTool({
    name: 'vs_delete',
    description: 'Delete records from a Databricks Vector Search index by primary key.',
    parameters: z.object({
      ids: z.array(z.string()).describe('List of primary key values to delete'),
    }),
    handler: async ({ ids }) => {
      const host = resolveHost(config.host);
      const token = resolveToken();

      await dbFetch(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/delete-data`, {
        token,
        method: 'POST',
        body: { primary_keys: ids },
      });

      return { success: true, deleted: ids.length };
    },
  });
}

/**
 * Tests for the Lakebase connector tools.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { z } from 'zod';
import {
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
} from '../src/connectors/lakebase.js';
import type { ConnectorConfig } from '../src/connectors/types.js';

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
};

/** Build a minimal successful SQL Statements API response. */
function makeSqlResponse(
  columns: string[],
  rows: Array<Array<string | null>>,
  statementId = 'stmt-1',
) {
  return {
    statement_id: statementId,
    status: { state: 'SUCCEEDED' },
    manifest: {
      schema: {
        columns: columns.map((name) => ({ name })),
      },
    },
    result: {
      data_array: rows,
    },
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMockFetch(responseBody: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => responseBody,
    text: async () => JSON.stringify(responseBody),
  });
}

// ---------------------------------------------------------------------------
// createLakebaseQueryTool
// ---------------------------------------------------------------------------

describe('createLakebaseQueryTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has the expected name and description', () => {
    const tool = createLakebaseQueryTool(baseConfig);
    expect(tool.name).toBe('lakebase_query');
    expect(tool.description).toContain('main.kg');
  });

  it('Zod schema accepts valid inputs', () => {
    const tool = createLakebaseQueryTool(baseConfig);
    const schema = tool.parameters as z.ZodType;

    expect(() =>
      schema.parse({ table: 'experts' }),
    ).not.toThrow();

    expect(() =>
      schema.parse({
        table: 'experts',
        columns: ['id', 'name'],
        filters: { active: true },
        limit: 50,
      }),
    ).not.toThrow();
  });

  it('Zod schema rejects missing required table', () => {
    const tool = createLakebaseQueryTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() => schema.parse({})).toThrow();
  });

  it('builds SELECT * with default limit when no columns/filters given', async () => {
    const response = makeSqlResponse(['id', 'name'], [['1', 'Alice']]);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts' });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toBe('SELECT * FROM main.kg.experts LIMIT 100');
    expect(body.parameters).toBeUndefined();
  });

  it('builds SELECT with specific columns', async () => {
    const response = makeSqlResponse(['id', 'name'], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts', columns: ['id', 'name'] });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toBe('SELECT id, name FROM main.kg.experts LIMIT 100');
  });

  it('builds SELECT with WHERE clause from filters', async () => {
    const response = makeSqlResponse(['id'], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts', filters: { status: 'active' } });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('WHERE status = :status');
    expect(body.parameters).toEqual([{ name: 'status', value: 'active', type: 'STRING' }]);
  });

  it('respects a custom limit', async () => {
    const response = makeSqlResponse(['id'], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts', limit: 25 });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('LIMIT 25');
  });

  it('converts response rows to objects using column names', async () => {
    const response = makeSqlResponse(
      ['id', 'name', 'score'],
      [
        ['1', 'Alice', '0.9'],
        ['2', 'Bob', '0.7'],
      ],
    );
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    const result = (await tool.handler({ table: 'experts' })) as Array<Record<string, unknown>>;

    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ id: '1', name: 'Alice', score: '0.9' });
    expect(result[1]).toEqual({ id: '2', name: 'Bob', score: '0.7' });
  });

  it('returns empty array when result has no rows', async () => {
    const response = makeSqlResponse(['id'], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    const result = await tool.handler({ table: 'experts' });
    expect(result).toEqual([]);
  });

  it('uses the correct API endpoint and method', async () => {
    const response = makeSqlResponse([], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts' });

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe('https://test-host.databricks.com/api/2.0/sql/statements/');
    expect(opts.method).toBe('POST');
  });

  it('sends the correct catalog and schema in the request body', async () => {
    const response = makeSqlResponse([], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts' });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.catalog).toBe('main');
    expect(body.schema).toBe('kg');
  });
});

// ---------------------------------------------------------------------------
// createLakebaseMutateTool
// ---------------------------------------------------------------------------

describe('createLakebaseMutateTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has the expected name and description', () => {
    const tool = createLakebaseMutateTool(baseConfig);
    expect(tool.name).toBe('lakebase_mutate');
    expect(tool.description).toContain('main.kg');
  });

  it('Zod schema accepts INSERT with values', () => {
    const tool = createLakebaseMutateTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() =>
      schema.parse({ table: 'experts', operation: 'INSERT', values: { name: 'Alice' } }),
    ).not.toThrow();
  });

  it('Zod schema accepts UPDATE with values and filters', () => {
    const tool = createLakebaseMutateTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() =>
      schema.parse({
        table: 'experts',
        operation: 'UPDATE',
        values: { status: 'active' },
        filters: { id: '1' },
      }),
    ).not.toThrow();
  });

  it('Zod schema accepts DELETE with filters', () => {
    const tool = createLakebaseMutateTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() =>
      schema.parse({ table: 'experts', operation: 'DELETE', filters: { id: '1' } }),
    ).not.toThrow();
  });

  it('builds correct INSERT statement', async () => {
    const mockFetch = makeMockFetch({ statement_id: 'stmt-2', status: { state: 'SUCCEEDED' } });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'experts',
      operation: 'INSERT',
      values: { name: 'Alice', score: 0.9 },
    });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('INSERT INTO main.kg.experts');
    expect(body.statement).toContain('(name, score)');
    expect(body.statement).toContain('(:name, :score)');
  });

  it('builds correct UPDATE statement with prefixed set params', async () => {
    const mockFetch = makeMockFetch({ statement_id: 'stmt-3', status: { state: 'SUCCEEDED' } });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'experts',
      operation: 'UPDATE',
      values: { status: 'active' },
      filters: { id: '42' },
    });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('UPDATE main.kg.experts SET status = :set_status WHERE id = :id');
    const paramNames = body.parameters.map((p: { name: string }) => p.name);
    expect(paramNames).toContain('set_status');
    expect(paramNames).toContain('id');
  });

  it('builds correct DELETE statement', async () => {
    const mockFetch = makeMockFetch({ statement_id: 'stmt-4', status: { state: 'SUCCEEDED' } });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'experts',
      operation: 'DELETE',
      filters: { id: '99' },
    });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toBe('DELETE FROM main.kg.experts WHERE id = :id');
  });

  it('returns success and statement_id', async () => {
    const mockFetch = makeMockFetch({ statement_id: 'stmt-5', status: { state: 'SUCCEEDED' } });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseMutateTool(baseConfig);
    const result = await tool.handler({
      table: 'experts',
      operation: 'DELETE',
      filters: { id: '1' },
    });

    expect(result).toEqual({ success: true, statement_id: 'stmt-5' });
  });

  it('throws when DELETE is called without filters (safety check)', async () => {
    const tool = createLakebaseMutateTool(baseConfig);
    await expect(
      tool.handler({ table: 'experts', operation: 'DELETE' }),
    ).rejects.toThrow('DELETE requires filters');
  });

  it('throws when DELETE is called with empty filters (safety check)', async () => {
    const tool = createLakebaseMutateTool(baseConfig);
    await expect(
      tool.handler({ table: 'experts', operation: 'DELETE', filters: {} }),
    ).rejects.toThrow('DELETE requires filters');
  });

  it('throws when UPDATE is called without filters', async () => {
    const tool = createLakebaseMutateTool(baseConfig);
    await expect(
      tool.handler({
        table: 'experts',
        operation: 'UPDATE',
        values: { name: 'Bob' },
      }),
    ).rejects.toThrow('UPDATE requires filters');
  });

  it('throws when INSERT is called without values', async () => {
    const tool = createLakebaseMutateTool(baseConfig);
    await expect(
      tool.handler({ table: 'experts', operation: 'INSERT' }),
    ).rejects.toThrow('INSERT requires values');
  });
});

// ---------------------------------------------------------------------------
// createLakebaseSchemaInspectTool
// ---------------------------------------------------------------------------

describe('createLakebaseSchemaInspectTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has the expected name and description', () => {
    const tool = createLakebaseSchemaInspectTool(baseConfig);
    expect(tool.name).toBe('lakebase_schema_inspect');
    expect(tool.description).toContain('main.kg');
  });

  it('Zod schema accepts empty input', () => {
    const tool = createLakebaseSchemaInspectTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() => schema.parse({})).not.toThrow();
  });

  it('Zod schema accepts optional table_filter', () => {
    const tool = createLakebaseSchemaInspectTool(baseConfig);
    const schema = tool.parameters as z.ZodType;
    expect(() => schema.parse({ table_filter: 'experts' })).not.toThrow();
  });

  it('queries information_schema.columns for the configured catalog and schema', async () => {
    const response = makeSqlResponse(
      ['table_name', 'column_name', 'data_type'],
      [['experts', 'id', 'STRING']],
    );
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseSchemaInspectTool(baseConfig);
    await tool.handler({});

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('information_schema.columns');
    expect(body.statement).toContain(':cat');
    expect(body.statement).toContain(':sch');

    const paramNames = body.parameters.map((p: { name: string }) => p.name);
    expect(paramNames).toContain('cat');
    expect(paramNames).toContain('sch');

    const catParam = body.parameters.find((p: { name: string }) => p.name === 'cat');
    const schParam = body.parameters.find((p: { name: string }) => p.name === 'sch');
    expect(catParam.value).toBe('main');
    expect(schParam.value).toBe('kg');
  });

  it('adds table_name filter when table_filter is provided', async () => {
    const response = makeSqlResponse(
      ['table_name', 'column_name'],
      [['experts', 'id']],
    );
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseSchemaInspectTool(baseConfig);
    await tool.handler({ table_filter: 'experts' });

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('table_name = :tbl');
    const tblParam = body.parameters.find((p: { name: string }) => p.name === 'tbl');
    expect(tblParam.value).toBe('experts');
  });

  it('does NOT add table_name filter when table_filter is absent', async () => {
    const response = makeSqlResponse(['table_name'], []);
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseSchemaInspectTool(baseConfig);
    await tool.handler({});

    const [, opts] = mockFetch.mock.calls[0];
    const body = JSON.parse(opts.body);
    expect(body.statement).not.toContain('table_name = :tbl');
  });

  it('returns row objects from the response', async () => {
    const response = makeSqlResponse(
      ['table_name', 'column_name', 'data_type'],
      [
        ['experts', 'id', 'STRING'],
        ['experts', 'name', 'STRING'],
      ],
    );
    const mockFetch = makeMockFetch(response);
    vi.stubGlobal('fetch', mockFetch);

    const tool = createLakebaseSchemaInspectTool(baseConfig);
    const result = (await tool.handler({})) as Array<Record<string, unknown>>;

    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ table_name: 'experts', column_name: 'id', data_type: 'STRING' });
    expect(result[1]).toEqual({ table_name: 'experts', column_name: 'name', data_type: 'STRING' });
  });
});

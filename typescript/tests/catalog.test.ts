/**
 * Tests for catalogTool(), lineageTool(), and schemaTool() factories.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { catalogTool, lineageTool, schemaTool } from '../src/catalog.js';
import { runWithContext } from '../src/agent/request-context.js';

// ---------------------------------------------------------------------------
// Mock fetch helper
// ---------------------------------------------------------------------------

function makeMockFetch(body: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 403,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
}

const OBO = { Authorization: 'Bearer test-token' };
const HOST = 'https://test.databricks.com';

// ---------------------------------------------------------------------------
// catalogTool
// ---------------------------------------------------------------------------

describe('catalogTool', () => {
  it('returns tool with default name', () => {
    const tool = catalogTool('main', 'sales', { host: HOST, oboHeaders: OBO });
    expect(tool.name).toBe('list_tables');
  });

  it('accepts custom name', () => {
    const tool = catalogTool('main', 'sales', { name: 'list_sales', host: HOST, oboHeaders: OBO });
    expect(tool.name).toBe('list_sales');
  });

  it('includes catalog and schema in default description', () => {
    const tool = catalogTool('mycat', 'myschema', { host: HOST, oboHeaders: OBO });
    expect(tool.description).toContain('mycat');
    expect(tool.description).toContain('myschema');
  });

  it('returns table list from API', async () => {
    global.fetch = makeMockFetch({
      tables: [
        { name: 'orders', full_name: 'main.sales.orders', table_type: 'MANAGED', comment: 'Order records' },
        { name: 'customers', full_name: 'main.sales.customers', table_type: 'MANAGED', comment: '' },
      ],
    });

    const tool = catalogTool('main', 'sales', { host: HOST, oboHeaders: OBO });
    const result = await tool.handler({}) as any[];
    expect(result).toHaveLength(2);
    expect(result[0].full_name).toBe('main.sales.orders');
    expect(result[0].comment).toBe('Order records');
  });

  it('calls correct UC tables endpoint', async () => {
    global.fetch = makeMockFetch({ tables: [] });

    const tool = catalogTool('mycat', 'myschema', { host: HOST, oboHeaders: OBO });
    await tool.handler({});

    const url = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain('/api/2.1/unity-catalog/tables');
    expect(url).toContain('catalog_name=mycat');
    expect(url).toContain('schema_name=myschema');
  });

  it('returns empty array when no tables', async () => {
    global.fetch = makeMockFetch({});
    const tool = catalogTool('main', 'sales', { host: HOST, oboHeaders: OBO });
    const result = await tool.handler({});
    expect(result).toEqual([]);
  });

  it('uses request context token automatically', async () => {
    global.fetch = makeMockFetch({ tables: [] });
    vi.stubEnv('DATABRICKS_HOST', HOST);

    const tool = catalogTool('main', 'sales');
    await runWithContext(
      { oboHeaders: { 'x-forwarded-access-token': 'ctx-token' } },
      () => tool.handler({}),
    );

    const call = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers.Authorization).toBe('Bearer ctx-token');
  });
});

// ---------------------------------------------------------------------------
// lineageTool
// ---------------------------------------------------------------------------

describe('lineageTool', () => {
  it('returns tool with default name', () => {
    expect(lineageTool({ host: HOST, oboHeaders: OBO }).name).toBe('get_table_lineage');
  });

  it('accepts custom name', () => {
    expect(lineageTool({ name: 'my_lineage', host: HOST, oboHeaders: OBO }).name).toBe('my_lineage');
  });

  it('returns upstreams and downstreams', async () => {
    global.fetch = makeMockFetch({
      upstreams: [{ tableInfo: { name: 'main.raw.events', table_type: 'MANAGED' } }],
      downstreams: [{ tableInfo: { name: 'main.gold.summary', table_type: 'MANAGED' } }],
    });

    const tool = lineageTool({ host: HOST, oboHeaders: OBO });
    const result = await tool.handler({ table_name: 'main.silver.cleaned' }) as any;
    expect(result.table).toBe('main.silver.cleaned');
    expect(result.upstreams[0].full_name).toBe('main.raw.events');
    expect(result.downstreams[0].full_name).toBe('main.gold.summary');
  });

  it('calls correct lineage endpoint with table_name', async () => {
    global.fetch = makeMockFetch({ upstreams: [], downstreams: [] });

    const tool = lineageTool({ host: HOST, oboHeaders: OBO });
    await tool.handler({ table_name: 'main.sales.orders' });

    const url = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain('/api/2.1/unity-catalog/lineage-tracking/table-lineage');
    expect(url).toContain(encodeURIComponent('main.sales.orders'));
  });

  it('handles empty lineage response', async () => {
    global.fetch = makeMockFetch({});
    const tool = lineageTool({ host: HOST, oboHeaders: OBO });
    const result = await tool.handler({ table_name: 'main.t' }) as any;
    expect(result.upstreams).toEqual([]);
    expect(result.downstreams).toEqual([]);
  });

  it('filters entries without tableInfo', async () => {
    global.fetch = makeMockFetch({
      upstreams: [
        { notebookInfos: [{ notebook_id: 123 }] },
        { tableInfo: { name: 'main.raw.x', table_type: 'MANAGED' } },
      ],
      downstreams: [],
    });

    const tool = lineageTool({ host: HOST, oboHeaders: OBO });
    const result = await tool.handler({ table_name: 'main.t' }) as any;
    expect(result.upstreams).toHaveLength(1);
    expect(result.upstreams[0].full_name).toBe('main.raw.x');
  });
});

// ---------------------------------------------------------------------------
// schemaTool
// ---------------------------------------------------------------------------

describe('schemaTool', () => {
  it('returns tool with default name', () => {
    expect(schemaTool({ host: HOST, oboHeaders: OBO }).name).toBe('describe_table');
  });

  it('accepts custom name', () => {
    expect(schemaTool({ name: 'inspect', host: HOST, oboHeaders: OBO }).name).toBe('inspect');
  });

  it('returns column list', async () => {
    global.fetch = makeMockFetch({
      columns: [
        { name: 'id', type_name: 'LONG', type_text: 'BIGINT', comment: 'Primary key', nullable: false, position: 0 },
        { name: 'email', type_name: 'STRING', type_text: 'STRING', comment: '', nullable: true, position: 1 },
      ],
    });

    const tool = schemaTool({ host: HOST, oboHeaders: OBO });
    const result = await tool.handler({ table_name: 'main.sales.customers' }) as any[];
    expect(result).toHaveLength(2);
    expect(result[0].name).toBe('id');
    expect(result[0].type).toBe('LONG');
    expect(result[0].nullable).toBe(false);
    expect(result[1].name).toBe('email');
  });

  it('calls correct UC tables endpoint with full table name', async () => {
    global.fetch = makeMockFetch({ columns: [] });

    const tool = schemaTool({ host: HOST, oboHeaders: OBO });
    await tool.handler({ table_name: 'main.sales.orders' });

    const url = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain('/api/2.1/unity-catalog/tables/');
    expect(url).toContain(encodeURIComponent('main.sales.orders'));
  });

  it('returns empty array when no columns', async () => {
    global.fetch = makeMockFetch({});
    const tool = schemaTool({ host: HOST, oboHeaders: OBO });
    const result = await tool.handler({ table_name: 'main.t' });
    expect(result).toEqual([]);
  });

  it('uses request context token automatically', async () => {
    global.fetch = makeMockFetch({ columns: [] });
    vi.stubEnv('DATABRICKS_HOST', HOST);

    const tool = schemaTool();
    await runWithContext(
      { oboHeaders: { 'x-forwarded-access-token': 'ctx-token' } },
      () => tool.handler({ table_name: 'main.t' }),
    );

    const call = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers.Authorization).toBe('Bearer ctx-token');
  });
});

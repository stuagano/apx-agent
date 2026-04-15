import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createVSQueryTool, createVSUpsertTool, createVSDeleteTool } from '../src/connectors/vector-search.js';
import type { ConnectorConfig } from '../src/connectors/types.js';

// ---------------------------------------------------------------------------
// Shared config and mock helpers
// ---------------------------------------------------------------------------

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
  vectorSearchIndex: 'main.kg.experts_vs_index',
};

const configWithoutIndex: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
};

function makeVSQueryResponse(rows: unknown[][] = []) {
  return {
    manifest: {
      column_count: 3,
      columns: [{ name: 'id' }, { name: 'text' }, { name: 'score' }],
    },
    result: {
      row_count: rows.length,
      data_array: rows,
    },
  };
}

function mockFetchOk(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => body,
  });
}

// ---------------------------------------------------------------------------
// createVSQueryTool
// ---------------------------------------------------------------------------

describe('createVSQueryTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has name vs_query', () => {
    const tool = createVSQueryTool(baseConfig);
    expect(tool.name).toBe('vs_query');
  });

  it('throws when vectorSearchIndex is not configured', () => {
    expect(() => createVSQueryTool(configWithoutIndex)).toThrow('vectorSearchIndex is required');
  });

  it('calls the correct query endpoint with query_text and default num_results', async () => {
    const mockFetch = mockFetchOk(makeVSQueryResponse([['e1', 'Expert text', 0.95]]));
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    await tool.handler({ query_text: 'machine learning expert' });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain('/api/2.0/vector-search/indexes/');
    expect(url).toContain('main.kg.experts_vs_index');
    expect(url).toContain('/query');

    const sentBody = JSON.parse(opts.body);
    expect(sentBody.query_text).toBe('machine learning expert');
    expect(sentBody.num_results).toBe(10);
  });

  it('sends custom num_results when provided', async () => {
    const mockFetch = mockFetchOk(makeVSQueryResponse([]));
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    await tool.handler({ query_text: 'test', num_results: 5 });

    const [, opts] = mockFetch.mock.calls[0];
    const sentBody = JSON.parse(opts.body);
    expect(sentBody.num_results).toBe(5);
  });

  it('passes filters_json when filters provided', async () => {
    const mockFetch = mockFetchOk(makeVSQueryResponse([]));
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    await tool.handler({ query_text: 'test', filters: { region: 'us-east' } });

    const [, opts] = mockFetch.mock.calls[0];
    const sentBody = JSON.parse(opts.body);
    expect(sentBody.filters_json).toBe(JSON.stringify({ region: 'us-east' }));
  });

  it('does not include filters_json when filters not provided', async () => {
    const mockFetch = mockFetchOk(makeVSQueryResponse([]));
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    await tool.handler({ query_text: 'test' });

    const [, opts] = mockFetch.mock.calls[0];
    const sentBody = JSON.parse(opts.body);
    expect(sentBody).not.toHaveProperty('filters_json');
  });

  it('formats response rows as objects keyed by column name', async () => {
    const mockFetch = mockFetchOk(
      makeVSQueryResponse([
        ['e1', 'Alice is a data scientist', 0.95],
        ['e2', 'Bob works on ML pipelines', 0.82],
      ]),
    );
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    const result = (await tool.handler({ query_text: 'data scientist' })) as Array<Record<string, unknown>>;

    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ id: 'e1', text: 'Alice is a data scientist', score: 0.95 });
    expect(result[1]).toEqual({ id: 'e2', text: 'Bob works on ML pipelines', score: 0.82 });
  });

  it('returns empty array when no results', async () => {
    const mockFetch = mockFetchOk(makeVSQueryResponse([]));
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSQueryTool(baseConfig);
    const result = await tool.handler({ query_text: 'nothing matches' });

    expect(result).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// createVSUpsertTool
// ---------------------------------------------------------------------------

describe('createVSUpsertTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has name vs_upsert', () => {
    const tool = createVSUpsertTool(baseConfig);
    expect(tool.name).toBe('vs_upsert');
  });

  it('throws when vectorSearchIndex is not configured', () => {
    expect(() => createVSUpsertTool(configWithoutIndex)).toThrow('vectorSearchIndex is required');
  });

  it('calls the correct upsert endpoint and sends inputs_json', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSUpsertTool(baseConfig);
    await tool.handler({ id: 'e42', text: 'Expert in distributed systems' });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain('/upsert-data');

    const sentBody = JSON.parse(opts.body);
    expect(sentBody).toHaveProperty('inputs_json');

    const inputs = JSON.parse(sentBody.inputs_json);
    expect(inputs).toHaveLength(1);
    expect(inputs[0].id).toBe('e42');
    expect(inputs[0].text).toBe('Expert in distributed systems');
  });

  it('includes metadata fields in inputs_json when provided', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSUpsertTool(baseConfig);
    await tool.handler({ id: 'e1', text: 'Alice', metadata: { region: 'us-west', tier: 'senior' } });

    const [, opts] = mockFetch.mock.calls[0];
    const sentBody = JSON.parse(opts.body);
    const inputs = JSON.parse(sentBody.inputs_json);

    expect(inputs[0]).toMatchObject({ id: 'e1', text: 'Alice', region: 'us-west', tier: 'senior' });
  });

  it('returns success: true with the provided id', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSUpsertTool(baseConfig);
    const result = await tool.handler({ id: 'e99', text: 'some text' });

    expect(result).toEqual({ success: true, id: 'e99' });
  });
});

// ---------------------------------------------------------------------------
// createVSDeleteTool
// ---------------------------------------------------------------------------

describe('createVSDeleteTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('has name vs_delete', () => {
    const tool = createVSDeleteTool(baseConfig);
    expect(tool.name).toBe('vs_delete');
  });

  it('throws when vectorSearchIndex is not configured', () => {
    expect(() => createVSDeleteTool(configWithoutIndex)).toThrow('vectorSearchIndex is required');
  });

  it('calls the correct delete endpoint and sends primary_keys', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSDeleteTool(baseConfig);
    await tool.handler({ ids: ['e1', 'e2', 'e3'] });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain('/delete-data');

    const sentBody = JSON.parse(opts.body);
    expect(sentBody.primary_keys).toEqual(['e1', 'e2', 'e3']);
  });

  it('returns success: true with deleted count', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSDeleteTool(baseConfig);
    const result = await tool.handler({ ids: ['e1', 'e2'] });

    expect(result).toEqual({ success: true, deleted: 2 });
  });

  it('handles single id deletion', async () => {
    const mockFetch = mockFetchOk({});
    vi.stubGlobal('fetch', mockFetch);

    const tool = createVSDeleteTool(baseConfig);
    const result = await tool.handler({ ids: ['e42'] });

    expect(result).toEqual({ success: true, deleted: 1 });
  });
});

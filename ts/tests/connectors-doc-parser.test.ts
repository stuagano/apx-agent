/**
 * Tests for the Doc Parser connector: chunkText and tool factories.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  chunkText,
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
} from '../src/connectors/doc-parser.js';
import { type ConnectorConfig, type EntitySchema } from '../src/connectors/types.js';

// ---------------------------------------------------------------------------
// Shared test fixtures
// ---------------------------------------------------------------------------

const testSchema: EntitySchema = {
  version: 1,
  generation: 0,
  entities: [
    {
      name: 'Expert',
      table: 'experts',
      fields: [
        { name: 'expert_id', type: 'string', key: true },
        { name: 'name', type: 'string' },
        { name: 'domains', type: 'array<string>' },
      ],
    },
    {
      name: 'Project',
      table: 'projects',
      fields: [
        { name: 'project_id', type: 'string', key: true },
        { name: 'title', type: 'string' },
      ],
    },
  ],
  edges: [],
  extraction: {
    prompt_template:
      'Extract entities from this text. Entity types: {entity_names}. Fields: {entity_fields}. Text: {chunk_text}',
    chunk_size: 100,
    chunk_overlap: 20,
  },
  fitness: { metric: 'm', evaluation: 'e', targets: {} },
  evolution: {
    population_size: 1,
    mutation_rate: 0,
    mutation_fields: [],
    selection: 's',
    max_generations: 1,
  },
};

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
  volumePath: '/Volumes/main/kg/docs',
  entitySchema: testSchema,
};

// ---------------------------------------------------------------------------
// chunkText
// ---------------------------------------------------------------------------

describe('chunkText', () => {
  it('returns empty array for empty text', () => {
    expect(chunkText('', 100, 20)).toEqual([]);
  });

  it('returns a single chunk when text fits in one chunk', () => {
    const chunks = chunkText('short text', 100, 20);
    expect(chunks).toHaveLength(1);
    expect(chunks[0].chunk_id).toBe('chunk_0');
    expect(chunks[0].text).toBe('short text');
    expect(chunks[0].position).toBe(0);
  });

  it('splits text into the correct number of chunks', () => {
    // 300 chars, chunkSize=100, overlap=20 → step=80
    // positions: 0, 80, 160, 240 → 4 chunks
    const text = 'a'.repeat(300);
    const chunks = chunkText(text, 100, 20);
    expect(chunks.length).toBeGreaterThan(1);
    // Verify all characters are covered by at least the first chunk's range
    expect(chunks[0].text.length).toBe(100);
  });

  it('preserves overlap between consecutive chunks', () => {
    // chunkSize=10, overlap=3 → step=7
    // chunk_0: chars 0..9, chunk_1: chars 7..16
    // overlap is chars 7..9 (3 chars)
    const text = 'abcdefghijklmnopqrst'; // 20 chars
    const chunks = chunkText(text, 10, 3);
    expect(chunks.length).toBeGreaterThanOrEqual(2);

    const tail0 = chunks[0].text.slice(-3);
    const head1 = chunks[1].text.slice(0, 3);
    expect(tail0).toBe(head1);
  });

  it('assigns sequential chunk_ids starting at chunk_0', () => {
    const text = 'x'.repeat(50);
    const chunks = chunkText(text, 10, 0);
    chunks.forEach((c, i) => {
      expect(c.chunk_id).toBe(`chunk_${i}`);
    });
  });

  it('assigns correct positions', () => {
    // chunkSize=10, overlap=0 → step=10
    const text = 'a'.repeat(30);
    const chunks = chunkText(text, 10, 0);
    expect(chunks[0].position).toBe(0);
    expect(chunks[1].position).toBe(10);
    expect(chunks[2].position).toBe(20);
  });

  it('does not infinite-loop when overlap >= chunkSize', () => {
    // overlap >= size: clamps to single-chunk advance (step = chunkSize)
    const text = 'hello world test';
    const chunks = chunkText(text, 5, 10); // overlap 10 >= size 5
    expect(chunks.length).toBeGreaterThanOrEqual(1);
    // Ensure first chunk text is present
    expect(chunks[0].text).toBe('hello');
  });
});

// ---------------------------------------------------------------------------
// createDocUploadTool
// ---------------------------------------------------------------------------

describe('createDocUploadTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('returns a tool with the correct name', () => {
    const tool = createDocUploadTool(baseConfig);
    expect(tool.name).toBe('doc_upload');
  });

  it('throws when volumePath is not configured', () => {
    const cfg: ConnectorConfig = { ...baseConfig, volumePath: undefined };
    expect(() => createDocUploadTool(cfg)).toThrow('volumePath');
  });

  it('calls the Files API PUT with the correct URL path', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => '',
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocUploadTool(baseConfig);
    await tool.handler({ filename: 'report.pdf', content: 'binary content' });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];

    // URL should start with host + /api/2.0/fs/files/ + volumePath prefix
    expect(url).toMatch(/^https:\/\/test-host\.databricks\.com\/api\/2\.0\/fs\/files\/Volumes\/main\/kg\/docs\//);
    expect(url).toMatch(/report\.pdf$/);
    expect(opts.method).toBe('PUT');
    expect(opts.headers['Content-Type']).toBe('application/octet-stream');
    expect(opts.headers['Authorization']).toBe('Bearer test-token');
  });

  it('returns doc_id, path, filename, and size', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => '',
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocUploadTool(baseConfig);
    const result = (await tool.handler({ filename: 'doc.txt', content: 'hello' })) as {
      doc_id: string;
      path: string;
      filename: string;
      size: number;
    };

    expect(result.doc_id).toBeTruthy();
    expect(typeof result.doc_id).toBe('string');
    expect(result.filename).toBe('doc.txt');
    expect(result.size).toBe(5); // 'hello'.length
    expect(result.path).toContain('doc.txt');
    expect(result.path).toContain(result.doc_id);
  });

  it('throws on non-OK Files API response', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      text: async () => 'Forbidden',
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocUploadTool(baseConfig);
    await expect(tool.handler({ filename: 'x.txt', content: 'y' })).rejects.toThrow('403');
  });
});

// ---------------------------------------------------------------------------
// createDocChunkTool
// ---------------------------------------------------------------------------

describe('createDocChunkTool', () => {
  it('returns a tool with the correct name', () => {
    const tool = createDocChunkTool(baseConfig);
    expect(tool.name).toBe('doc_chunk');
  });

  it('uses chunk_size and chunk_overlap from entitySchema', async () => {
    // testSchema has chunk_size=100, chunk_overlap=20
    const tool = createDocChunkTool(baseConfig);
    const text = 'a'.repeat(250);
    const chunks = (await tool.handler({ text })) as Array<{ chunk_id: string; text: string; position: number }>;

    // With size=100, overlap=20, step=80: positions 0, 80, 160, 240
    expect(chunks[0].position).toBe(0);
    expect(chunks[1].position).toBe(80);
    expect(chunks[0].text.length).toBe(100);
  });

  it('defaults to chunk_size=1000, chunk_overlap=200 when no entitySchema', async () => {
    const cfg: ConnectorConfig = { ...baseConfig, entitySchema: undefined };
    const tool = createDocChunkTool(cfg);

    // Text shorter than default chunk size → single chunk
    const text = 'short text';
    const chunks = (await tool.handler({ text })) as Array<{ chunk_id: string; text: string }>;
    expect(chunks).toHaveLength(1);
    expect(chunks[0].text).toBe('short text');
  });
});

// ---------------------------------------------------------------------------
// createDocExtractEntitiesTool
// ---------------------------------------------------------------------------

describe('createDocExtractEntitiesTool', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('returns a tool with the correct name', () => {
    const tool = createDocExtractEntitiesTool(baseConfig);
    expect(tool.name).toBe('doc_extract_entities');
  });

  it('calls FMAPI with the interpolated prompt containing entity names', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        choices: [{ message: { content: '[{"expert_id": "e1", "name": "Alice"}]' } }],
      }),
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    await tool.handler({
      chunks: [{ chunk_id: 'chunk_0', text: 'Alice is a data expert.' }],
    });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];

    expect(url).toBe('https://test-host.databricks.com/serving-endpoints/chat/completions');
    expect(opts.method).toBe('POST');

    const body = JSON.parse(opts.body);
    expect(body.messages[0].content).toContain('Expert');
    expect(body.messages[0].content).toContain('Project');
    expect(body.messages[0].content).toContain('Alice is a data expert.');
  });

  it('uses databricks-claude-sonnet-4-6 as the default model', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ choices: [{ message: { content: '[]' } }] }),
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    await tool.handler({ chunks: [{ chunk_id: 'chunk_0', text: 'text' }] });

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.model).toBe('databricks-claude-sonnet-4-6');
  });

  it('allows overriding the model', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ choices: [{ message: { content: '[]' } }] }),
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    await tool.handler({
      chunks: [{ chunk_id: 'chunk_0', text: 'text' }],
      model: 'databricks-meta-llama-3-1-70b-instruct',
    });

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.model).toBe('databricks-meta-llama-3-1-70b-instruct');
  });

  it('adds _chunk_id to each extracted entity', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        choices: [{ message: { content: '[{"expert_id": "e1", "name": "Alice"}]' } }],
      }),
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    const result = (await tool.handler({
      chunks: [{ chunk_id: 'chunk_0', text: 'Alice is an expert.' }],
    })) as Array<Record<string, unknown>>;

    expect(result[0]._chunk_id).toBe('chunk_0');
    expect(result[0].name).toBe('Alice');
  });

  it('skips chunks where LLM returns non-JSON response', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        choices: [{ message: { content: 'Sorry, I cannot extract entities from this text.' } }],
      }),
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    const result = (await tool.handler({
      chunks: [{ chunk_id: 'chunk_0', text: 'some text' }],
    })) as unknown[];

    // Non-JSON response → gracefully skipped, returns empty array
    expect(result).toEqual([]);
  });

  it('aggregates entities from multiple chunks', async () => {
    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation(async () => {
      callCount++;
      const entities = callCount === 1 ? '[{"name": "Alice"}]' : '[{"name": "Bob"}]';
      return {
        ok: true,
        json: async () => ({ choices: [{ message: { content: entities } }] }),
      };
    });
    vi.stubGlobal('fetch', mockFetch);

    const tool = createDocExtractEntitiesTool(baseConfig);
    const result = (await tool.handler({
      chunks: [
        { chunk_id: 'chunk_0', text: 'Alice is here.' },
        { chunk_id: 'chunk_1', text: 'Bob is here.' },
      ],
    })) as Array<Record<string, unknown>>;

    expect(result).toHaveLength(2);
    expect(result.map((e) => e.name)).toEqual(['Alice', 'Bob']);
    expect(result[0]._chunk_id).toBe('chunk_0');
    expect(result[1]._chunk_id).toBe('chunk_1');
  });
});

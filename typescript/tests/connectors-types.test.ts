import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  parseEntitySchema,
  dbFetch,
  resolveHost,
  buildSqlParams,
  type ConnectorConfig,
  type EntitySchema,
} from '../src/connectors/types.js';

describe('parseEntitySchema', () => {
  it('parses a minimal schema object', () => {
    const raw = {
      version: 1,
      generation: 0,
      entities: [
        {
          name: 'Expert',
          table: 'experts',
          fields: [{ name: 'expert_id', type: 'string', key: true }],
        },
      ],
      edges: [],
      extraction: { prompt_template: 'Extract: {chunk_text}', chunk_size: 500, chunk_overlap: 100 },
      fitness: { metric: 'score', evaluation: 'eval', targets: { precision: 0.8 } },
      evolution: {
        population_size: 3,
        mutation_rate: 0.1,
        mutation_fields: [],
        selection: 'top_1',
        max_generations: 5,
      },
    };
    const schema = parseEntitySchema(raw);
    expect(schema.version).toBe(1);
    expect(schema.generation).toBe(0);
    expect(schema.entities).toHaveLength(1);
    expect(schema.entities[0].name).toBe('Expert');
    expect(schema.entities[0].fields[0].key).toBe(true);
  });

  it('throws on missing required fields', () => {
    expect(() => parseEntitySchema({})).toThrow();
    expect(() => parseEntitySchema({ version: 1 })).toThrow();
  });

  it('parses edges with from/to references', () => {
    const raw = {
      version: 1,
      generation: 0,
      entities: [],
      edges: [
        {
          name: 'matched_to',
          table: 'edges_matched',
          from: 'Expert',
          to: 'Project',
          fields: [{ name: 'weight', type: 'float', default: 0.5 }],
        },
      ],
      extraction: { prompt_template: '', chunk_size: 500, chunk_overlap: 100 },
      fitness: { metric: 'm', evaluation: 'e', targets: {} },
      evolution: { population_size: 1, mutation_rate: 0, mutation_fields: [], selection: 's', max_generations: 1 },
    };
    const schema = parseEntitySchema(raw);
    expect(schema.edges[0].from).toBe('Expert');
    expect(schema.edges[0].to).toBe('Project');
    expect(schema.edges[0].fields[0].default).toBe(0.5);
  });
});

describe('resolveHost', () => {
  it('returns config host when provided', () => {
    expect(resolveHost('https://my-ws.cloud.databricks.com')).toBe('https://my-ws.cloud.databricks.com');
  });

  it('adds https:// when missing', () => {
    expect(resolveHost('my-ws.cloud.databricks.com')).toBe('https://my-ws.cloud.databricks.com');
  });

  it('strips trailing slash', () => {
    expect(resolveHost('https://my-ws.cloud.databricks.com/')).toBe('https://my-ws.cloud.databricks.com');
  });

  it('falls back to DATABRICKS_HOST env var', () => {
    const prev = process.env.DATABRICKS_HOST;
    process.env.DATABRICKS_HOST = 'https://env-host.databricks.com';
    try {
      expect(resolveHost()).toBe('https://env-host.databricks.com');
    } finally {
      if (prev !== undefined) process.env.DATABRICKS_HOST = prev;
      else delete process.env.DATABRICKS_HOST;
    }
  });

  it('throws when no host available', () => {
    const prev = process.env.DATABRICKS_HOST;
    delete process.env.DATABRICKS_HOST;
    try {
      expect(() => resolveHost()).toThrow('DATABRICKS_HOST');
    } finally {
      if (prev !== undefined) process.env.DATABRICKS_HOST = prev;
    }
  });
});

describe('buildSqlParams', () => {
  it('builds parameterized WHERE clause from filters', () => {
    const { clause, params } = buildSqlParams({ name: 'Alice', age: 30 });
    expect(clause).toBe('name = :name AND age = :age');
    expect(params).toEqual([
      { name: 'name', value: 'Alice', type: 'STRING' },
      { name: 'age', value: '30', type: 'INT' },
    ]);
  });

  it('returns empty clause for empty filters', () => {
    const { clause, params } = buildSqlParams({});
    expect(clause).toBe('');
    expect(params).toEqual([]);
  });

  it('handles float values', () => {
    const { params } = buildSqlParams({ weight: 0.75 });
    expect(params[0].type).toBe('FLOAT');
    expect(params[0].value).toBe('0.75');
  });

  it('handles boolean values', () => {
    const { params } = buildSqlParams({ active: true });
    expect(params[0].type).toBe('BOOLEAN');
    expect(params[0].value).toBe('true');
  });
});

describe('dbFetch', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('adds Authorization header from token', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ result: 'ok' }),
    });
    vi.stubGlobal('fetch', mockFetch);

    await dbFetch('https://host.databricks.com/api/2.0/test', {
      token: 'tok_123',
      method: 'GET',
    });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe('https://host.databricks.com/api/2.0/test');
    expect(opts.headers['Authorization']).toBe('Bearer tok_123');
  });

  it('sends JSON body for POST requests', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', mockFetch);

    await dbFetch('https://host.databricks.com/api/2.0/test', {
      token: 'tok_123',
      method: 'POST',
      body: { key: 'value' },
    });

    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.method).toBe('POST');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ key: 'value' });
  });

  it('throws on non-OK response', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      text: async () => 'Forbidden',
    });
    vi.stubGlobal('fetch', mockFetch);

    await expect(
      dbFetch('https://host.databricks.com/api/2.0/test', { token: 'tok', method: 'GET' }),
    ).rejects.toThrow('Databricks API 403');
  });
});

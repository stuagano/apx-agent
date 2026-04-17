# AppKit Domain Connectors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three generic domain connectors (Lakebase, Vector Search, Doc Parser) to the appkit-agent TypeScript package, enabling schema-driven tool composition for Databricks Apps.

**Architecture:** Each connector exports `defineTool()` factories that accept a `ConnectorConfig`. The config includes an optional `EntitySchema` (parsed from schema.yaml) that drives validation and prompt templates. Connectors use Databricks REST APIs via `fetch` — no native drivers. OBO auth tokens flow from AppKit request headers.

**Tech Stack:** TypeScript, Zod v4, vitest, Databricks REST APIs (SQL Statements, Vector Search, Files)

**Spec:** `docs/superpowers/specs/2026-04-15-guidepoint-appkit-connectors-design.md`

---

## File Map

| File | Responsibility |
|------|----------------|
| `ts/src/connectors/types.ts` | ConnectorConfig, EntitySchema, and all related interfaces. Schema YAML parser. Databricks API helper (`dbFetch`). |
| `ts/src/connectors/lakebase.ts` | `createLakebaseQueryTool`, `createLakebaseMutateTool`, `createLakebaseSchemaInspectTool` |
| `ts/src/connectors/vector-search.ts` | `createVSQueryTool`, `createVSUpsertTool`, `createVSDeleteTool` |
| `ts/src/connectors/doc-parser.ts` | `createDocUploadTool`, `createDocChunkTool`, `createDocExtractEntitiesTool` |
| `ts/src/connectors/index.ts` | Re-exports from all connector modules |
| `ts/src/index.ts` | Add connector exports to package entry point |
| `ts/tests/connectors-types.test.ts` | Tests for types, schema parsing, dbFetch |
| `ts/tests/connectors-lakebase.test.ts` | Tests for Lakebase tool factories |
| `ts/tests/connectors-vector-search.test.ts` | Tests for Vector Search tool factories |
| `ts/tests/connectors-doc-parser.test.ts` | Tests for Doc Parser tool factories |

---

### Task 1: Shared Types and Databricks API Helper

**Files:**
- Create: `ts/src/connectors/types.ts`
- Create: `ts/tests/connectors-types.test.ts`

- [ ] **Step 1: Write the failing tests for ConnectorConfig, EntitySchema, and dbFetch**

```typescript
// ts/tests/connectors-types.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  parseEntitySchema,
  dbFetch,
  resolveHost,
  buildSqlParams,
  type ConnectorConfig,
  type EntitySchema,
} from '../src/connectors/types.js';

// ---------------------------------------------------------------------------
// parseEntitySchema
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// resolveHost
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// buildSqlParams
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// dbFetch
// ---------------------------------------------------------------------------

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-types.test.ts`
Expected: FAIL — module `../src/connectors/types.js` does not exist

- [ ] **Step 3: Implement types.ts**

```typescript
// ts/src/connectors/types.ts

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Field / Entity / Edge definitions
// ---------------------------------------------------------------------------

export interface FieldDef {
  name: string;
  type: string;
  key?: boolean;
  nullable?: boolean;
  default?: number | string;
  index?: boolean;
}

export interface EntityDef {
  name: string;
  table: string;
  fields: FieldDef[];
  embedding_source?: string;
}

export interface EdgeDef {
  name: string;
  table: string;
  from: string;
  to: string;
  fields: FieldDef[];
}

// ---------------------------------------------------------------------------
// Schema sub-configs
// ---------------------------------------------------------------------------

export interface ExtractionConfig {
  prompt_template: string;
  chunk_size: number;
  chunk_overlap: number;
}

export interface FitnessConfig {
  metric: string;
  evaluation: string;
  targets: Record<string, number>;
}

export interface EvolutionConfig {
  population_size: number;
  mutation_rate: number;
  mutation_fields: string[];
  selection: string;
  max_generations: number;
}

// ---------------------------------------------------------------------------
// EntitySchema — the full genome
// ---------------------------------------------------------------------------

export interface EntitySchema {
  version: number;
  generation: number;
  entities: EntityDef[];
  edges: EdgeDef[];
  extraction: ExtractionConfig;
  fitness: FitnessConfig;
  evolution: EvolutionConfig;
}

// ---------------------------------------------------------------------------
// ConnectorConfig — passed to every tool factory
// ---------------------------------------------------------------------------

export interface ConnectorConfig {
  /** Databricks workspace host. Defaults to DATABRICKS_HOST env var. */
  host?: string;
  /** Lakebase catalog for entity/edge tables. */
  catalog: string;
  /** Lakebase schema within the catalog. */
  schema: string;
  /** Vector Search index name. */
  vectorSearchIndex?: string;
  /** UC Volume path for document storage. */
  volumePath?: string;
  /** Schema definition loaded from schema.yaml. */
  entitySchema?: EntitySchema;
}

// ---------------------------------------------------------------------------
// Schema parser (validates raw YAML/JSON → EntitySchema)
// ---------------------------------------------------------------------------

const fieldDefSchema = z.object({
  name: z.string(),
  type: z.string(),
  key: z.boolean().optional(),
  nullable: z.boolean().optional(),
  default: z.union([z.number(), z.string()]).optional(),
  index: z.boolean().optional(),
});

const entityDefSchema = z.object({
  name: z.string(),
  table: z.string(),
  fields: z.array(fieldDefSchema),
  embedding_source: z.string().optional(),
});

const edgeDefSchema = z.object({
  name: z.string(),
  table: z.string(),
  from: z.string(),
  to: z.string(),
  fields: z.array(fieldDefSchema),
});

const extractionSchema = z.object({
  prompt_template: z.string(),
  chunk_size: z.number().int().positive(),
  chunk_overlap: z.number().int().min(0),
});

const fitnessSchema = z.object({
  metric: z.string(),
  evaluation: z.string(),
  targets: z.record(z.number()),
});

const evolutionSchema = z.object({
  population_size: z.number().int().positive(),
  mutation_rate: z.number().min(0).max(1),
  mutation_fields: z.array(z.string()),
  selection: z.string(),
  max_generations: z.number().int().positive(),
});

const entitySchemaValidator = z.object({
  version: z.number().int(),
  generation: z.number().int().min(0),
  entities: z.array(entityDefSchema),
  edges: z.array(edgeDefSchema),
  extraction: extractionSchema,
  fitness: fitnessSchema,
  evolution: evolutionSchema,
});

/** Parse and validate a raw object (from YAML or JSON) into an EntitySchema. */
export function parseEntitySchema(raw: unknown): EntitySchema {
  return entitySchemaValidator.parse(raw);
}

// ---------------------------------------------------------------------------
// Host resolution
// ---------------------------------------------------------------------------

/** Resolve Databricks host from explicit value or DATABRICKS_HOST env var. */
export function resolveHost(host?: string): string {
  const h = host ?? process.env.DATABRICKS_HOST;
  if (!h) throw new Error('No Databricks host: pass host in config or set DATABRICKS_HOST env var');
  const normalized = h.startsWith('http') ? h : `https://${h}`;
  return normalized.replace(/\/$/, '');
}

// ---------------------------------------------------------------------------
// SQL parameter builder
// ---------------------------------------------------------------------------

export interface SqlParam {
  name: string;
  value: string;
  type: 'STRING' | 'INT' | 'FLOAT' | 'BOOLEAN';
}

/** Build a parameterized WHERE clause and parameter list from a filters object. */
export function buildSqlParams(filters: Record<string, unknown>): {
  clause: string;
  params: SqlParam[];
} {
  const entries = Object.entries(filters);
  if (entries.length === 0) return { clause: '', params: [] };

  const params: SqlParam[] = entries.map(([key, value]) => {
    let type: SqlParam['type'] = 'STRING';
    if (typeof value === 'number') {
      type = Number.isInteger(value) ? 'INT' : 'FLOAT';
    } else if (typeof value === 'boolean') {
      type = 'BOOLEAN';
    }
    return { name: key, value: String(value), type };
  });

  const clause = entries.map(([key]) => `${key} = :${key}`).join(' AND ');
  return { clause, params };
}

// ---------------------------------------------------------------------------
// Databricks API fetch helper
// ---------------------------------------------------------------------------

export interface DbFetchOptions {
  token: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
}

/** Fetch wrapper for Databricks REST APIs with auth and error handling. */
export async function dbFetch<T = unknown>(url: string, opts: DbFetchOptions): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${opts.token}`,
  };

  const init: RequestInit = {
    method: opts.method,
    headers,
  };

  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }

  const res = await fetch(url, init);

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Databricks API ${res.status}: ${text}`);
  }

  return res.json() as Promise<T>;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-types.test.ts`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/ts
git add src/connectors/types.ts tests/connectors-types.test.ts
git commit -m "feat(connectors): add shared types, schema parser, and dbFetch helper"
```

---

### Task 2: Lakebase Connector

**Files:**
- Create: `ts/src/connectors/lakebase.ts`
- Create: `ts/tests/connectors-lakebase.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/connectors-lakebase.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { z } from 'zod';
import {
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
} from '../src/connectors/lakebase.js';
import type { ConnectorConfig } from '../src/connectors/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
};

function mockFetchResponse(data: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 500,
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------
// createLakebaseQueryTool
// ---------------------------------------------------------------------------

describe('createLakebaseQueryTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name and description', () => {
    const tool = createLakebaseQueryTool(baseConfig);
    expect(tool.name).toBe('lakebase_query');
    expect(tool.description).toContain('query');
  });

  it('has Zod parameters for table, columns, filters, limit', () => {
    const tool = createLakebaseQueryTool(baseConfig);
    // Validate that the expected inputs are accepted
    const valid = tool.parameters.parse({
      table: 'experts',
      columns: ['name', 'domains'],
      filters: { expert_id: 'abc' },
      limit: 10,
    });
    expect(valid.table).toBe('experts');
  });

  it('builds SELECT with catalog.schema prefix and executes via SQL Statements API', async () => {
    // Mock the SQL Statements API response (async poll pattern)
    const stmtResponse = {
      statement_id: 'stmt-1',
      status: { state: 'SUCCEEDED' },
      manifest: { schema: { columns: [{ name: 'name' }, { name: 'domains' }] } },
      result: {
        data_array: [['Alice', '["AI","ML"]'], ['Bob', '["Data"]']],
      },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseQueryTool(baseConfig);
    const result = await tool.handler({
      table: 'experts',
      columns: ['name', 'domains'],
      filters: {},
      limit: 50,
    });

    // Verify fetch was called with SQL Statements API
    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toBe('https://test-host.databricks.com/api/2.0/sql/statements/');
    const body = JSON.parse(opts.body);
    expect(body.statement).toContain('SELECT name, domains FROM main.kg.experts');
    expect(body.statement).toContain('LIMIT 50');
  });

  it('adds WHERE clause when filters are provided', async () => {
    const stmtResponse = {
      statement_id: 'stmt-2',
      status: { state: 'SUCCEEDED' },
      manifest: { schema: { columns: [{ name: 'name' }] } },
      result: { data_array: [['Alice']] },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({
      table: 'experts',
      columns: ['name'],
      filters: { expert_id: 'abc123' },
      limit: 10,
    });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('WHERE expert_id = :expert_id');
    expect(body.parameters).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: 'expert_id', value: 'abc123' }),
      ]),
    );
  });

  it('defaults limit to 100 when not provided', async () => {
    const stmtResponse = {
      statement_id: 'stmt-3',
      status: { state: 'SUCCEEDED' },
      manifest: { schema: { columns: [{ name: 'name' }] } },
      result: { data_array: [] },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseQueryTool(baseConfig);
    await tool.handler({ table: 'experts', filters: {} });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('LIMIT 100');
  });

  it('formats result as array of row objects', async () => {
    const stmtResponse = {
      statement_id: 'stmt-4',
      status: { state: 'SUCCEEDED' },
      manifest: { schema: { columns: [{ name: 'expert_id' }, { name: 'name' }] } },
      result: { data_array: [['e1', 'Alice'], ['e2', 'Bob']] },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseQueryTool(baseConfig);
    const result = await tool.handler({ table: 'experts', filters: {} });

    expect(result).toEqual([
      { expert_id: 'e1', name: 'Alice' },
      { expert_id: 'e2', name: 'Bob' },
    ]);
  });
});

// ---------------------------------------------------------------------------
// createLakebaseMutateTool
// ---------------------------------------------------------------------------

describe('createLakebaseMutateTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createLakebaseMutateTool(baseConfig);
    expect(tool.name).toBe('lakebase_mutate');
  });

  it('accepts insert operation and builds INSERT statement', async () => {
    const stmtResponse = {
      statement_id: 'stmt-5',
      status: { state: 'SUCCEEDED' },
      result: { data_array: [] },
      manifest: { schema: { columns: [] } },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'experts',
      operation: 'insert',
      values: { expert_id: 'e3', name: 'Carol' },
    });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('INSERT INTO main.kg.experts');
    expect(body.statement).toContain('expert_id, name');
  });

  it('accepts update operation with filters and builds UPDATE statement', async () => {
    const stmtResponse = {
      statement_id: 'stmt-6',
      status: { state: 'SUCCEEDED' },
      result: { data_array: [] },
      manifest: { schema: { columns: [] } },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'edges_matched',
      operation: 'update',
      values: { weight: 0.8 },
      filters: { match_type: 'semantic' },
    });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('UPDATE main.kg.edges_matched');
    expect(body.statement).toContain('SET weight = :set_weight');
    expect(body.statement).toContain('WHERE match_type = :match_type');
  });

  it('accepts delete operation with filters', async () => {
    const stmtResponse = {
      statement_id: 'stmt-7',
      status: { state: 'SUCCEEDED' },
      result: { data_array: [] },
      manifest: { schema: { columns: [] } },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseMutateTool(baseConfig);
    await tool.handler({
      table: 'edges_matched',
      operation: 'delete',
      filters: { confidence: 0 },
    });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('DELETE FROM main.kg.edges_matched');
    expect(body.statement).toContain('WHERE confidence = :confidence');
  });

  it('rejects delete without filters (safety)', async () => {
    const tool = createLakebaseMutateTool(baseConfig);
    await expect(
      tool.handler({ table: 'experts', operation: 'delete', filters: {} }),
    ).rejects.toThrow('filters');
  });
});

// ---------------------------------------------------------------------------
// createLakebaseSchemaInspectTool
// ---------------------------------------------------------------------------

describe('createLakebaseSchemaInspectTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createLakebaseSchemaInspectTool(baseConfig);
    expect(tool.name).toBe('lakebase_schema_inspect');
  });

  it('queries information_schema for table/column info', async () => {
    const stmtResponse = {
      statement_id: 'stmt-8',
      status: { state: 'SUCCEEDED' },
      manifest: {
        schema: { columns: [{ name: 'table_name' }, { name: 'column_name' }, { name: 'data_type' }] },
      },
      result: {
        data_array: [
          ['experts', 'expert_id', 'STRING'],
          ['experts', 'name', 'STRING'],
        ],
      },
    };
    vi.stubGlobal('fetch', mockFetchResponse(stmtResponse));

    const tool = createLakebaseSchemaInspectTool(baseConfig);
    const result = await tool.handler({});

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.statement).toContain('information_schema.columns');
    expect(body.statement).toContain('main');
    expect(body.statement).toContain('kg');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-lakebase.test.ts`
Expected: FAIL — module `../src/connectors/lakebase.js` does not exist

- [ ] **Step 3: Implement lakebase.ts**

```typescript
// ts/src/connectors/lakebase.ts

import { z } from 'zod';
import { defineTool } from '../agent/tools.js';
import type { AgentTool } from '../agent/tools.js';
import type { ConnectorConfig, SqlParam } from './types.js';
import { resolveHost, buildSqlParams } from './types.js';

// ---------------------------------------------------------------------------
// SQL Statements API helper
// ---------------------------------------------------------------------------

interface StatementResponse {
  statement_id: string;
  status: { state: string };
  manifest: { schema: { columns: Array<{ name: string }> } };
  result: { data_array: string[][] };
}

async function executeSql(
  host: string,
  token: string,
  catalog: string,
  schema: string,
  statement: string,
  params: SqlParam[] = [],
): Promise<StatementResponse> {
  const res = await fetch(`${host}/api/2.0/sql/statements/`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      statement,
      warehouse_id: process.env.DATABRICKS_WAREHOUSE_ID ?? '',
      catalog,
      schema,
      parameters: params.length > 0 ? params : undefined,
      wait_timeout: '30s',
      disposition: 'INLINE',
      format: 'JSON_ARRAY',
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`SQL Statements API ${res.status}: ${text}`);
  }

  return res.json() as Promise<StatementResponse>;
}

function rowsToObjects(response: StatementResponse): Record<string, string>[] {
  const columns = response.manifest.schema.columns.map((c) => c.name);
  return (response.result.data_array ?? []).map((row) => {
    const obj: Record<string, string> = {};
    for (let i = 0; i < columns.length; i++) {
      obj[columns[i]] = row[i];
    }
    return obj;
  });
}

function resolveToken(oboHeaders?: Record<string, string>): string {
  if (oboHeaders) {
    const obo =
      oboHeaders['x-forwarded-access-token'] ||
      (oboHeaders['authorization'] ?? '').replace(/^Bearer\s+/i, '');
    if (obo) return obo;
  }
  return process.env.DATABRICKS_TOKEN ?? '';
}

// ---------------------------------------------------------------------------
// createLakebaseQueryTool
// ---------------------------------------------------------------------------

export function createLakebaseQueryTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);

  return defineTool({
    name: 'lakebase_query',
    description: `Query tables in ${config.catalog}.${config.schema} using structured filters. Returns rows as JSON.`,
    parameters: z.object({
      table: z.string().describe('Table name (without catalog.schema prefix)'),
      columns: z.array(z.string()).optional().describe('Columns to select. Omit for all columns.'),
      filters: z.record(z.unknown()).describe('Column=value filters for WHERE clause'),
      limit: z.number().int().positive().optional().describe('Max rows to return (default 100)'),
    }),
    handler: async ({ table, columns, filters, limit }) => {
      const token = resolveToken();
      const fqn = `${config.catalog}.${config.schema}.${table}`;
      const cols = columns && columns.length > 0 ? columns.join(', ') : '*';
      const maxRows = limit ?? 100;

      const { clause, params } = buildSqlParams(filters ?? {});
      const where = clause ? ` WHERE ${clause}` : '';
      const statement = `SELECT ${cols} FROM ${fqn}${where} LIMIT ${maxRows}`;

      const response = await executeSql(host, token, config.catalog, config.schema, statement, params);
      return rowsToObjects(response);
    },
  });
}

// ---------------------------------------------------------------------------
// createLakebaseMutateTool
// ---------------------------------------------------------------------------

export function createLakebaseMutateTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);

  return defineTool({
    name: 'lakebase_mutate',
    description: `Insert, update, or delete rows in ${config.catalog}.${config.schema} tables.`,
    parameters: z.object({
      table: z.string().describe('Table name (without catalog.schema prefix)'),
      operation: z.enum(['insert', 'update', 'delete']).describe('SQL operation'),
      values: z.record(z.unknown()).optional().describe('Column=value pairs for INSERT or SET clause'),
      filters: z.record(z.unknown()).optional().describe('Column=value filters for WHERE clause (required for UPDATE/DELETE)'),
    }),
    handler: async ({ table, operation, values, filters }) => {
      const token = resolveToken();
      const fqn = `${config.catalog}.${config.schema}.${table}`;
      let statement: string;
      let params: SqlParam[] = [];

      if (operation === 'insert') {
        const entries = Object.entries(values ?? {});
        if (entries.length === 0) throw new Error('insert requires at least one value');
        const colNames = entries.map(([k]) => k).join(', ');
        const placeholders = entries.map(([k]) => `:${k}`).join(', ');
        params = entries.map(([k, v]) => {
          let type: SqlParam['type'] = 'STRING';
          if (typeof v === 'number') type = Number.isInteger(v) ? 'INT' : 'FLOAT';
          else if (typeof v === 'boolean') type = 'BOOLEAN';
          return { name: k, value: String(v), type };
        });
        statement = `INSERT INTO ${fqn} (${colNames}) VALUES (${placeholders})`;
      } else if (operation === 'update') {
        const setEntries = Object.entries(values ?? {});
        if (setEntries.length === 0) throw new Error('update requires at least one value');
        const filterEntries = Object.entries(filters ?? {});
        if (filterEntries.length === 0) throw new Error('update requires at least one filter');

        const setParams: SqlParam[] = setEntries.map(([k, v]) => {
          let type: SqlParam['type'] = 'STRING';
          if (typeof v === 'number') type = Number.isInteger(v) ? 'INT' : 'FLOAT';
          else if (typeof v === 'boolean') type = 'BOOLEAN';
          return { name: `set_${k}`, value: String(v), type };
        });
        const setClause = setEntries.map(([k]) => `${k} = :set_${k}`).join(', ');

        const { clause: whereClause, params: whereParams } = buildSqlParams(filters ?? {});
        params = [...setParams, ...whereParams];
        statement = `UPDATE ${fqn} SET ${setClause} WHERE ${whereClause}`;
      } else {
        // delete
        const filterEntries = Object.entries(filters ?? {});
        if (filterEntries.length === 0) throw new Error('delete requires at least one filter (safety: no unfiltered deletes)');
        const { clause, params: whereParams } = buildSqlParams(filters ?? {});
        params = whereParams;
        statement = `DELETE FROM ${fqn} WHERE ${clause}`;
      }

      const response = await executeSql(host, token, config.catalog, config.schema, statement, params);
      return { success: true, statement_id: response.statement_id };
    },
  });
}

// ---------------------------------------------------------------------------
// createLakebaseSchemaInspectTool
// ---------------------------------------------------------------------------

export function createLakebaseSchemaInspectTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);

  return defineTool({
    name: 'lakebase_schema_inspect',
    description: `List tables and columns in ${config.catalog}.${config.schema}.`,
    parameters: z.object({
      table_filter: z.string().optional().describe('Optional table name to filter to'),
    }),
    handler: async ({ table_filter }) => {
      const token = resolveToken();
      let statement =
        `SELECT table_name, column_name, data_type, is_nullable ` +
        `FROM ${config.catalog}.information_schema.columns ` +
        `WHERE table_catalog = '${config.catalog}' AND table_schema = '${config.schema}'`;

      if (table_filter) {
        statement += ` AND table_name = '${table_filter}'`;
      }
      statement += ` ORDER BY table_name, ordinal_position`;

      const response = await executeSql(host, token, config.catalog, config.schema, statement);
      return rowsToObjects(response);
    },
  });
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-lakebase.test.ts`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/ts
git add src/connectors/lakebase.ts tests/connectors-lakebase.test.ts
git commit -m "feat(connectors): add Lakebase query, mutate, and schema inspect tools"
```

---

### Task 3: Vector Search Connector

**Files:**
- Create: `ts/src/connectors/vector-search.ts`
- Create: `ts/tests/connectors-vector-search.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/connectors-vector-search.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  createVSQueryTool,
  createVSUpsertTool,
  createVSDeleteTool,
} from '../src/connectors/vector-search.js';
import type { ConnectorConfig } from '../src/connectors/types.js';

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
  vectorSearchIndex: 'main.kg.experts_vs_index',
};

function mockFetchResponse(data: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 500,
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------
// createVSQueryTool
// ---------------------------------------------------------------------------

describe('createVSQueryTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createVSQueryTool(baseConfig);
    expect(tool.name).toBe('vector_search_query');
  });

  it('calls Vector Search query endpoint with query_text', async () => {
    const vsResponse = {
      manifest: { column_count: 3, columns: [{ name: 'id' }, { name: 'text' }, { name: 'score' }] },
      result: {
        row_count: 2,
        data_array: [['e1', 'Expert in AI', 0.95], ['e2', 'ML specialist', 0.87]],
      },
    };
    vi.stubGlobal('fetch', mockFetchResponse(vsResponse));

    const tool = createVSQueryTool(baseConfig);
    const result = await tool.handler({ query_text: 'AI expert', num_results: 5 });

    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toContain('/api/2.0/vector-search/indexes/main.kg.experts_vs_index/query');
    const body = JSON.parse(opts.body);
    expect(body.query_text).toBe('AI expert');
    expect(body.num_results).toBe(5);

    expect(result).toEqual([
      { id: 'e1', text: 'Expert in AI', score: 0.95 },
      { id: 'e2', text: 'ML specialist', score: 0.87 },
    ]);
  });

  it('passes filters_json when filters are provided', async () => {
    const vsResponse = {
      manifest: { column_count: 2, columns: [{ name: 'id' }, { name: 'score' }] },
      result: { row_count: 0, data_array: [] },
    };
    vi.stubGlobal('fetch', mockFetchResponse(vsResponse));

    const tool = createVSQueryTool(baseConfig);
    await tool.handler({ query_text: 'test', filters: { industry: 'Finance' }, num_results: 10 });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.filters_json).toBe('{"industry": "Finance"}');
  });

  it('throws when vectorSearchIndex is not configured', () => {
    const noIndex = { ...baseConfig, vectorSearchIndex: undefined };
    expect(() => createVSQueryTool(noIndex)).toThrow('vectorSearchIndex');
  });
});

// ---------------------------------------------------------------------------
// createVSUpsertTool
// ---------------------------------------------------------------------------

describe('createVSUpsertTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createVSUpsertTool(baseConfig);
    expect(tool.name).toBe('vector_search_upsert');
  });

  it('calls upsert endpoint with id, text, and metadata', async () => {
    vi.stubGlobal('fetch', mockFetchResponse({ status: 'SUCCESS' }));

    const tool = createVSUpsertTool(baseConfig);
    await tool.handler({
      id: 'e3',
      text: 'Carol is an expert in NLP',
      metadata: { name: 'Carol', domains: ['NLP'] },
    });

    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toContain('/api/2.0/vector-search/indexes/main.kg.experts_vs_index/upsert-data');
    const body = JSON.parse(opts.body);
    expect(body.inputs_json).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// createVSDeleteTool
// ---------------------------------------------------------------------------

describe('createVSDeleteTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createVSDeleteTool(baseConfig);
    expect(tool.name).toBe('vector_search_delete');
  });

  it('calls delete endpoint with primary keys', async () => {
    vi.stubGlobal('fetch', mockFetchResponse({ status: 'SUCCESS' }));

    const tool = createVSDeleteTool(baseConfig);
    await tool.handler({ ids: ['e1', 'e2'] });

    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toContain('/api/2.0/vector-search/indexes/main.kg.experts_vs_index/delete-data');
    const body = JSON.parse(opts.body);
    expect(body.primary_keys).toEqual(['e1', 'e2']);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-vector-search.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement vector-search.ts**

```typescript
// ts/src/connectors/vector-search.ts

import { z } from 'zod';
import { defineTool } from '../agent/tools.js';
import type { AgentTool } from '../agent/tools.js';
import type { ConnectorConfig } from './types.js';
import { resolveHost } from './types.js';

function resolveToken(): string {
  return process.env.DATABRICKS_TOKEN ?? '';
}

function requireIndex(config: ConnectorConfig): string {
  if (!config.vectorSearchIndex) {
    throw new Error('vectorSearchIndex must be set in ConnectorConfig for Vector Search tools');
  }
  return config.vectorSearchIndex;
}

// ---------------------------------------------------------------------------
// createVSQueryTool
// ---------------------------------------------------------------------------

export function createVSQueryTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);
  const indexName = requireIndex(config);

  return defineTool({
    name: 'vector_search_query',
    description: `Semantic similarity search against the ${indexName} Vector Search index.`,
    parameters: z.object({
      query_text: z.string().describe('Text to search for similar vectors'),
      filters: z.record(z.unknown()).optional().describe('Column filters to narrow results'),
      num_results: z.number().int().positive().optional().describe('Max results (default 10)'),
    }),
    handler: async ({ query_text, filters, num_results }) => {
      const token = resolveToken();
      const body: Record<string, unknown> = {
        query_text,
        num_results: num_results ?? 10,
        columns: [],
      };
      if (filters && Object.keys(filters).length > 0) {
        body.filters_json = JSON.stringify(filters);
      }

      const res = await fetch(
        `${host}/api/2.0/vector-search/indexes/${indexName}/query`,
        {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(body),
        },
      );

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Vector Search query ${res.status}: ${text}`);
      }

      const data = (await res.json()) as {
        manifest: { columns: Array<{ name: string }> };
        result: { data_array: unknown[][] };
      };

      const columns = data.manifest.columns.map((c) => c.name);
      return (data.result.data_array ?? []).map((row) => {
        const obj: Record<string, unknown> = {};
        for (let i = 0; i < columns.length; i++) {
          obj[columns[i]] = row[i];
        }
        return obj;
      });
    },
  });
}

// ---------------------------------------------------------------------------
// createVSUpsertTool
// ---------------------------------------------------------------------------

export function createVSUpsertTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);
  const indexName = requireIndex(config);

  return defineTool({
    name: 'vector_search_upsert',
    description: `Add or update a vector in the ${indexName} index. Embedding is computed server-side.`,
    parameters: z.object({
      id: z.string().describe('Primary key for the vector'),
      text: z.string().describe('Text to embed and index'),
      metadata: z.record(z.unknown()).optional().describe('Additional columns to store'),
    }),
    handler: async ({ id, text, metadata }) => {
      const token = resolveToken();
      const row: Record<string, unknown> = { id, text, ...(metadata ?? {}) };

      const res = await fetch(
        `${host}/api/2.0/vector-search/indexes/${indexName}/upsert-data`,
        {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ inputs_json: JSON.stringify([row]) }),
        },
      );

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Vector Search upsert ${res.status}: ${text}`);
      }

      return { success: true, id };
    },
  });
}

// ---------------------------------------------------------------------------
// createVSDeleteTool
// ---------------------------------------------------------------------------

export function createVSDeleteTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);
  const indexName = requireIndex(config);

  return defineTool({
    name: 'vector_search_delete',
    description: `Delete vectors from the ${indexName} index by primary key.`,
    parameters: z.object({
      ids: z.array(z.string()).describe('Primary keys of vectors to delete'),
    }),
    handler: async ({ ids }) => {
      const token = resolveToken();

      const res = await fetch(
        `${host}/api/2.0/vector-search/indexes/${indexName}/delete-data`,
        {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ primary_keys: ids }),
        },
      );

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Vector Search delete ${res.status}: ${errText}`);
      }

      return { success: true, deleted: ids.length };
    },
  });
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-vector-search.test.ts`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/ts
git add src/connectors/vector-search.ts tests/connectors-vector-search.test.ts
git commit -m "feat(connectors): add Vector Search query, upsert, and delete tools"
```

---

### Task 4: Doc Parser Connector

**Files:**
- Create: `ts/src/connectors/doc-parser.ts`
- Create: `ts/tests/connectors-doc-parser.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/connectors-doc-parser.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
  chunkText,
} from '../src/connectors/doc-parser.js';
import type { ConnectorConfig, EntitySchema } from '../src/connectors/types.js';

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
    prompt_template: 'Extract entities from this text. Entity types: {entity_names}. Fields: {entity_fields}. Text: {chunk_text}',
    chunk_size: 100,
    chunk_overlap: 20,
  },
  fitness: { metric: 'm', evaluation: 'e', targets: {} },
  evolution: { population_size: 1, mutation_rate: 0, mutation_fields: [], selection: 's', max_generations: 1 },
};

const baseConfig: ConnectorConfig = {
  host: 'https://test-host.databricks.com',
  catalog: 'main',
  schema: 'kg',
  volumePath: '/Volumes/main/kg/docs',
  entitySchema: testSchema,
};

function mockFetchResponse(data: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 500,
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------
// chunkText (pure function, no API calls)
// ---------------------------------------------------------------------------

describe('chunkText', () => {
  it('splits text into chunks of specified size', () => {
    const text = 'a'.repeat(250);
    const chunks = chunkText(text, 100, 20);
    expect(chunks.length).toBe(3);
    expect(chunks[0].text).toHaveLength(100);
    expect(chunks[1].text).toHaveLength(100);
    // Last chunk is the remainder
    expect(chunks[2].text.length).toBeLessThanOrEqual(100);
  });

  it('preserves overlap between chunks', () => {
    const text = 'abcdefghijklmnopqrstuvwxyz'; // 26 chars
    const chunks = chunkText(text, 10, 3);
    // First chunk: 0-10, second chunk starts at 10-3=7
    expect(chunks[0].text).toBe('abcdefghij');
    expect(chunks[1].text.startsWith('hij')).toBe(true);
  });

  it('returns single chunk for short text', () => {
    const chunks = chunkText('short', 100, 20);
    expect(chunks).toHaveLength(1);
    expect(chunks[0].text).toBe('short');
    expect(chunks[0].position).toBe(0);
  });

  it('assigns sequential chunk_ids and positions', () => {
    const text = 'a'.repeat(250);
    const chunks = chunkText(text, 100, 0);
    expect(chunks[0].chunk_id).toBe('chunk_0');
    expect(chunks[0].position).toBe(0);
    expect(chunks[1].chunk_id).toBe('chunk_1');
    expect(chunks[1].position).toBe(100);
    expect(chunks[2].chunk_id).toBe('chunk_2');
    expect(chunks[2].position).toBe(200);
  });

  it('handles empty text', () => {
    const chunks = chunkText('', 100, 20);
    expect(chunks).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// createDocUploadTool
// ---------------------------------------------------------------------------

describe('createDocUploadTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createDocUploadTool(baseConfig);
    expect(tool.name).toBe('doc_upload');
  });

  it('uploads file content to Volume via Files API', async () => {
    vi.stubGlobal('fetch', mockFetchResponse({}));

    const tool = createDocUploadTool(baseConfig);
    const result = await tool.handler({
      filename: 'expert_bio.txt',
      content: 'Alice is an AI researcher with 10 years experience.',
    });

    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toContain('/api/2.0/fs/files/Volumes/main/kg/docs/');
    expect(url).toContain('expert_bio.txt');
    expect(opts.method).toBe('PUT');

    expect(result).toHaveProperty('doc_id');
    expect(result).toHaveProperty('path');
    expect(result.filename).toBe('expert_bio.txt');
  });

  it('throws when volumePath is not configured', () => {
    const noVolume = { ...baseConfig, volumePath: undefined };
    expect(() => createDocUploadTool(noVolume)).toThrow('volumePath');
  });
});

// ---------------------------------------------------------------------------
// createDocChunkTool
// ---------------------------------------------------------------------------

describe('createDocChunkTool', () => {
  it('returns an AgentTool with correct name', () => {
    const tool = createDocChunkTool(baseConfig);
    expect(tool.name).toBe('doc_chunk');
  });

  it('chunks provided text using schema.yaml settings', async () => {
    const tool = createDocChunkTool(baseConfig);
    const longText = 'word '.repeat(50); // 250 chars
    const result = await tool.handler({ text: longText });

    expect(Array.isArray(result)).toBe(true);
    expect(result.length).toBeGreaterThan(1);
    expect(result[0]).toHaveProperty('chunk_id');
    expect(result[0]).toHaveProperty('text');
    expect(result[0]).toHaveProperty('position');
  });

  it('uses default chunk_size 1000 when no schema provided', async () => {
    const noSchema = { ...baseConfig, entitySchema: undefined };
    const tool = createDocChunkTool(noSchema);
    const shortText = 'a'.repeat(500);
    const result = await tool.handler({ text: shortText });
    // 500 chars < 1000 default → single chunk
    expect(result).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// createDocExtractEntitiesTool
// ---------------------------------------------------------------------------

describe('createDocExtractEntitiesTool', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns an AgentTool with correct name', () => {
    const tool = createDocExtractEntitiesTool(baseConfig);
    expect(tool.name).toBe('doc_extract_entities');
  });

  it('calls FMAPI with interpolated prompt from schema.yaml', async () => {
    const fmapiResponse = {
      choices: [{
        message: {
          content: JSON.stringify([
            { entity_type: 'Expert', expert_id: 'e1', name: 'Alice', domains: ['AI'] },
          ]),
        },
      }],
    };
    vi.stubGlobal('fetch', mockFetchResponse(fmapiResponse));

    const tool = createDocExtractEntitiesTool(baseConfig);
    const result = await tool.handler({
      chunks: [{ chunk_id: 'c0', text: 'Alice is an AI researcher.' }],
    });

    // Verify FMAPI was called
    const [url, opts] = (fetch as any).mock.calls[0];
    expect(url).toContain('/serving-endpoints/chat/completions');

    const body = JSON.parse(opts.body);
    const systemMsg = body.messages.find((m: any) => m.role === 'system');
    // Prompt should contain entity names from schema
    expect(systemMsg.content).toContain('Expert');
    expect(systemMsg.content).toContain('Project');

    expect(Array.isArray(result)).toBe(true);
  });

  it('uses default extraction model when not specified', async () => {
    const fmapiResponse = {
      choices: [{ message: { content: '[]' } }],
    };
    vi.stubGlobal('fetch', mockFetchResponse(fmapiResponse));

    const tool = createDocExtractEntitiesTool(baseConfig);
    await tool.handler({ chunks: [{ chunk_id: 'c0', text: 'test' }] });

    const body = JSON.parse((fetch as any).mock.calls[0][1].body);
    expect(body.model).toBe('databricks-claude-sonnet-4-6');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-doc-parser.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement doc-parser.ts**

```typescript
// ts/src/connectors/doc-parser.ts

import { z } from 'zod';
import { randomUUID } from 'node:crypto';
import { defineTool } from '../agent/tools.js';
import type { AgentTool } from '../agent/tools.js';
import type { ConnectorConfig } from './types.js';
import { resolveHost } from './types.js';

// ---------------------------------------------------------------------------
// Chunk type
// ---------------------------------------------------------------------------

export interface Chunk {
  chunk_id: string;
  text: string;
  position: number;
}

// ---------------------------------------------------------------------------
// chunkText — pure function, exported for direct use and testing
// ---------------------------------------------------------------------------

export function chunkText(text: string, chunkSize: number, chunkOverlap: number): Chunk[] {
  if (!text || text.length === 0) return [];

  const chunks: Chunk[] = [];
  let start = 0;

  while (start < text.length) {
    const end = Math.min(start + chunkSize, text.length);
    chunks.push({
      chunk_id: `chunk_${chunks.length}`,
      text: text.slice(start, end),
      position: start,
    });

    const nextStart = start + chunkSize - chunkOverlap;
    if (nextStart <= start) break; // prevent infinite loop if overlap >= size
    start = nextStart;

    if (start >= text.length) break;
  }

  return chunks;
}

// ---------------------------------------------------------------------------
// Prompt interpolation
// ---------------------------------------------------------------------------

function interpolatePrompt(
  template: string,
  entityNames: string[],
  entityFields: string[],
  chunkText: string,
): string {
  return template
    .replace('{entity_names}', entityNames.join(', '))
    .replace('{entity_fields}', entityFields.join('; '))
    .replace('{chunk_text}', chunkText);
}

function resolveToken(): string {
  return process.env.DATABRICKS_TOKEN ?? '';
}

// ---------------------------------------------------------------------------
// createDocUploadTool
// ---------------------------------------------------------------------------

export function createDocUploadTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);
  if (!config.volumePath) {
    throw new Error('volumePath must be set in ConnectorConfig for doc upload');
  }
  const volumePath = config.volumePath;

  return defineTool({
    name: 'doc_upload',
    description: `Upload a document to the ${volumePath} UC Volume.`,
    parameters: z.object({
      filename: z.string().describe('Name for the file in the Volume'),
      content: z.string().describe('File content as text'),
    }),
    handler: async ({ filename, content }) => {
      const token = resolveToken();
      const docId = randomUUID();
      const path = `${volumePath}/${docId}_${filename}`;
      // Strip leading slash for Files API path
      const apiPath = path.startsWith('/') ? path.slice(1) : path;

      const res = await fetch(`${host}/api/2.0/fs/files/${apiPath}`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/octet-stream',
        },
        body: content,
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Files API upload ${res.status}: ${errText}`);
      }

      return { doc_id: docId, path, filename, size: content.length };
    },
  });
}

// ---------------------------------------------------------------------------
// createDocChunkTool
// ---------------------------------------------------------------------------

export function createDocChunkTool(config: ConnectorConfig): AgentTool {
  const chunkSize = config.entitySchema?.extraction.chunk_size ?? 1000;
  const chunkOverlap = config.entitySchema?.extraction.chunk_overlap ?? 200;

  return defineTool({
    name: 'doc_chunk',
    description: `Split text into chunks (size=${chunkSize}, overlap=${chunkOverlap}) for processing.`,
    parameters: z.object({
      text: z.string().describe('Text content to chunk'),
    }),
    handler: async ({ text }) => {
      return chunkText(text, chunkSize, chunkOverlap);
    },
  });
}

// ---------------------------------------------------------------------------
// createDocExtractEntitiesTool
// ---------------------------------------------------------------------------

export function createDocExtractEntitiesTool(config: ConnectorConfig): AgentTool {
  const host = resolveHost(config.host);
  const schema = config.entitySchema;

  const entityNames = schema?.entities.map((e) => e.name) ?? [];
  const entityFields = schema?.entities.map(
    (e) => `${e.name}: ${e.fields.map((f) => f.name).join(', ')}`,
  ) ?? [];
  const promptTemplate = schema?.extraction.prompt_template ?? 'Extract entities from: {chunk_text}';

  return defineTool({
    name: 'doc_extract_entities',
    description: 'Extract structured entities from document chunks using LLM.',
    parameters: z.object({
      chunks: z.array(
        z.object({
          chunk_id: z.string(),
          text: z.string(),
        }),
      ).describe('Chunks to extract entities from'),
      model: z.string().optional().describe('Model to use (default: databricks-claude-sonnet-4-6)'),
    }),
    handler: async ({ chunks, model }) => {
      const token = resolveToken();
      const useModel = model ?? 'databricks-claude-sonnet-4-6';

      const allEntities: unknown[] = [];

      for (const chunk of chunks) {
        const prompt = interpolatePrompt(promptTemplate, entityNames, entityFields, chunk.text);

        const res = await fetch(`${host}/serving-endpoints/chat/completions`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            model: useModel,
            messages: [
              { role: 'system', content: prompt },
              {
                role: 'user',
                content: `Extract all ${entityNames.join(' and ')} entities from the text above. Return a JSON array of objects, each with an "entity_type" field.`,
              },
            ],
          }),
        });

        if (!res.ok) {
          const errText = await res.text();
          throw new Error(`FMAPI extraction ${res.status}: ${errText}`);
        }

        const data = (await res.json()) as {
          choices: Array<{ message: { content: string } }>;
        };

        const content = data.choices?.[0]?.message?.content ?? '[]';
        try {
          const parsed = JSON.parse(content);
          if (Array.isArray(parsed)) {
            allEntities.push(
              ...parsed.map((e: Record<string, unknown>) => ({ ...e, _chunk_id: chunk.chunk_id })),
            );
          }
        } catch {
          // LLM returned non-JSON — skip this chunk
        }
      }

      return allEntities;
    },
  });
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run tests/connectors-doc-parser.test.ts`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/ts
git add src/connectors/doc-parser.ts tests/connectors-doc-parser.test.ts
git commit -m "feat(connectors): add doc upload, chunking, and entity extraction tools"
```

---

### Task 5: Connector Index and Package Exports

**Files:**
- Create: `ts/src/connectors/index.ts`
- Modify: `ts/src/index.ts`

- [ ] **Step 1: Create the connector barrel export**

```typescript
// ts/src/connectors/index.ts

// Types
export type {
  ConnectorConfig,
  EntitySchema,
  EntityDef,
  EdgeDef,
  FieldDef,
  ExtractionConfig,
  FitnessConfig,
  EvolutionConfig,
  SqlParam,
  DbFetchOptions,
} from './types.js';

export { parseEntitySchema, resolveHost, buildSqlParams, dbFetch } from './types.js';

// Lakebase
export {
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
} from './lakebase.js';

// Vector Search
export {
  createVSQueryTool,
  createVSUpsertTool,
  createVSDeleteTool,
} from './vector-search.js';

// Doc Parser
export {
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
  chunkText,
} from './doc-parser.js';
```

- [ ] **Step 2: Add connector exports to the package entry point**

Add the following to the end of `ts/src/index.ts`, before any closing comments:

```typescript
// Connectors — domain tool factories for Lakebase, Vector Search, Doc Parser
export {
  // Types
  type ConnectorConfig,
  type EntitySchema,
  type EntityDef,
  type EdgeDef,
  type FieldDef,
  parseEntitySchema,
  // Lakebase
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
  // Vector Search
  createVSQueryTool,
  createVSUpsertTool,
  createVSDeleteTool,
  // Doc Parser
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
  chunkText,
} from './connectors/index.js';
```

- [ ] **Step 3: Run the full test suite to verify nothing is broken**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run`
Expected: All existing tests PASS + all new connector tests PASS (total ~277 tests)

- [ ] **Step 4: Run typecheck**

Run: `cd ~/Documents/apx-agent/ts && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/ts
git add src/connectors/index.ts src/index.ts
git commit -m "feat(connectors): add barrel exports and wire into package entry point"
```

---

### Task 6: Build Verification

- [ ] **Step 1: Run the build**

Run: `cd ~/Documents/apx-agent/ts && npm run build`
Expected: Build succeeds, `dist/` contains compiled connector modules

- [ ] **Step 2: Verify exports are accessible**

Run: `cd ~/Documents/apx-agent/ts && node -e "import('./dist/index.js').then(m => { console.log('Lakebase:', typeof m.createLakebaseQueryTool); console.log('VS:', typeof m.createVSQueryTool); console.log('Doc:', typeof m.createDocUploadTool); console.log('Schema:', typeof m.parseEntitySchema); })"`
Expected:
```
Lakebase: function
VS: function
Doc: function
Schema: function
```

- [ ] **Step 3: Run full test suite one final time**

Run: `cd ~/Documents/apx-agent/ts && npx vitest run`
Expected: All tests PASS

- [ ] **Step 4: Final commit with version bump**

```bash
cd ~/Documents/apx-agent/ts
# Bump patch version in package.json: 0.1.0 → 0.2.0 (minor — new connector feature)
npm version minor --no-git-tag-version
git add package.json
git commit -m "feat(connectors): v0.2.0 — Lakebase, Vector Search, and Doc Parser connectors

Generic domain tool factories for building schema-driven agents on Databricks.
Supports parameterized SQL, similarity search, doc upload/chunking/extraction.
All tools follow the defineTool() pattern with Zod schemas."
```

---

## What's Next

This plan covers **Phase 1 (Connectors)** from the spec. After these land, a second plan will cover:

- **Phase 2:** KG Agent App (schema-loader, 6 tools, RouterAgent, DABs deployment)
- **Phase 3:** Doc Agent App (5 tools, SequentialAgent pipeline)
- **Phase 4:** PII Agent App (watchdog bridge, PII detector, 5 tools)
- **Phase 5:** Workflow Orchestrator (Python, 3 tasks, evolutionary loop)
- **Phase 6:** Integration testing across all units

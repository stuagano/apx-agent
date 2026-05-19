/**
 * DeltaExampleStore — UC Delta-backed `ExampleStore`.
 *
 * Sibling to `DeltaMemoryStore`. Same four-way recall decision tree (vector
 * search delegation, embedded client-side, recency fallback). The
 * discriminating field embedded at write time is `input` (not `output`) —
 * recall matches "examples whose inputs look like this query."
 *
 * Table schema (auto-created when `autoCreate=true`):
 *
 *   apx_examples (
 *     id            STRING,
 *     agent_id      STRING,
 *     intent        STRING,
 *     input         STRING,
 *     output        STRING,
 *     score         FLOAT,
 *     tags          ARRAY<STRING>,
 *     embedding     ARRAY<FLOAT>,
 *     metadata      STRING,     -- JSON-encoded
 *     created_at    DOUBLE,
 *     updated_at    DOUBLE
 *   ) USING DELTA
 */

import { z } from 'zod';
import {
  applyExamplePatch,
  cosineSimilarity,
  isoNow,
  materializeExample,
  type Example,
  type ExampleFilter,
  type ExampleInput,
  type ExamplePatch,
  type ExampleResult,
  type ExampleStore,
  type FindSimilarOptions,
} from './example.js';
import type { EmbeddingFn } from './memory.js';
import type {
  SqlExecExecutor,
  SqlQueryExecutor,
  VectorSearchClient,
} from './memory-delta.js';

export type { SqlExecExecutor, SqlQueryExecutor, VectorSearchClient } from './memory-delta.js';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const optionsSchema = z.object({
  tableName: z.string().optional(),
  embeddingDim: z.number().int().positive().optional(),
  autoCreate: z.boolean().optional(),
  indexName: z.string().optional(),
});

export interface DeltaExampleStoreOptions {
  querySql: SqlQueryExecutor;
  execSql: SqlExecExecutor;
  embeddingFn?: EmbeddingFn;
  embeddingDim?: number;
  /** Fully-qualified UC table name. Default 'apx_examples'. */
  tableName?: string;
  autoCreate?: boolean;
  vectorSearch?: VectorSearchClient;
  indexName?: string;
}

// ---------------------------------------------------------------------------
// SQL helpers (duplicated here to keep example-delta self-contained for tree-shaking)
// ---------------------------------------------------------------------------

function sqlStr(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}

function sqlArrayStr(items: readonly string[] | undefined): string {
  if (!items || items.length === 0) return 'array()';
  return 'array(' + items.map(sqlStr).join(', ') + ')';
}

function sqlArrayFloat(items: readonly number[] | undefined): string {
  if (!items) return 'CAST(NULL AS ARRAY<FLOAT>)';
  if (items.length === 0) return 'array()';
  return 'array(' + items.map((n) => (Number.isFinite(n) ? String(n) : '0')).join(', ') + ')';
}

function isoToEpoch(iso: string | undefined): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms / 1000 : 0;
}

function epochToIso(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return new Date(0).toISOString();
  }
  return new Date(seconds * 1000).toISOString();
}

function rowToExample(row: Record<string, unknown>): Example {
  let metadata: Record<string, unknown> = {};
  const metaRaw = row.metadata;
  if (typeof metaRaw === 'string' && metaRaw.length > 0) {
    try {
      metadata = JSON.parse(metaRaw) as Record<string, unknown>;
    } catch {
      metadata = {};
    }
  } else if (metaRaw && typeof metaRaw === 'object') {
    metadata = metaRaw as Record<string, unknown>;
  }

  const tagsRaw = row.tags;
  const tags: string[] = Array.isArray(tagsRaw) ? tagsRaw.map((t) => String(t)) : [];

  const embRaw = row.embedding;
  const embedding: number[] | undefined = Array.isArray(embRaw)
    ? embRaw.map((n) => Number(n))
    : undefined;

  const scoreRaw = row.score;
  const score: number | undefined =
    scoreRaw === null || scoreRaw === undefined ? undefined : Number(scoreRaw);

  return Object.freeze({
    id: String(row.id ?? ''),
    agentId: String(row.agent_id ?? ''),
    intent: String(row.intent ?? 'default'),
    input: String(row.input ?? ''),
    output: String(row.output ?? ''),
    score,
    tags: Object.freeze(tags),
    embedding: embedding ? Object.freeze(embedding) : undefined,
    metadata: Object.freeze(metadata),
    createdAt: epochToIso(Number(row.created_at ?? 0)),
    updatedAt: epochToIso(Number(row.updated_at ?? 0)),
  });
}

// ---------------------------------------------------------------------------
// DeltaExampleStore
// ---------------------------------------------------------------------------

export class DeltaExampleStore implements ExampleStore {
  readonly tableName: string;
  private readonly querySql: SqlQueryExecutor;
  private readonly execSql: SqlExecExecutor;
  private readonly embeddingFn?: EmbeddingFn;
  private readonly embeddingDim?: number;
  private readonly autoCreate: boolean;
  private readonly vectorSearch?: VectorSearchClient;
  private readonly indexName?: string;
  private created = false;

  constructor(opts: DeltaExampleStoreOptions) {
    optionsSchema.parse({
      tableName: opts.tableName,
      embeddingDim: opts.embeddingDim,
      autoCreate: opts.autoCreate,
      indexName: opts.indexName,
    });
    this.tableName = opts.tableName ?? 'apx_examples';
    this.querySql = opts.querySql;
    this.execSql = opts.execSql;
    this.embeddingFn = opts.embeddingFn;
    this.embeddingDim = opts.embeddingDim;
    this.autoCreate = opts.autoCreate ?? true;
    this.vectorSearch = opts.vectorSearch;
    this.indexName = opts.indexName;
  }

  private async ensureTable(): Promise<void> {
    if (this.created || !this.autoCreate) return;
    this.created = true;
    const sql =
      `CREATE TABLE IF NOT EXISTS ${this.tableName} (` +
      `  id STRING,` +
      `  agent_id STRING,` +
      `  intent STRING,` +
      `  input STRING,` +
      `  output STRING,` +
      `  score FLOAT,` +
      `  tags ARRAY<STRING>,` +
      `  embedding ARRAY<FLOAT>,` +
      `  metadata STRING,` +
      `  created_at DOUBLE,` +
      `  updated_at DOUBLE` +
      `) USING DELTA`;
    try {
      await this.execSql(sql);
    } catch (e) {
      console.warn(
        `DeltaExampleStore: CREATE TABLE IF NOT EXISTS ${this.tableName} failed: ${String(e)}`,
      );
    }
  }

  private mergeSql(example: Example): string {
    const createdAt = isoToEpoch(example.createdAt);
    const updatedAt = isoToEpoch(example.updatedAt);
    const metaJson = JSON.stringify(example.metadata ?? {});
    const scoreLit =
      example.score === undefined
        ? 'CAST(NULL AS FLOAT)'
        : `CAST(${example.score} AS FLOAT)`;
    return (
      `MERGE INTO ${this.tableName} AS target ` +
      `USING (SELECT ` +
      `  ${sqlStr(example.id)} AS id, ` +
      `  ${sqlStr(example.agentId)} AS agent_id, ` +
      `  ${sqlStr(example.intent)} AS intent, ` +
      `  ${sqlStr(example.input)} AS input, ` +
      `  ${sqlStr(example.output)} AS output, ` +
      `  ${scoreLit} AS score, ` +
      `  ${sqlArrayStr(example.tags)} AS tags, ` +
      `  ${sqlArrayFloat(example.embedding as number[] | undefined)} AS embedding, ` +
      `  ${sqlStr(metaJson)} AS metadata, ` +
      `  ${createdAt} AS created_at, ` +
      `  ${updatedAt} AS updated_at` +
      `) AS src ` +
      `ON target.id = src.id ` +
      `WHEN MATCHED THEN UPDATE SET ` +
      `  agent_id = src.agent_id, ` +
      `  intent = src.intent, ` +
      `  input = src.input, ` +
      `  output = src.output, ` +
      `  score = src.score, ` +
      `  tags = src.tags, ` +
      `  embedding = src.embedding, ` +
      `  metadata = src.metadata, ` +
      `  updated_at = src.updated_at ` +
      `WHEN NOT MATCHED THEN INSERT (` +
      `  id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at` +
      `) VALUES (` +
      `  src.id, src.agent_id, src.intent, src.input, src.output, src.score, src.tags, src.embedding, src.metadata, src.created_at, src.updated_at` +
      `)`
    );
  }

  async add(input: ExampleInput): Promise<Example> {
    await this.ensureTable();
    const [embedding] = this.embeddingFn
      ? await this.embeddingFn([input.input])
      : [undefined];
    const example = materializeExample(input, embedding);
    await this.execSql(this.mergeSql(example));
    return example;
  }

  async addBatch(inputs: readonly ExampleInput[]): Promise<Example[]> {
    if (inputs.length === 0) return [];
    await this.ensureTable();
    const embeddings = this.embeddingFn
      ? await this.embeddingFn(inputs.map((i) => i.input))
      : inputs.map(() => undefined as unknown as number[]);
    const out: Example[] = [];
    for (let i = 0; i < inputs.length; i++) {
      const example = materializeExample(inputs[i]!, embeddings[i]);
      await this.execSql(this.mergeSql(example));
      out.push(example);
    }
    return out;
  }

  async get(id: string): Promise<Example | null> {
    await this.ensureTable();
    const sql =
      `SELECT id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at ` +
      `FROM ${this.tableName} WHERE id = ${sqlStr(id)} LIMIT 1`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql);
    } catch (e) {
      console.warn(`DeltaExampleStore.get(${id}) failed: ${String(e)}`);
      return null;
    }
    if (!rows || rows.length === 0) return null;
    return rowToExample(rows[0]!);
  }

  async update(id: string, patch: ExamplePatch): Promise<Example | null> {
    const existing = await this.get(id);
    if (!existing) return null;
    let next = applyExamplePatch(existing, patch);
    if (patch.input !== undefined && this.embeddingFn) {
      const [emb] = await this.embeddingFn([patch.input]);
      next = Object.freeze({ ...next, embedding: emb ? Object.freeze([...emb]) : undefined });
    }
    next = Object.freeze({ ...next, updatedAt: isoNow() });
    await this.execSql(this.mergeSql(next));
    return next;
  }

  async delete(id: string): Promise<boolean> {
    await this.ensureTable();
    const sql = `DELETE FROM ${this.tableName} WHERE id = ${sqlStr(id)}`;
    try {
      await this.execSql(sql);
      return true;
    } catch (e) {
      console.warn(`DeltaExampleStore.delete(${id}) failed: ${String(e)}`);
      return false;
    }
  }

  async list(filter: ExampleFilter): Promise<Example[]> {
    await this.ensureTable();
    const limit = filter.limit ?? 100;
    const where: string[] = [`agent_id = ${sqlStr(filter.agentId)}`];
    if (filter.intent !== undefined) {
      where.push(`intent = ${sqlStr(filter.intent)}`);
    }
    if (filter.minScore !== undefined) {
      where.push(`score >= ${filter.minScore}`);
    }
    if (filter.tags && filter.tags.length > 0) {
      const arrLit = sqlArrayStr(filter.tags);
      where.push(`size(array_intersect(tags, ${arrLit})) > 0`);
    }
    const sql =
      `SELECT id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at ` +
      `FROM ${this.tableName} WHERE ${where.join(' AND ')} ` +
      `ORDER BY updated_at DESC LIMIT ${limit}`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql);
    } catch (e) {
      console.warn(`DeltaExampleStore.list failed: ${String(e)}`);
      return [];
    }
    return (rows ?? []).map(rowToExample);
  }

  async findSimilar(opts: FindSimilarOptions): Promise<ExampleResult[]> {
    const k = opts.k ?? 5;

    if (this.vectorSearch && this.indexName) {
      const filters: Record<string, unknown> = {
        agent_id: opts.agentId,
      };
      if (opts.intent !== undefined) {
        filters.intent = opts.intent;
      }
      const ssOpts: {
        indexName: string;
        queryVector?: number[];
        queryText?: string;
        columns: string[];
        numResults: number;
        filters?: Record<string, unknown>;
      } = {
        indexName: this.indexName,
        columns: ['id', 'agent_id', 'intent', 'input', 'output', 'score', 'tags', 'metadata', 'created_at', 'updated_at'],
        numResults: k,
        filters,
      };
      if (this.embeddingFn) {
        const [qv] = await this.embeddingFn([opts.query]);
        ssOpts.queryVector = qv;
      } else {
        ssOpts.queryText = opts.query;
      }
      const result = await this.vectorSearch.similaritySearch(ssOpts);
      return (result.rows ?? []).map((row) => {
        const scoreRaw = row._score ?? row.score ?? 0;
        return {
          example: rowToExample(row),
          score: Number(scoreRaw),
        };
      });
    }

    const candidates = await this.list({
      agentId: opts.agentId,
      intent: opts.intent,
      tags: opts.tags,
      minScore: opts.minScore,
      limit: 10_000,
    });

    if (!this.embeddingFn || candidates.length === 0) {
      return candidates.slice(0, k).map((example, idx) => ({
        example,
        score: candidates.length === 0 ? 0 : 1 - idx / Math.max(candidates.length, 1),
      }));
    }

    const [queryEmbedding] = await this.embeddingFn([opts.query]);
    const scored = candidates
      .filter((ex) => ex.embedding && ex.embedding.length > 0)
      .map((example) => ({
        example,
        score: cosineSimilarity(example.embedding!, queryEmbedding!),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
    return scored;
  }
}

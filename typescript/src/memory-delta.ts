/**
 * DeltaMemoryStore — UC Delta-backed `MemoryStore`.
 *
 * Persists per-principal memories to a Unity Catalog Delta table.
 *
 * Recall has a four-way decision tree:
 *
 *   1. `vectorSearch` + `embeddingFn` configured
 *        → caller-side embed of the query, pass `queryVector` to the
 *          injected `VectorSearchClient` (matches the embedder used at
 *          write time, avoiding embedder drift).
 *   2. `vectorSearch` only
 *        → pass `queryText` to the client, let the backend embed.
 *   3. `embeddingFn` only
 *        → list candidates via SQL with WHERE filters (principal,
 *          namespace, tags, minImportance), compute cosine similarity
 *          client-side, return top-k.
 *   4. Neither
 *        → recency fallback: ORDER BY updated_at DESC LIMIT k, score is
 *          normalized chronological position.
 *
 * The store takes the same injectable executor pair as `DeltaSessionStore`
 * (`querySql` + `execSql`) so the caller wires whatever Databricks SQL
 * transport they prefer. No SDK imports here — only injected callables.
 */

import { z } from 'zod';
import {
  cosineSimilarity,
  isoNow,
  materializeMemory,
  applyPatch,
  type EmbeddingFn,
  type Memory,
  type MemoryFilter,
  type MemoryInput,
  type MemoryPatch,
  type MemoryStore,
  type RecallOptions,
  type RecallResult,
} from './memory.js';

// ---------------------------------------------------------------------------
// Executor + VectorSearchClient types
// ---------------------------------------------------------------------------

/** Row-returning executor for SELECT statements (mirrors DeltaSessionStore). */
export type SqlQueryExecutor = (sql: string) => Promise<Array<Record<string, unknown>>>;

/** Side-effecting executor for CREATE/INSERT/MERGE/DELETE statements. */
export type SqlExecExecutor = (sql: string) => Promise<void>;

/**
 * Duck-typed Vector Search client. When provided, `.recall()` delegates
 * similarity search to the backend index instead of doing SQL + client-side
 * cosine. The shape matches Databricks Vector Search SDK conventions but
 * is intentionally an interface — any backend can be wired in.
 */
export interface VectorSearchClient {
  similaritySearch(opts: {
    indexName: string;
    queryVector?: number[];
    queryText?: string;
    columns: string[];
    numResults: number;
    filters?: Record<string, unknown>;
  }): Promise<{ rows: Array<Record<string, unknown>> }>;
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const optionsSchema = z.object({
  tableName: z.string().optional(),
  embeddingDim: z.number().int().positive().optional(),
  autoCreate: z.boolean().optional(),
  indexName: z.string().optional(),
});

export interface DeltaMemoryStoreOptions {
  querySql: SqlQueryExecutor;
  execSql: SqlExecExecutor;
  embeddingFn?: EmbeddingFn;
  /** Embedding dimensionality — informational; ARRAY<FLOAT> doesn't constrain length. */
  embeddingDim?: number;
  /** Fully-qualified UC table name. Default 'apx_memories'. */
  tableName?: string;
  autoCreate?: boolean;
  /** Optional Vector Search delegate. When set, .recall() delegates to this client. */
  vectorSearch?: VectorSearchClient;
  /** Vector Search index name. Required when vectorSearch is set. */
  indexName?: string;
}

// ---------------------------------------------------------------------------
// SQL helpers
// ---------------------------------------------------------------------------

/** SQL-escape a string literal — single quotes doubled. */
function sqlStr(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}

/** Render an ARRAY<STRING> Spark SQL literal. Empty arrays become array(). */
function sqlArrayStr(items: readonly string[] | undefined): string {
  if (!items || items.length === 0) return 'array()';
  return 'array(' + items.map(sqlStr).join(', ') + ')';
}

/** Render an ARRAY<FLOAT> Spark SQL literal. NULL when undefined. */
function sqlArrayFloat(items: readonly number[] | undefined): string {
  if (!items) return 'CAST(NULL AS ARRAY<FLOAT>)';
  if (items.length === 0) return 'array()';
  return 'array(' + items.map((n) => (Number.isFinite(n) ? String(n) : '0')).join(', ') + ')';
}

/** ISO string -> epoch seconds (DOUBLE). Returns 0 for blank/invalid. */
function isoToEpoch(iso: string | undefined): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms / 1000 : 0;
}

/** Epoch seconds -> ISO string. */
function epochToIso(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return new Date(0).toISOString();
  }
  return new Date(seconds * 1000).toISOString();
}

/** Convert raw driver row -> Memory. */
function rowToMemory(row: Record<string, unknown>): Memory {
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

  return Object.freeze({
    id: String(row.id ?? ''),
    principalId: String(row.principal_id ?? ''),
    namespace: String(row.namespace ?? 'default'),
    content: String(row.content ?? ''),
    tags: Object.freeze(tags),
    importance: Number(row.importance ?? 0.5),
    embedding: embedding ? Object.freeze(embedding) : undefined,
    metadata: Object.freeze(metadata),
    createdAt: epochToIso(Number(row.created_at ?? 0)),
    updatedAt: epochToIso(Number(row.updated_at ?? 0)),
  });
}

// ---------------------------------------------------------------------------
// DeltaMemoryStore
// ---------------------------------------------------------------------------

export class DeltaMemoryStore implements MemoryStore {
  readonly tableName: string;
  private readonly querySql: SqlQueryExecutor;
  private readonly execSql: SqlExecExecutor;
  private readonly embeddingFn?: EmbeddingFn;
  private readonly embeddingDim?: number;
  private readonly autoCreate: boolean;
  private readonly vectorSearch?: VectorSearchClient;
  private readonly indexName?: string;
  private created = false;

  constructor(opts: DeltaMemoryStoreOptions) {
    optionsSchema.parse({
      tableName: opts.tableName,
      embeddingDim: opts.embeddingDim,
      autoCreate: opts.autoCreate,
      indexName: opts.indexName,
    });
    this.tableName = opts.tableName ?? 'apx_memories';
    this.querySql = opts.querySql;
    this.execSql = opts.execSql;
    this.embeddingFn = opts.embeddingFn;
    this.embeddingDim = opts.embeddingDim;
    this.autoCreate = opts.autoCreate ?? true;
    this.vectorSearch = opts.vectorSearch;
    this.indexName = opts.indexName;
  }

  // -- DDL ----------------------------------------------------------------

  private async ensureTable(): Promise<void> {
    if (this.created || !this.autoCreate) return;
    this.created = true;
    const sql =
      `CREATE TABLE IF NOT EXISTS ${this.tableName} (` +
      `  id STRING,` +
      `  principal_id STRING,` +
      `  namespace STRING,` +
      `  content STRING,` +
      `  tags ARRAY<STRING>,` +
      `  importance FLOAT,` +
      `  embedding ARRAY<FLOAT>,` +
      `  metadata STRING,` +
      `  created_at DOUBLE,` +
      `  updated_at DOUBLE` +
      `) USING DELTA`;
    try {
      await this.execSql(sql);
    } catch (e) {
      console.warn(
        `DeltaMemoryStore: CREATE TABLE IF NOT EXISTS ${this.tableName} failed: ${String(e)} — ` +
          `subsequent operations may fail until the table exists.`,
      );
    }
  }

  // -- write helpers ------------------------------------------------------

  private mergeSql(memory: Memory): string {
    const createdAt = isoToEpoch(memory.createdAt);
    const updatedAt = isoToEpoch(memory.updatedAt);
    const metaJson = JSON.stringify(memory.metadata ?? {});
    return (
      `MERGE INTO ${this.tableName} AS target ` +
      `USING (SELECT ` +
      `  ${sqlStr(memory.id)} AS id, ` +
      `  ${sqlStr(memory.principalId)} AS principal_id, ` +
      `  ${sqlStr(memory.namespace)} AS namespace, ` +
      `  ${sqlStr(memory.content)} AS content, ` +
      `  ${sqlArrayStr(memory.tags)} AS tags, ` +
      `  CAST(${memory.importance} AS FLOAT) AS importance, ` +
      `  ${sqlArrayFloat(memory.embedding as number[] | undefined)} AS embedding, ` +
      `  ${sqlStr(metaJson)} AS metadata, ` +
      `  ${createdAt} AS created_at, ` +
      `  ${updatedAt} AS updated_at` +
      `) AS src ` +
      `ON target.id = src.id ` +
      `WHEN MATCHED THEN UPDATE SET ` +
      `  principal_id = src.principal_id, ` +
      `  namespace = src.namespace, ` +
      `  content = src.content, ` +
      `  tags = src.tags, ` +
      `  importance = src.importance, ` +
      `  embedding = src.embedding, ` +
      `  metadata = src.metadata, ` +
      `  updated_at = src.updated_at ` +
      `WHEN NOT MATCHED THEN INSERT (` +
      `  id, principal_id, namespace, content, tags, importance, embedding, metadata, created_at, updated_at` +
      `) VALUES (` +
      `  src.id, src.principal_id, src.namespace, src.content, src.tags, src.importance, src.embedding, src.metadata, src.created_at, src.updated_at` +
      `)`
    );
  }

  // -- MemoryStore protocol -----------------------------------------------

  async add(input: MemoryInput): Promise<Memory> {
    await this.ensureTable();
    const [embedding] = this.embeddingFn
      ? await this.embeddingFn([input.content])
      : [undefined];
    const memory = materializeMemory(input, embedding);
    await this.execSql(this.mergeSql(memory));
    return memory;
  }

  async addBatch(inputs: readonly MemoryInput[]): Promise<Memory[]> {
    if (inputs.length === 0) return [];
    await this.ensureTable();
    const embeddings = this.embeddingFn
      ? await this.embeddingFn(inputs.map((i) => i.content))
      : inputs.map(() => undefined as unknown as number[]);
    const out: Memory[] = [];
    for (let i = 0; i < inputs.length; i++) {
      const memory = materializeMemory(inputs[i]!, embeddings[i]);
      await this.execSql(this.mergeSql(memory));
      out.push(memory);
    }
    return out;
  }

  async get(id: string): Promise<Memory | null> {
    await this.ensureTable();
    const sql =
      `SELECT id, principal_id, namespace, content, tags, importance, embedding, metadata, created_at, updated_at ` +
      `FROM ${this.tableName} WHERE id = ${sqlStr(id)} LIMIT 1`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql);
    } catch (e) {
      console.warn(`DeltaMemoryStore.get(${id}) failed: ${String(e)} — returning null.`);
      return null;
    }
    if (!rows || rows.length === 0) return null;
    return rowToMemory(rows[0]!);
  }

  async update(id: string, patch: MemoryPatch): Promise<Memory | null> {
    const existing = await this.get(id);
    if (!existing) return null;
    let next = applyPatch(existing, patch);
    if (patch.content !== undefined && this.embeddingFn) {
      const [emb] = await this.embeddingFn([patch.content]);
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
      console.warn(`DeltaMemoryStore.delete(${id}) failed: ${String(e)}`);
      return false;
    }
  }

  async list(filter: MemoryFilter): Promise<Memory[]> {
    await this.ensureTable();
    const limit = filter.limit ?? 100;
    const where: string[] = [`principal_id = ${sqlStr(filter.principalId)}`];
    if (filter.namespace !== undefined) {
      where.push(`namespace = ${sqlStr(filter.namespace)}`);
    }
    if (filter.minImportance !== undefined) {
      where.push(`importance >= ${filter.minImportance}`);
    }
    if (filter.tags && filter.tags.length > 0) {
      // any-of: at least one filter tag must be in the row's tags array.
      const arrLit = sqlArrayStr(filter.tags);
      where.push(`size(array_intersect(tags, ${arrLit})) > 0`);
    }
    const sql =
      `SELECT id, principal_id, namespace, content, tags, importance, embedding, metadata, created_at, updated_at ` +
      `FROM ${this.tableName} WHERE ${where.join(' AND ')} ` +
      `ORDER BY updated_at DESC LIMIT ${limit}`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql);
    } catch (e) {
      console.warn(`DeltaMemoryStore.list failed: ${String(e)} — returning [].`);
      return [];
    }
    return (rows ?? []).map(rowToMemory);
  }

  async recall(opts: RecallOptions): Promise<RecallResult[]> {
    const k = opts.k ?? 5;

    // Branch 1 + 2: Vector Search delegation.
    if (this.vectorSearch && this.indexName) {
      const filters: Record<string, unknown> = {
        principal_id: opts.principalId,
      };
      if (opts.namespace !== undefined) {
        filters.namespace = opts.namespace;
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
        columns: ['id', 'principal_id', 'namespace', 'content', 'tags', 'importance', 'metadata', 'created_at', 'updated_at'],
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
          memory: rowToMemory(row),
          score: Number(scoreRaw),
        };
      });
    }

    // Branch 3 + 4: list candidates via SQL.
    const candidates = await this.list({
      principalId: opts.principalId,
      namespace: opts.namespace,
      tags: opts.tags,
      minImportance: opts.minImportance,
      limit: 10_000,
    });

    if (!this.embeddingFn || candidates.length === 0) {
      // Branch 4: recency fallback. list() already sorted by updated_at desc.
      return candidates.slice(0, k).map((memory, idx) => ({
        memory,
        score: candidates.length === 0 ? 0 : 1 - idx / Math.max(candidates.length, 1),
      }));
    }

    // Branch 3: client-side cosine.
    const [queryEmbedding] = await this.embeddingFn([opts.query]);
    const scored = candidates
      .filter((m) => m.embedding && m.embedding.length > 0)
      .map((memory) => ({
        memory,
        score: cosineSimilarity(memory.embedding!, queryEmbedding!),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
    return scored;
  }
}

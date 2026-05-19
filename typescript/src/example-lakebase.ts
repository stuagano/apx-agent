/**
 * LakebaseExampleStore — pgvector-backed `ExampleStore` over Databricks Lakebase.
 *
 * Sibling to `LakebaseMemoryStore`. Examples are scoped by `agentId` and
 * partitioned by `intent`. Embedding indexes the `input` field (the
 * discriminator queries are compared against) — `output` is stored verbatim
 * but not embedded.
 *
 * Schema (created on first use when `autoCreate=true`):
 *
 * ```sql
 * CREATE EXTENSION IF NOT EXISTS vector;
 * CREATE TABLE IF NOT EXISTS {table_name} (
 *   id          TEXT PRIMARY KEY,
 *   agent_id    TEXT NOT NULL,
 *   intent      TEXT NOT NULL DEFAULT 'default',
 *   input       TEXT NOT NULL,
 *   output      TEXT NOT NULL,
 *   score       REAL,
 *   tags        TEXT[] NOT NULL DEFAULT '{}',
 *   embedding   vector({embedding_dim}),
 *   metadata    JSONB NOT NULL DEFAULT '{}',
 *   created_at  DOUBLE PRECISION,
 *   updated_at  DOUBLE PRECISION
 * );
 * ```
 */

import { z } from 'zod';
import {
  applyExamplePatch,
  isoNow,
  materializeExample,
  newExampleId,
  type Example,
  type ExampleFilter,
  type ExampleInput,
  type ExamplePatch,
  type ExampleResult,
  type ExampleStore,
  type FindSimilarOptions,
} from './example.js';
import type { EmbeddingFn } from './memory.js';
import { tagsLiteral, vectorLiteral } from './memory-lakebase.js';
import type {
  PgExecExecutor,
  PgQueryExecutor,
} from './memory-lakebase.js';

// Re-export the executor types so callers using only example-lakebase have
// a single import target.
export type { PgExecExecutor, PgQueryExecutor } from './memory-lakebase.js';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const optionsSchema = z.object({
  tableName: z.string().min(1).optional(),
  autoCreate: z.boolean().optional(),
  ensureExtension: z.boolean().optional(),
  embeddingDim: z.number().int().positive(),
});

export interface LakebaseExampleStoreOptions {
  /** Row-returning executor for parameterized SELECT. */
  querySql: PgQueryExecutor;
  /** Side-effecting executor for parameterized DDL / DML. */
  execSql: PgExecExecutor;
  /** Required: embedding function. pgvector schema is fixed-width. */
  embeddingFn: EmbeddingFn;
  /** Required: dimensionality the embedder produces. Must match the `vector(N)` column. */
  embeddingDim: number;
  /** Table name (may include schema prefix). Defaults to `"apx_examples"`. */
  tableName?: string;
  /** Run `CREATE TABLE IF NOT EXISTS` and index DDL on first use. Defaults to true. */
  autoCreate?: boolean;
  /** Issue `CREATE EXTENSION IF NOT EXISTS vector` alongside the table DDL. Defaults to true. */
  ensureExtension?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers (parallel to memory-lakebase, intentionally duplicated to avoid
// reaching into another module's private API)
// ---------------------------------------------------------------------------

function parseVectorLiteral(raw: unknown): number[] | undefined {
  if (raw === null || raw === undefined) return undefined;
  if (Array.isArray(raw)) return raw.map((v) => Number(v));
  let s = String(raw).trim();
  if (s.length === 0) return undefined;
  if (s.startsWith('"') && s.endsWith('"')) s = s.slice(1, -1);
  if (s.startsWith('[') && s.endsWith(']')) s = s.slice(1, -1);
  if (s.length === 0) return [];
  return s.split(',').map((p) => Number(p.trim()));
}

function parseTagsLiteral(raw: unknown): string[] {
  if (raw === null || raw === undefined) return [];
  if (Array.isArray(raw)) return raw.map((v) => String(v));
  let s = String(raw).trim();
  if (s === '{}' || s.length === 0) return [];
  if (s.startsWith('{') && s.endsWith('}')) s = s.slice(1, -1);
  if (s.length === 0) return [];
  const out: string[] = [];
  let cur = '';
  let inQuote = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]!;
    if (ch === '\\' && i + 1 < s.length) {
      cur += s[i + 1];
      i += 1;
      continue;
    }
    if (ch === '"') {
      inQuote = !inQuote;
      continue;
    }
    if (ch === ',' && !inQuote) {
      out.push(cur);
      cur = '';
      continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

function parseMetadata(raw: unknown): Record<string, unknown> {
  if (raw === null || raw === undefined) return {};
  if (typeof raw === 'object') return raw as Record<string, unknown>;
  try {
    const v = JSON.parse(String(raw));
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function epochToIso(seconds: unknown): string {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return new Date(0).toISOString();
  return new Date(n * 1000).toISOString();
}

function isoToEpoch(iso: string | undefined): number {
  if (!iso) return Date.now() / 1000;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms / 1000 : Date.now() / 1000;
}

function rowToExample(row: Record<string, unknown>): Example {
  const embedding = parseVectorLiteral(row.embedding);
  return Object.freeze({
    id: String(row.id),
    agentId: String(row.agent_id),
    intent: String(row.intent ?? 'default'),
    input: String(row.input ?? ''),
    output: String(row.output ?? ''),
    score:
      row.score === null || row.score === undefined ? undefined : Number(row.score),
    tags: Object.freeze(parseTagsLiteral(row.tags)),
    embedding: embedding ? Object.freeze([...embedding]) : undefined,
    metadata: Object.freeze(parseMetadata(row.metadata)),
    createdAt: epochToIso(row.created_at),
    updatedAt: epochToIso(row.updated_at),
  });
}

// ---------------------------------------------------------------------------
// LakebaseExampleStore
// ---------------------------------------------------------------------------

export class LakebaseExampleStore implements ExampleStore {
  readonly tableName: string;
  readonly embeddingDim: number;
  private readonly querySql: PgQueryExecutor;
  private readonly execSql: PgExecExecutor;
  private readonly embeddingFn: EmbeddingFn;
  private autoCreate: boolean;
  private ensureExtension: boolean;
  private ensurePromise: Promise<void> | null = null;

  constructor(options: LakebaseExampleStoreOptions) {
    optionsSchema.parse({
      tableName: options.tableName,
      autoCreate: options.autoCreate,
      ensureExtension: options.ensureExtension,
      embeddingDim: options.embeddingDim,
    });
    if (typeof options.embeddingFn !== 'function') {
      throw new Error(
        'LakebaseExampleStore: `embeddingFn` is required — pgvector schema demands embeddings at write time.',
      );
    }
    this.tableName = options.tableName ?? 'apx_examples';
    this.embeddingDim = options.embeddingDim;
    this.querySql = options.querySql;
    this.execSql = options.execSql;
    this.embeddingFn = options.embeddingFn;
    this.autoCreate = options.autoCreate ?? true;
    this.ensureExtension = options.ensureExtension ?? true;
  }

  // -- schema bootstrap -------------------------------------------------

  private async ensureSchema(): Promise<void> {
    if (!this.autoCreate) return;
    if (this.ensurePromise) return this.ensurePromise;
    this.ensurePromise = this._runDdl();
    return this.ensurePromise;
  }

  private async _runDdl(): Promise<void> {
    try {
      if (this.ensureExtension) {
        await this.execSql('CREATE EXTENSION IF NOT EXISTS vector', []);
      }
      const ddl =
        `CREATE TABLE IF NOT EXISTS ${this.tableName} (` +
        `  id TEXT PRIMARY KEY,` +
        `  agent_id TEXT NOT NULL,` +
        `  intent TEXT NOT NULL DEFAULT 'default',` +
        `  input TEXT NOT NULL,` +
        `  output TEXT NOT NULL,` +
        `  score REAL,` +
        `  tags TEXT[] NOT NULL DEFAULT '{}',` +
        `  embedding vector(${this.embeddingDim}),` +
        `  metadata JSONB NOT NULL DEFAULT '{}',` +
        `  created_at DOUBLE PRECISION,` +
        `  updated_at DOUBLE PRECISION` +
        `)`;
      await this.execSql(ddl, []);
      await this.execSql(
        `CREATE INDEX IF NOT EXISTS ${this.tableName.replace(/\./g, '_')}_agent_idx ` +
          `ON ${this.tableName} (agent_id)`,
        [],
      );
      await this.execSql(
        `CREATE INDEX IF NOT EXISTS ${this.tableName.replace(/\./g, '_')}_embedding_idx ` +
          `ON ${this.tableName} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`,
        [],
      );
    } catch (e) {
      console.warn(
        `LakebaseExampleStore: DDL bootstrap on ${this.tableName} failed: ${String(e)} — ` +
          `subsequent reads/writes may fail until the schema exists.`,
      );
    }
  }

  // -- internal row write -----------------------------------------------

  private async insertRow(example: Example, embedding: readonly number[]): Promise<void> {
    const sql =
      `INSERT INTO ${this.tableName} ` +
      `(id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at) ` +
      `VALUES ($1, $2, $3, $4, $5, $6, $7::text[], $8::vector, $9::jsonb, $10, $11)`;
    await this.execSql(sql, [
      example.id,
      example.agentId,
      example.intent,
      example.input,
      example.output,
      example.score ?? null,
      tagsLiteral(example.tags),
      vectorLiteral(embedding),
      JSON.stringify(example.metadata ?? {}),
      isoToEpoch(example.createdAt),
      isoToEpoch(example.updatedAt),
    ]);
  }

  // -- ExampleStore protocol --------------------------------------------

  async add(input: ExampleInput): Promise<Example> {
    await this.ensureSchema();
    const [embedding] = await this.embeddingFn([input.input]);
    if (!embedding || embedding.length !== this.embeddingDim) {
      throw new Error(
        `LakebaseExampleStore.add: embedder returned ${embedding?.length ?? 0} dims, ` +
          `expected ${this.embeddingDim}.`,
      );
    }
    const example = materializeExample(
      { ...input, id: input.id ?? newExampleId() },
      embedding,
    );
    await this.insertRow(example, embedding);
    return example;
  }

  async addBatch(inputs: readonly ExampleInput[]): Promise<Example[]> {
    if (inputs.length === 0) return [];
    await this.ensureSchema();
    const embeddings = await this.embeddingFn(inputs.map((i) => i.input));
    if (embeddings.length !== inputs.length) {
      throw new Error(
        `LakebaseExampleStore.addBatch: embedder returned ${embeddings.length} vectors, ` +
          `expected ${inputs.length}.`,
      );
    }
    const valueRows: string[] = [];
    const params: unknown[] = [];
    const out: Example[] = [];
    let p = 1;
    for (let i = 0; i < inputs.length; i++) {
      const emb = embeddings[i]!;
      if (!emb || emb.length !== this.embeddingDim) {
        throw new Error(
          `LakebaseExampleStore.addBatch: row ${i} embedder returned ${emb?.length ?? 0} dims, ` +
            `expected ${this.embeddingDim}.`,
        );
      }
      const example = materializeExample(
        { ...inputs[i]!, id: inputs[i]!.id ?? newExampleId() },
        emb,
      );
      out.push(example);
      valueRows.push(
        `($${p}, $${p + 1}, $${p + 2}, $${p + 3}, $${p + 4}, $${p + 5}, ` +
          `$${p + 6}::text[], $${p + 7}::vector, $${p + 8}::jsonb, $${p + 9}, $${p + 10})`,
      );
      p += 11;
      params.push(
        example.id,
        example.agentId,
        example.intent,
        example.input,
        example.output,
        example.score ?? null,
        tagsLiteral(example.tags),
        vectorLiteral(emb),
        JSON.stringify(example.metadata ?? {}),
        isoToEpoch(example.createdAt),
        isoToEpoch(example.updatedAt),
      );
    }
    const sql =
      `INSERT INTO ${this.tableName} ` +
      `(id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at) ` +
      `VALUES ${valueRows.join(', ')}`;
    await this.execSql(sql, params);
    return out;
  }

  async get(id: string): Promise<Example | null> {
    await this.ensureSchema();
    const sql =
      `SELECT id, agent_id, intent, input, output, score, tags, embedding, metadata, ` +
      `created_at, updated_at FROM ${this.tableName} WHERE id = $1 LIMIT 1`;
    try {
      const rows = await this.querySql(sql, [id]);
      if (!rows || rows.length === 0) return null;
      return rowToExample(rows[0]!);
    } catch (e) {
      console.warn(`LakebaseExampleStore.get(${id}) failed: ${String(e)} — returning null.`);
      return null;
    }
  }

  async update(id: string, patch: ExamplePatch): Promise<Example | null> {
    await this.ensureSchema();
    const existing = await this.get(id);
    if (!existing) return null;
    let next = applyExamplePatch(existing, patch);

    let embedding: readonly number[] | undefined = existing.embedding;
    if (patch.input !== undefined) {
      const [emb] = await this.embeddingFn([patch.input]);
      if (!emb || emb.length !== this.embeddingDim) {
        throw new Error(
          `LakebaseExampleStore.update: embedder returned ${emb?.length ?? 0} dims, ` +
            `expected ${this.embeddingDim}.`,
        );
      }
      embedding = emb;
      next = Object.freeze({ ...next, embedding: Object.freeze([...emb]) });
    }

    const sql =
      `UPDATE ${this.tableName} SET ` +
      `agent_id = $1, intent = $2, input = $3, output = $4, score = $5, ` +
      `tags = $6::text[], embedding = $7::vector, metadata = $8::jsonb, ` +
      `created_at = $9, updated_at = $10 WHERE id = $11`;
    await this.execSql(sql, [
      next.agentId,
      next.intent,
      next.input,
      next.output,
      next.score ?? null,
      tagsLiteral(next.tags),
      embedding ? vectorLiteral(embedding) : vectorLiteral(new Array(this.embeddingDim).fill(0)),
      JSON.stringify(next.metadata ?? {}),
      isoToEpoch(next.createdAt),
      isoToEpoch(next.updatedAt),
      id,
    ]);
    return next;
  }

  async delete(id: string): Promise<boolean> {
    await this.ensureSchema();
    const existed = (await this.get(id)) !== null;
    if (!existed) return false;
    const sql = `DELETE FROM ${this.tableName} WHERE id = $1`;
    try {
      await this.execSql(sql, [id]);
      return true;
    } catch (e) {
      console.warn(`LakebaseExampleStore.delete(${id}) failed: ${String(e)}`);
      return false;
    }
  }

  async list(filter: ExampleFilter): Promise<Example[]> {
    await this.ensureSchema();
    const limit = filter.limit ?? 100;
    const sql =
      `SELECT id, agent_id, intent, input, output, score, tags, embedding, metadata, ` +
      `created_at, updated_at FROM ${this.tableName} ` +
      `WHERE agent_id = $1 ` +
      `AND ($2::text IS NULL OR intent = $2) ` +
      `AND ($3::text[] IS NULL OR tags && $3::text[]) ` +
      `AND ($4::real IS NULL OR (score IS NOT NULL AND score >= $4)) ` +
      `ORDER BY updated_at DESC LIMIT $5`;
    const params: unknown[] = [
      filter.agentId,
      filter.intent ?? null,
      filter.tags && filter.tags.length > 0 ? tagsLiteral(filter.tags) : null,
      filter.minScore ?? null,
      limit,
    ];
    try {
      const rows = await this.querySql(sql, params);
      return rows.map((r) => rowToExample(r));
    } catch (e) {
      console.warn(`LakebaseExampleStore.list failed: ${String(e)} — returning [].`);
      return [];
    }
  }

  async findSimilar(opts: FindSimilarOptions): Promise<ExampleResult[]> {
    await this.ensureSchema();
    const k = opts.k ?? 5;
    const [queryEmbedding] = await this.embeddingFn([opts.query]);
    if (!queryEmbedding || queryEmbedding.length !== this.embeddingDim) {
      throw new Error(
        `LakebaseExampleStore.findSimilar: embedder returned ${queryEmbedding?.length ?? 0} dims, ` +
          `expected ${this.embeddingDim}.`,
      );
    }
    const sql =
      `SELECT id, agent_id, intent, input, output, score, tags, embedding, metadata, ` +
      `created_at, updated_at, 1 - (embedding <=> $1::vector) AS similarity ` +
      `FROM ${this.tableName} ` +
      `WHERE agent_id = $2 ` +
      `AND ($3::text IS NULL OR intent = $3) ` +
      `AND ($4::text[] IS NULL OR tags && $4::text[]) ` +
      `AND ($5::real IS NULL OR (score IS NOT NULL AND score >= $5)) ` +
      `AND embedding IS NOT NULL ` +
      `ORDER BY embedding <=> $1::vector LIMIT $6`;
    const params: unknown[] = [
      vectorLiteral(queryEmbedding),
      opts.agentId,
      opts.intent ?? null,
      opts.tags && opts.tags.length > 0 ? tagsLiteral(opts.tags) : null,
      opts.minScore ?? null,
      k,
    ];
    try {
      const rows = await this.querySql(sql, params);
      return rows.map((r) => ({
        example: rowToExample(r),
        score: Number(r.similarity ?? 0),
      }));
    } catch (e) {
      console.warn(`LakebaseExampleStore.findSimilar failed: ${String(e)} — returning [].`);
      return [];
    }
  }

  // -- internal test hooks ----------------------------------------------

  /** @internal — Test helper to disable auto-create. */
  _disableAutoCreate(): void {
    this.autoCreate = false;
    this.ensurePromise = Promise.resolve();
  }
}

export { isoNow };

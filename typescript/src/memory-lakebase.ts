/**
 * LakebaseMemoryStore — pgvector-backed `MemoryStore` over Databricks Lakebase.
 *
 * Sibling pattern to `LakebaseSessionStore`. The store takes injectable
 * Postgres-style executors (query + exec) so the package never imports a
 * driver. Vector recall uses the `pgvector` extension's cosine-distance
 * operator (`<=>`) and converts distance to a 0..1 similarity score on the
 * way back out.
 *
 * Schema (created on first use when `autoCreate=true`):
 *
 * ```sql
 * CREATE EXTENSION IF NOT EXISTS vector;
 * CREATE TABLE IF NOT EXISTS {table_name} (
 *   id            TEXT PRIMARY KEY,
 *   principal_id  TEXT NOT NULL,
 *   namespace     TEXT NOT NULL DEFAULT 'default',
 *   content       TEXT NOT NULL,
 *   tags          TEXT[] NOT NULL DEFAULT '{}',
 *   importance    REAL NOT NULL DEFAULT 0.5,
 *   embedding     vector({embedding_dim}),
 *   metadata      JSONB NOT NULL DEFAULT '{}',
 *   created_at    DOUBLE PRECISION,
 *   updated_at    DOUBLE PRECISION
 * );
 * ```
 *
 * Embeddings are **required**: the schema's `vector(N)` column is fixed
 * dimensionality and recall needs an embedder for the query side. The
 * constructor rejects callers that omit `embeddingFn` / `embeddingDim`.
 */

import { z } from 'zod';
import {
  applyPatch,
  isoNow,
  materializeMemory,
  newMemoryId,
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
// Executor types — positional `$N` parameter binding.
//
// Distinct from the named-param executors in `session-lakebase.ts` because
// pgvector requires explicit `$N::vector` casts that are awkward to express
// with named placeholders. Callers wire whichever transport they prefer
// (`pg`, `postgres`, custom) into a thin adapter that matches this shape.
// ---------------------------------------------------------------------------

/** Row-returning executor — runs a parameterized SELECT and returns rows. */
export type PgQueryExecutor = (
  sql: string,
  params: readonly unknown[],
) => Promise<Array<Record<string, unknown>>>;

/** Side-effecting executor — runs parameterized INSERT/UPDATE/DELETE/DDL. */
export type PgExecExecutor = (
  sql: string,
  params: readonly unknown[],
) => Promise<void>;

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const optionsSchema = z.object({
  tableName: z.string().min(1).optional(),
  autoCreate: z.boolean().optional(),
  ensureExtension: z.boolean().optional(),
  embeddingDim: z.number().int().positive(),
});

export interface LakebaseMemoryStoreOptions {
  /** Row-returning executor for parameterized SELECT. */
  querySql: PgQueryExecutor;
  /** Side-effecting executor for parameterized DDL / DML. */
  execSql: PgExecExecutor;
  /** Required: embedding function. pgvector schema is fixed-width so this is non-optional. */
  embeddingFn: EmbeddingFn;
  /** Required: dimensionality the embedder produces. Must match the `vector(N)` column. */
  embeddingDim: number;
  /** Table name (may include schema prefix). Defaults to `"apx_memories"`. */
  tableName?: string;
  /** Run `CREATE TABLE IF NOT EXISTS` and index DDL on first use. Defaults to true. */
  autoCreate?: boolean;
  /** Issue `CREATE EXTENSION IF NOT EXISTS vector` alongside the table DDL. Defaults to true. */
  ensureExtension?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Render a vector as pgvector's text literal — e.g. `[0.1,0.2,0.3]`. Passed
 * as a `text` parameter and cast `$N::vector` in the SQL so we don't have to
 * register a custom OID with the driver.
 *
 * NaN/Infinity coerce to 0 to keep the literal valid; callers should not
 * feed those in but defensiveness is cheap.
 */
export function vectorLiteral(emb: readonly number[]): string {
  const parts = new Array<string>(emb.length);
  for (let i = 0; i < emb.length; i++) {
    const v = emb[i]!;
    parts[i] = Number.isFinite(v) ? String(v) : '0';
  }
  return `[${parts.join(',')}]`;
}

/**
 * Render a tag list as a Postgres text-array literal — e.g. `{a,b,"c d"}`.
 * Tags containing the array specials (`,`, `{`, `}`, `"`, `\`, whitespace,
 * or `NULL`) are double-quoted with `\` and `"` escaped inside.
 *
 * Passed as `text` and cast `$N::text[]` in the SQL.
 */
export function tagsLiteral(tags: readonly string[]): string {
  if (tags.length === 0) return '{}';
  const parts = tags.map((tag) => {
    if (tag === '') return '""';
    // Postgres array element parser specials.
    const needsQuoting =
      /[,{}"\\\s]/.test(tag) || tag.toUpperCase() === 'NULL';
    if (!needsQuoting) return tag;
    const escaped = tag.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return `"${escaped}"`;
  });
  return `{${parts.join(',')}}`;
}

/**
 * Parse a pgvector text literal (`[0.1,0.2]` or `"[0.1,0.2]"`) back to a
 * number array. Tolerates null / empty.
 */
function parseVectorLiteral(raw: unknown): number[] | undefined {
  if (raw === null || raw === undefined) return undefined;
  let s = typeof raw === 'string' ? raw : String(raw);
  s = s.trim();
  if (s.length === 0) return undefined;
  // Some drivers return it as a real array already.
  if (Array.isArray(raw)) {
    return (raw as unknown[]).map((v) => Number(v));
  }
  if (s.startsWith('"') && s.endsWith('"')) s = s.slice(1, -1);
  if (s.startsWith('[') && s.endsWith(']')) s = s.slice(1, -1);
  if (s.length === 0) return [];
  return s.split(',').map((p) => Number(p.trim()));
}

/**
 * Parse a Postgres text-array literal back to a string list. Accepts either
 * the raw text form (`{a,b,"c d"}`) or a native string[] some drivers hand
 * back. Empty `{}` → `[]`.
 */
function parseTagsLiteral(raw: unknown): string[] {
  if (raw === null || raw === undefined) return [];
  if (Array.isArray(raw)) return raw.map((v) => String(v));
  let s = String(raw).trim();
  if (s === '{}' || s.length === 0) return [];
  if (s.startsWith('{') && s.endsWith('}')) s = s.slice(1, -1);
  if (s.length === 0) return [];
  // Split on commas not inside quoted elements.
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

/** Parse metadata, accepting JSON string (text column) or parsed object (JSONB). */
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

/** Reconstruct a `Memory` from a SELECT row. Embedding is parsed when present. */
function rowToMemory(row: Record<string, unknown>): Memory {
  const embedding = parseVectorLiteral(row.embedding);
  return Object.freeze({
    id: String(row.id),
    principalId: String(row.principal_id),
    namespace: String(row.namespace ?? 'default'),
    content: String(row.content ?? ''),
    tags: Object.freeze(parseTagsLiteral(row.tags)),
    importance: Number(row.importance ?? 0.5),
    embedding: embedding ? Object.freeze([...embedding]) : undefined,
    metadata: Object.freeze(parseMetadata(row.metadata)),
    createdAt: epochToIso(row.created_at),
    updatedAt: epochToIso(row.updated_at),
  });
}

// ---------------------------------------------------------------------------
// LakebaseMemoryStore
// ---------------------------------------------------------------------------

export class LakebaseMemoryStore implements MemoryStore {
  readonly tableName: string;
  readonly embeddingDim: number;
  private readonly querySql: PgQueryExecutor;
  private readonly execSql: PgExecExecutor;
  private readonly embeddingFn: EmbeddingFn;
  private autoCreate: boolean;
  private ensureExtension: boolean;
  private ensurePromise: Promise<void> | null = null;

  constructor(options: LakebaseMemoryStoreOptions) {
    optionsSchema.parse({
      tableName: options.tableName,
      autoCreate: options.autoCreate,
      ensureExtension: options.ensureExtension,
      embeddingDim: options.embeddingDim,
    });
    if (typeof options.embeddingFn !== 'function') {
      throw new Error(
        'LakebaseMemoryStore: `embeddingFn` is required — pgvector schema demands embeddings at write time.',
      );
    }
    this.tableName = options.tableName ?? 'apx_memories';
    this.embeddingDim = options.embeddingDim;
    this.querySql = options.querySql;
    this.execSql = options.execSql;
    this.embeddingFn = options.embeddingFn;
    this.autoCreate = options.autoCreate ?? true;
    this.ensureExtension = options.ensureExtension ?? true;
  }

  // -- schema bootstrap ---------------------------------------------------

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
        `  principal_id TEXT NOT NULL,` +
        `  namespace TEXT NOT NULL DEFAULT 'default',` +
        `  content TEXT NOT NULL,` +
        `  tags TEXT[] NOT NULL DEFAULT '{}',` +
        `  importance REAL NOT NULL DEFAULT 0.5,` +
        `  embedding vector(${this.embeddingDim}),` +
        `  metadata JSONB NOT NULL DEFAULT '{}',` +
        `  created_at DOUBLE PRECISION,` +
        `  updated_at DOUBLE PRECISION` +
        `)`;
      await this.execSql(ddl, []);
      await this.execSql(
        `CREATE INDEX IF NOT EXISTS ${this.tableName.replace(/\./g, '_')}_principal_idx ` +
          `ON ${this.tableName} (principal_id)`,
        [],
      );
      await this.execSql(
        `CREATE INDEX IF NOT EXISTS ${this.tableName.replace(/\./g, '_')}_embedding_idx ` +
          `ON ${this.tableName} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`,
        [],
      );
    } catch (e) {
      // Mirror session-lakebase: warn and let subsequent SQL surface the
      // failure to the caller in its own context.
      console.warn(
        `LakebaseMemoryStore: DDL bootstrap on ${this.tableName} failed: ${String(e)} — ` +
          `subsequent reads/writes may fail until the schema exists.`,
      );
    }
  }

  // -- internal: row read/write -----------------------------------------

  private async insertRow(memory: Memory, embedding: readonly number[]): Promise<void> {
    const sql =
      `INSERT INTO ${this.tableName} ` +
      `(id, principal_id, namespace, content, tags, importance, embedding, metadata, created_at, updated_at) ` +
      `VALUES ($1, $2, $3, $4, $5::text[], $6, $7::vector, $8::jsonb, $9, $10)`;
    await this.execSql(sql, [
      memory.id,
      memory.principalId,
      memory.namespace,
      memory.content,
      tagsLiteral(memory.tags),
      memory.importance,
      vectorLiteral(embedding),
      JSON.stringify(memory.metadata ?? {}),
      isoToEpoch(memory.createdAt),
      isoToEpoch(memory.updatedAt),
    ]);
  }

  // -- MemoryStore protocol ---------------------------------------------

  async add(input: MemoryInput): Promise<Memory> {
    await this.ensureSchema();
    const [embedding] = await this.embeddingFn([input.content]);
    if (!embedding || embedding.length !== this.embeddingDim) {
      throw new Error(
        `LakebaseMemoryStore.add: embedder returned ${embedding?.length ?? 0} dims, ` +
          `expected ${this.embeddingDim}.`,
      );
    }
    const memory = materializeMemory(
      { ...input, id: input.id ?? newMemoryId() },
      embedding,
    );
    await this.insertRow(memory, embedding);
    return memory;
  }

  async addBatch(inputs: readonly MemoryInput[]): Promise<Memory[]> {
    if (inputs.length === 0) return [];
    await this.ensureSchema();
    const embeddings = await this.embeddingFn(inputs.map((i) => i.content));
    if (embeddings.length !== inputs.length) {
      throw new Error(
        `LakebaseMemoryStore.addBatch: embedder returned ${embeddings.length} vectors, ` +
          `expected ${inputs.length}.`,
      );
    }

    // Build a single multi-row INSERT.
    const valueRows: string[] = [];
    const params: unknown[] = [];
    const out: Memory[] = [];
    let p = 1;
    for (let i = 0; i < inputs.length; i++) {
      const emb = embeddings[i]!;
      if (!emb || emb.length !== this.embeddingDim) {
        throw new Error(
          `LakebaseMemoryStore.addBatch: row ${i} embedder returned ${emb?.length ?? 0} dims, ` +
            `expected ${this.embeddingDim}.`,
        );
      }
      const memory = materializeMemory(
        { ...inputs[i]!, id: inputs[i]!.id ?? newMemoryId() },
        emb,
      );
      out.push(memory);
      valueRows.push(
        `($${p}, $${p + 1}, $${p + 2}, $${p + 3}, $${p + 4}::text[], $${p + 5}, ` +
          `$${p + 6}::vector, $${p + 7}::jsonb, $${p + 8}, $${p + 9})`,
      );
      p += 10;
      params.push(
        memory.id,
        memory.principalId,
        memory.namespace,
        memory.content,
        tagsLiteral(memory.tags),
        memory.importance,
        vectorLiteral(emb),
        JSON.stringify(memory.metadata ?? {}),
        isoToEpoch(memory.createdAt),
        isoToEpoch(memory.updatedAt),
      );
    }
    const sql =
      `INSERT INTO ${this.tableName} ` +
      `(id, principal_id, namespace, content, tags, importance, embedding, metadata, created_at, updated_at) ` +
      `VALUES ${valueRows.join(', ')}`;
    await this.execSql(sql, params);
    return out;
  }

  async get(id: string): Promise<Memory | null> {
    await this.ensureSchema();
    const sql =
      `SELECT id, principal_id, namespace, content, tags, importance, embedding, metadata, ` +
      `created_at, updated_at FROM ${this.tableName} WHERE id = $1 LIMIT 1`;
    try {
      const rows = await this.querySql(sql, [id]);
      if (!rows || rows.length === 0) return null;
      return rowToMemory(rows[0]!);
    } catch (e) {
      console.warn(`LakebaseMemoryStore.get(${id}) failed: ${String(e)} — returning null.`);
      return null;
    }
  }

  async update(id: string, patch: MemoryPatch): Promise<Memory | null> {
    await this.ensureSchema();
    const existing = await this.get(id);
    if (!existing) return null;
    let next = applyPatch(existing, patch);

    let embedding: readonly number[] | undefined = existing.embedding;
    if (patch.content !== undefined) {
      const [emb] = await this.embeddingFn([patch.content]);
      if (!emb || emb.length !== this.embeddingDim) {
        throw new Error(
          `LakebaseMemoryStore.update: embedder returned ${emb?.length ?? 0} dims, ` +
            `expected ${this.embeddingDim}.`,
        );
      }
      embedding = emb;
      next = Object.freeze({ ...next, embedding: Object.freeze([...emb]) });
    }

    // Full-row UPDATE — cheap, deterministic, avoids dynamic SET clause
    // bookkeeping. The schema is small.
    const sql =
      `UPDATE ${this.tableName} SET ` +
      `principal_id = $1, namespace = $2, content = $3, tags = $4::text[], ` +
      `importance = $5, embedding = $6::vector, metadata = $7::jsonb, ` +
      `created_at = $8, updated_at = $9 WHERE id = $10`;
    await this.execSql(sql, [
      next.principalId,
      next.namespace,
      next.content,
      tagsLiteral(next.tags),
      next.importance,
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
    // SELECT count by reading the row first; cheap and avoids a DELETE
    // RETURNING (some drivers misbehave with RETURNING on the exec path).
    const existed = (await this.get(id)) !== null;
    if (!existed) return false;
    const sql = `DELETE FROM ${this.tableName} WHERE id = $1`;
    try {
      await this.execSql(sql, [id]);
      return true;
    } catch (e) {
      console.warn(`LakebaseMemoryStore.delete(${id}) failed: ${String(e)}`);
      return false;
    }
  }

  async list(filter: MemoryFilter): Promise<Memory[]> {
    await this.ensureSchema();
    const limit = filter.limit ?? 100;
    const sql =
      `SELECT id, principal_id, namespace, content, tags, importance, embedding, metadata, ` +
      `created_at, updated_at FROM ${this.tableName} ` +
      `WHERE principal_id = $1 ` +
      `AND ($2::text IS NULL OR namespace = $2) ` +
      `AND ($3::text[] IS NULL OR tags && $3::text[]) ` +
      `AND ($4::real IS NULL OR importance >= $4) ` +
      `ORDER BY updated_at DESC LIMIT $5`;
    const params: unknown[] = [
      filter.principalId,
      filter.namespace ?? null,
      filter.tags && filter.tags.length > 0 ? tagsLiteral(filter.tags) : null,
      filter.minImportance ?? null,
      limit,
    ];
    try {
      const rows = await this.querySql(sql, params);
      return rows.map((r) => rowToMemory(r));
    } catch (e) {
      console.warn(`LakebaseMemoryStore.list failed: ${String(e)} — returning [].`);
      return [];
    }
  }

  async recall(opts: RecallOptions): Promise<RecallResult[]> {
    await this.ensureSchema();
    const k = opts.k ?? 5;
    const [queryEmbedding] = await this.embeddingFn([opts.query]);
    if (!queryEmbedding || queryEmbedding.length !== this.embeddingDim) {
      throw new Error(
        `LakebaseMemoryStore.recall: embedder returned ${queryEmbedding?.length ?? 0} dims, ` +
          `expected ${this.embeddingDim}.`,
      );
    }
    const sql =
      `SELECT id, principal_id, namespace, content, tags, importance, embedding, metadata, ` +
      `created_at, updated_at, 1 - (embedding <=> $1::vector) AS score ` +
      `FROM ${this.tableName} ` +
      `WHERE principal_id = $2 ` +
      `AND ($3::text IS NULL OR namespace = $3) ` +
      `AND ($4::text[] IS NULL OR tags && $4::text[]) ` +
      `AND ($5::real IS NULL OR importance >= $5) ` +
      `AND embedding IS NOT NULL ` +
      `ORDER BY embedding <=> $1::vector LIMIT $6`;
    const params: unknown[] = [
      vectorLiteral(queryEmbedding),
      opts.principalId,
      opts.namespace ?? null,
      opts.tags && opts.tags.length > 0 ? tagsLiteral(opts.tags) : null,
      opts.minImportance ?? null,
      k,
    ];
    try {
      const rows = await this.querySql(sql, params);
      return rows.map((r) => ({
        memory: rowToMemory(r),
        score: Number(r.score ?? 0),
      }));
    } catch (e) {
      console.warn(`LakebaseMemoryStore.recall failed: ${String(e)} — returning [].`);
      return [];
    }
  }

  // -- internal test hooks ----------------------------------------------

  /** @internal — Test helper to disable auto-create (mirrors session-lakebase). */
  _disableAutoCreate(): void {
    this.autoCreate = false;
    this.ensurePromise = Promise.resolve();
  }
}

// Re-export the helper isoNow for parity with memory.ts surface.
export { isoNow };

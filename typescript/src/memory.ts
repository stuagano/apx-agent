/**
 * memory — durable, semantically-retrievable memory for agents.
 *
 * Sibling pattern to `workflows/session.ts`:
 *   - Sessions persist per-conversation history + state.
 *   - Memories persist per-principal long-lived facts/observations across
 *     conversations, with semantic recall.
 *
 * A `MemoryStore` is keyed by `principalId` (user, customer, agent —
 * whatever identity the caller chooses) and `namespace` (e.g. 'profile',
 * 'episodic', 'semantic', or any custom bucket the agent designates).
 *
 * Recall is **vector-based by default**. When an `embeddingFn` is wired
 * into the store, `.add()` embeds the content at write time and
 * `.recall()` embeds the query at read time and ranks by cosine
 * similarity. Stores that don't have an embedder (or callers that don't
 * want semantic recall) can pass `embeddingFn: undefined` — `.recall()`
 * then degrades to tag-filtered chronological recency.
 *
 * Same injectable-dependency philosophy as the rest of the framework:
 * no `@databricks/embeddings` import inside the package; the caller
 * wires the actual embedding source (FMAPI, Vector Search endpoint,
 * OpenAI, a local model, a test fake) via the `EmbeddingFn` callable.
 */

import { randomUUID } from 'node:crypto';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * A single durable memory. Equivalent to a row in the store.
 *
 * `principalId` is the scoping key — usually a user ID, customer ID, or
 * agent ID. `namespace` is a free-form bucket the agent chooses (e.g.
 * `'profile'`, `'preference'`, `'episodic'`). `tags` allow finer-grained
 * filtering inside a namespace.
 *
 * `importance` is a 0..1 scalar the caller can use for ranking,
 * decay-over-time, or pruning. The store does not interpret it — it is
 * just stored.
 *
 * `embedding` is present when the store is backed by a vector backend
 * AND an `embeddingFn` was wired in. Callers should not depend on its
 * shape — it's an implementation detail of the store.
 */
export interface Memory {
  id: string;
  principalId: string;
  namespace: string;
  content: string;
  tags: readonly string[];
  importance: number;
  embedding?: readonly number[];
  metadata: Readonly<Record<string, unknown>>;
  createdAt: string;
  updatedAt: string;
}

/** Shape callers pass into `.add()`. The store fills in id + timestamps + embedding. */
export interface MemoryInput {
  principalId: string;
  content: string;
  namespace?: string;
  tags?: readonly string[];
  importance?: number;
  metadata?: Readonly<Record<string, unknown>>;
  /** Optional caller-supplied id. When omitted, a UUID is generated. */
  id?: string;
}

/** Filter shape for `.list()` — exact match on principalId, optional refinements. */
export interface MemoryFilter {
  principalId: string;
  namespace?: string;
  /** Match memories whose tags overlap with this set (any-of). */
  tags?: readonly string[];
  /** Minimum importance threshold. */
  minImportance?: number;
  /** Max rows. Default 100. */
  limit?: number;
}

/** Options for `.recall()`. */
export interface RecallOptions {
  principalId: string;
  query: string;
  namespace?: string;
  tags?: readonly string[];
  minImportance?: number;
  /** Top-k. Default 5. */
  k?: number;
}

/** A recall result — the memory plus its similarity score (when vector recall ran). */
export interface RecallResult {
  memory: Memory;
  /**
   * Cosine similarity 0..1. When the store has no embedder (recency fallback),
   * `score` is the chronological position normalized to 0..1, newer = higher.
   */
  score: number;
}

/**
 * Injectable embedding callable.
 *
 * Stores accept a batched embedding function so they can compute embeddings
 * for a list of memories on bulk-add or for a single query on recall. The
 * implementation may proxy to Databricks Foundation Model APIs, an OpenAI
 * client, a local model, or a test fake.
 *
 * Embedding dimensionality is determined by the function. Stores that
 * persist embeddings (Lakebase pgvector, Delta) must be configured with the
 * same `embeddingDim` the function actually returns.
 */
export type EmbeddingFn = (texts: readonly string[]) => Promise<number[][]>;

/**
 * Common store interface. All backends implement this shape.
 *
 * `recall()` is the load-bearing read path. `list()` is the unranked
 * tag-filter path. `update()` is intentionally narrow — only the
 * mutable surface (`content`, `tags`, `importance`, `metadata`,
 * `namespace`); changing `principalId` or `id` requires delete-then-add.
 */
export interface MemoryStore {
  add(memory: MemoryInput): Promise<Memory>;
  addBatch(memories: readonly MemoryInput[]): Promise<Memory[]>;
  get(id: string): Promise<Memory | null>;
  update(id: string, patch: MemoryPatch): Promise<Memory | null>;
  delete(id: string): Promise<boolean>;
  list(filter: MemoryFilter): Promise<Memory[]>;
  recall(opts: RecallOptions): Promise<RecallResult[]>;
}

/** Mutable fields on a Memory. */
export interface MemoryPatch {
  content?: string;
  tags?: readonly string[];
  importance?: number;
  metadata?: Readonly<Record<string, unknown>>;
  namespace?: string;
}

// ---------------------------------------------------------------------------
// Pure helpers exported for backends
// ---------------------------------------------------------------------------

/**
 * Cosine similarity between two vectors. Returns 0 when either is empty
 * or all-zero. Used by `InMemoryMemoryStore` and exported so backends
 * with client-side ranking (Delta + Vector Search post-filter, in-memory
 * tests) share the same math.
 */
export function cosineSimilarity(
  a: readonly number[],
  b: readonly number[],
): number {
  if (a.length === 0 || a.length !== b.length) return 0;
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  if (normA === 0 || normB === 0) return 0;
  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

/** ISO-now helper. Backends should call this once per write, not per field. */
export function isoNow(): string {
  return new Date().toISOString();
}

/** Generate a memory id. Backends accept caller-supplied ids; fall back to UUID. */
export function newMemoryId(prefix = 'mem'): string {
  return `${prefix}_${randomUUID()}`;
}

/**
 * Normalize a `MemoryInput` to a fully-populated `Memory` (sans embedding).
 * Backends call this to centralize defaulting + id minting.
 */
export function materializeMemory(
  input: MemoryInput,
  embedding?: readonly number[],
): Memory {
  const now = isoNow();
  return Object.freeze({
    id: input.id ?? newMemoryId(),
    principalId: input.principalId,
    namespace: input.namespace ?? 'default',
    content: input.content,
    tags: Object.freeze([...(input.tags ?? [])]),
    importance: input.importance ?? 0.5,
    embedding: embedding ? Object.freeze([...embedding]) : undefined,
    metadata: Object.freeze({ ...(input.metadata ?? {}) }),
    createdAt: now,
    updatedAt: now,
  });
}

/** Apply a `MemoryPatch` to an existing memory, bumping updatedAt. */
export function applyPatch(memory: Memory, patch: MemoryPatch): Memory {
  return Object.freeze({
    ...memory,
    ...(patch.content !== undefined ? { content: patch.content } : {}),
    ...(patch.tags !== undefined
      ? { tags: Object.freeze([...patch.tags]) }
      : {}),
    ...(patch.importance !== undefined ? { importance: patch.importance } : {}),
    ...(patch.metadata !== undefined
      ? { metadata: Object.freeze({ ...patch.metadata }) }
      : {}),
    ...(patch.namespace !== undefined ? { namespace: patch.namespace } : {}),
    updatedAt: isoNow(),
  });
}

/**
 * Tag overlap check — true when the memory has at least one of the filter
 * tags (any-of semantics). Empty filter → matches everything.
 */
export function tagsOverlap(
  memoryTags: readonly string[],
  filterTags: readonly string[] | undefined,
): boolean {
  if (!filterTags || filterTags.length === 0) return true;
  const set = new Set(memoryTags);
  for (const t of filterTags) {
    if (set.has(t)) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// InMemoryMemoryStore — baseline for dev, tests, and fallback
// ---------------------------------------------------------------------------

/**
 * In-process store. Cosine-similarity recall when an embedder is wired,
 * recency fallback otherwise. Data is lost on restart. Suitable for
 * tests and local agent development.
 */
export class InMemoryMemoryStore implements MemoryStore {
  private rows = new Map<string, Memory>();
  private embeddingFn?: EmbeddingFn;

  constructor(opts: { embeddingFn?: EmbeddingFn } = {}) {
    this.embeddingFn = opts.embeddingFn;
  }

  async add(input: MemoryInput): Promise<Memory> {
    const [embedding] = this.embeddingFn
      ? await this.embeddingFn([input.content])
      : [undefined];
    const memory = materializeMemory(input, embedding);
    this.rows.set(memory.id, memory);
    return memory;
  }

  async addBatch(inputs: readonly MemoryInput[]): Promise<Memory[]> {
    if (inputs.length === 0) return [];
    const embeddings = this.embeddingFn
      ? await this.embeddingFn(inputs.map((i) => i.content))
      : inputs.map(() => undefined as unknown as number[]);
    const out: Memory[] = [];
    for (let i = 0; i < inputs.length; i++) {
      const memory = materializeMemory(inputs[i], embeddings[i]);
      this.rows.set(memory.id, memory);
      out.push(memory);
    }
    return out;
  }

  async get(id: string): Promise<Memory | null> {
    return this.rows.get(id) ?? null;
  }

  async update(id: string, patch: MemoryPatch): Promise<Memory | null> {
    const existing = this.rows.get(id);
    if (!existing) return null;
    let next = applyPatch(existing, patch);
    if (patch.content !== undefined && this.embeddingFn) {
      const [emb] = await this.embeddingFn([patch.content]);
      next = Object.freeze({ ...next, embedding: Object.freeze([...emb]) });
    }
    this.rows.set(id, next);
    return next;
  }

  async delete(id: string): Promise<boolean> {
    return this.rows.delete(id);
  }

  async list(filter: MemoryFilter): Promise<Memory[]> {
    const limit = filter.limit ?? 100;
    const out: Memory[] = [];
    for (const m of this.rows.values()) {
      if (m.principalId !== filter.principalId) continue;
      if (filter.namespace !== undefined && m.namespace !== filter.namespace) continue;
      if (!tagsOverlap(m.tags, filter.tags)) continue;
      if (filter.minImportance !== undefined && m.importance < filter.minImportance) continue;
      out.push(m);
    }
    out.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    return out.slice(0, limit);
  }

  async recall(opts: RecallOptions): Promise<RecallResult[]> {
    const k = opts.k ?? 5;

    const candidates = await this.list({
      principalId: opts.principalId,
      namespace: opts.namespace,
      tags: opts.tags,
      minImportance: opts.minImportance,
      limit: 10_000,
    });

    if (!this.embeddingFn || candidates.length === 0) {
      // Recency fallback — list() already sorted by updatedAt desc.
      return candidates.slice(0, k).map((memory, idx) => ({
        memory,
        score: candidates.length === 0 ? 0 : 1 - idx / Math.max(candidates.length, 1),
      }));
    }

    const [queryEmbedding] = await this.embeddingFn([opts.query]);
    const scored = candidates
      .filter((m) => m.embedding && m.embedding.length > 0)
      .map((memory) => ({
        memory,
        score: cosineSimilarity(memory.embedding!, queryEmbedding),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
    return scored;
  }
}

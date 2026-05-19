/**
 * example — durable, semantically-retrievable few-shot examples for agents.
 *
 * Sibling to `memory.ts`. Where `MemoryStore` persists per-principal
 * observations, `ExampleStore` persists per-agent input/output exemplars
 * used for in-context learning, prompt assembly, and quality reference.
 *
 * Recall is **vector-based by default**, same pattern as memory: an
 * `embeddingFn` is wired into the store; `.add()` embeds the example's
 * `input` (the discriminating field — what the LLM compares the
 * incoming query against) at write time, and `.findSimilar()` embeds
 * the query and ranks by cosine similarity.
 *
 * Scope key is `agentId` — examples are bound to a specific agent
 * (typically the supervisor or a sub-agent's logical name). `intent` is
 * a free-form bucket the agent uses to partition the example space
 * (e.g. `'classify_intent'`, `'extract_fields'`, `'final_answer'`).
 */

import { randomUUID } from 'node:crypto';
import { cosineSimilarity, tagsOverlap, type EmbeddingFn } from './memory.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * A single few-shot example. `input` is what an incoming query is
 * compared against; `output` is the demonstration response.
 *
 * `score` is a 0..1 quality signal (human rating, eval result, etc.).
 * `tags` allow finer-grained filtering inside an intent.
 *
 * `embedding` is the vector of `input` (not of input+output), so recall
 * matches "examples whose inputs look like this query."
 */
export interface Example {
  id: string;
  agentId: string;
  intent: string;
  input: string;
  output: string;
  score?: number;
  tags: readonly string[];
  embedding?: readonly number[];
  metadata: Readonly<Record<string, unknown>>;
  createdAt: string;
  updatedAt: string;
}

/** Shape callers pass into `.add()`. The store fills in id + timestamps + embedding. */
export interface ExampleInput {
  agentId: string;
  input: string;
  output: string;
  intent?: string;
  score?: number;
  tags?: readonly string[];
  metadata?: Readonly<Record<string, unknown>>;
  id?: string;
}

/** Filter shape for `.list()` — exact match on agentId, optional refinements. */
export interface ExampleFilter {
  agentId: string;
  intent?: string;
  tags?: readonly string[];
  minScore?: number;
  limit?: number;
}

/** Options for `.findSimilar()`. */
export interface FindSimilarOptions {
  agentId: string;
  query: string;
  intent?: string;
  tags?: readonly string[];
  minScore?: number;
  k?: number;
}

/** A retrieval result — the example plus its similarity score. */
export interface ExampleResult {
  example: Example;
  score: number;
}

/** Common store interface. All backends implement this shape. */
export interface ExampleStore {
  add(example: ExampleInput): Promise<Example>;
  addBatch(examples: readonly ExampleInput[]): Promise<Example[]>;
  get(id: string): Promise<Example | null>;
  update(id: string, patch: ExamplePatch): Promise<Example | null>;
  delete(id: string): Promise<boolean>;
  list(filter: ExampleFilter): Promise<Example[]>;
  findSimilar(opts: FindSimilarOptions): Promise<ExampleResult[]>;
}

/** Mutable fields on an Example. Changing `input` triggers a re-embed. */
export interface ExamplePatch {
  input?: string;
  output?: string;
  score?: number;
  tags?: readonly string[];
  metadata?: Readonly<Record<string, unknown>>;
  intent?: string;
}

// ---------------------------------------------------------------------------
// Pure helpers exported for backends
// ---------------------------------------------------------------------------

export { cosineSimilarity, tagsOverlap } from './memory.js';

export function isoNow(): string {
  return new Date().toISOString();
}

export function newExampleId(prefix = 'ex'): string {
  return `${prefix}_${randomUUID()}`;
}

export function materializeExample(
  input: ExampleInput,
  embedding?: readonly number[],
): Example {
  const now = isoNow();
  return Object.freeze({
    id: input.id ?? newExampleId(),
    agentId: input.agentId,
    intent: input.intent ?? 'default',
    input: input.input,
    output: input.output,
    score: input.score,
    tags: Object.freeze([...(input.tags ?? [])]),
    embedding: embedding ? Object.freeze([...embedding]) : undefined,
    metadata: Object.freeze({ ...(input.metadata ?? {}) }),
    createdAt: now,
    updatedAt: now,
  });
}

export function applyExamplePatch(example: Example, patch: ExamplePatch): Example {
  return Object.freeze({
    ...example,
    ...(patch.input !== undefined ? { input: patch.input } : {}),
    ...(patch.output !== undefined ? { output: patch.output } : {}),
    ...(patch.score !== undefined ? { score: patch.score } : {}),
    ...(patch.tags !== undefined
      ? { tags: Object.freeze([...patch.tags]) }
      : {}),
    ...(patch.metadata !== undefined
      ? { metadata: Object.freeze({ ...patch.metadata }) }
      : {}),
    ...(patch.intent !== undefined ? { intent: patch.intent } : {}),
    updatedAt: isoNow(),
  });
}

// ---------------------------------------------------------------------------
// InMemoryExampleStore — baseline
// ---------------------------------------------------------------------------

export class InMemoryExampleStore implements ExampleStore {
  private rows = new Map<string, Example>();
  private embeddingFn?: EmbeddingFn;

  constructor(opts: { embeddingFn?: EmbeddingFn } = {}) {
    this.embeddingFn = opts.embeddingFn;
  }

  async add(input: ExampleInput): Promise<Example> {
    const [embedding] = this.embeddingFn
      ? await this.embeddingFn([input.input])
      : [undefined];
    const example = materializeExample(input, embedding);
    this.rows.set(example.id, example);
    return example;
  }

  async addBatch(inputs: readonly ExampleInput[]): Promise<Example[]> {
    if (inputs.length === 0) return [];
    const embeddings = this.embeddingFn
      ? await this.embeddingFn(inputs.map((i) => i.input))
      : inputs.map(() => undefined as unknown as number[]);
    const out: Example[] = [];
    for (let i = 0; i < inputs.length; i++) {
      const example = materializeExample(inputs[i], embeddings[i]);
      this.rows.set(example.id, example);
      out.push(example);
    }
    return out;
  }

  async get(id: string): Promise<Example | null> {
    return this.rows.get(id) ?? null;
  }

  async update(id: string, patch: ExamplePatch): Promise<Example | null> {
    const existing = this.rows.get(id);
    if (!existing) return null;
    let next = applyExamplePatch(existing, patch);
    if (patch.input !== undefined && this.embeddingFn) {
      const [emb] = await this.embeddingFn([patch.input]);
      next = Object.freeze({ ...next, embedding: Object.freeze([...emb]) });
    }
    this.rows.set(id, next);
    return next;
  }

  async delete(id: string): Promise<boolean> {
    return this.rows.delete(id);
  }

  async list(filter: ExampleFilter): Promise<Example[]> {
    const limit = filter.limit ?? 100;
    const out: Example[] = [];
    for (const ex of this.rows.values()) {
      if (ex.agentId !== filter.agentId) continue;
      if (filter.intent !== undefined && ex.intent !== filter.intent) continue;
      if (!tagsOverlap(ex.tags, filter.tags)) continue;
      if (
        filter.minScore !== undefined &&
        (ex.score === undefined || ex.score < filter.minScore)
      ) {
        continue;
      }
      out.push(ex);
    }
    out.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    return out.slice(0, limit);
  }

  async findSimilar(opts: FindSimilarOptions): Promise<ExampleResult[]> {
    const k = opts.k ?? 5;

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
        score: cosineSimilarity(example.embedding!, queryEmbedding),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
    return scored;
  }
}

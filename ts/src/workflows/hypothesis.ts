import { randomUUID } from 'node:crypto';

export interface Hypothesis {
  id: string;                          // randomUUID() truncated to 8 chars
  generation: number;
  parent_id: string | null;
  fitness: Record<string, number>;     // named fitness signals
  metadata: Record<string, unknown>;   // domain-specific fields
  flagged_for_review: boolean;
  created_at: string;                  // ISO timestamp
}

export function createHypothesis(opts: {
  generation: number;
  parent_id?: string;
  fitness?: Record<string, number>;
  metadata?: Record<string, unknown>;
}): Hypothesis {
  return {
    id: randomUUID().replace(/-/g, '').slice(0, 8),
    generation: opts.generation,
    parent_id: opts.parent_id ?? null,
    fitness: opts.fitness ?? {},
    metadata: opts.metadata ?? {},
    flagged_for_review: false,
    created_at: new Date().toISOString(),
  };
}

export function compositeFitness(
  h: Hypothesis,
  weights: Record<string, number>,
): number {
  const entries = Object.entries(weights);
  if (entries.length === 0) return 0;
  let sum = 0;
  for (const [key, weight] of entries) {
    sum += (h.fitness[key] ?? 0) * weight;
  }
  return sum;
}

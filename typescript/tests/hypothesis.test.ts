import { describe, it, expect } from 'vitest';
import { createHypothesis, compositeFitness } from '../src/workflows/hypothesis';

describe('createHypothesis', () => {
  it('produces an 8-character id', () => {
    const h = createHypothesis({ generation: 0 });
    expect(h.id).toHaveLength(8);
    expect(h.id).toMatch(/^[0-9a-f]{8}$/);
  });

  it('applies default values when only generation is supplied', () => {
    const h = createHypothesis({ generation: 1 });
    expect(h.generation).toBe(1);
    expect(h.parent_id).toBeNull();
    expect(h.fitness).toEqual({});
    expect(h.metadata).toEqual({});
    expect(h.flagged_for_review).toBe(false);
    expect(h.created_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it('stores parent_id, fitness, and metadata when provided', () => {
    const h = createHypothesis({
      generation: 2,
      parent_id: 'abc12345',
      fitness: { accuracy: 0.9 },
      metadata: { notes: 'test' },
    });
    expect(h.parent_id).toBe('abc12345');
    expect(h.fitness).toEqual({ accuracy: 0.9 });
    expect(h.metadata).toEqual({ notes: 'test' });
  });

  it('generates unique ids across calls', () => {
    const ids = Array.from({ length: 20 }, () => createHypothesis({ generation: 0 }).id);
    const unique = new Set(ids);
    expect(unique.size).toBe(20);
  });
});

describe('compositeFitness', () => {
  it('computes a weighted sum of fitness signals', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { accuracy: 0.8, speed: 0.5 },
    });
    const score = compositeFitness(h, { accuracy: 0.7, speed: 0.3 });
    expect(score).toBeCloseTo(0.8 * 0.7 + 0.5 * 0.3);
  });

  it('treats missing fitness keys as 0', () => {
    const h = createHypothesis({ generation: 0, fitness: { accuracy: 1.0 } });
    const score = compositeFitness(h, { accuracy: 0.5, speed: 0.5 });
    expect(score).toBeCloseTo(0.5);
  });

  it('handles normalization with equal weights summing to 1', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { a: 1, b: 1, c: 1 },
    });
    const score = compositeFitness(h, { a: 1 / 3, b: 1 / 3, c: 1 / 3 });
    expect(score).toBeCloseTo(1);
  });

  it('returns 0 for empty fitness and any weights', () => {
    const h = createHypothesis({ generation: 0 });
    expect(compositeFitness(h, { accuracy: 1 })).toBe(0);
  });

  it('returns 0 when weights map is empty', () => {
    const h = createHypothesis({ generation: 0, fitness: { accuracy: 0.9 } });
    expect(compositeFitness(h, {})).toBe(0);
  });
});

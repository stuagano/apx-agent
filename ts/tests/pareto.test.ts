/**
 * Tests for Pareto dominance, frontier detection, and survivor selection.
 */

import { describe, it, expect } from 'vitest';
import type { Hypothesis } from '../src/workflows/hypothesis.js';
import { paretoDominates, paretoFrontier, selectSurvivors } from '../src/workflows/pareto.js';

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

let _seq = 0;

/** Build a minimal Hypothesis with the given fitness map. */
function h(fitness: Record<string, number>, id?: string): Hypothesis {
  return {
    id: id ?? `h-${++_seq}`,
    generation: 0,
    parent_id: null,
    fitness,
    metadata: {},
    flagged_for_review: false,
    created_at: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// paretoDominates
// ---------------------------------------------------------------------------

describe('paretoDominates', () => {
  it('returns true when a is better on all objectives and strictly better on one', () => {
    const a = h({ accuracy: 0.9, speed: 0.8 });
    const b = h({ accuracy: 0.7, speed: 0.8 });
    expect(paretoDominates(a, b, ['accuracy', 'speed'])).toBe(true);
  });

  it('returns false when b is strictly better on one objective', () => {
    const a = h({ accuracy: 0.9, speed: 0.5 });
    const b = h({ accuracy: 0.7, speed: 0.9 });
    expect(paretoDominates(a, b, ['accuracy', 'speed'])).toBe(false);
  });

  it('returns false when both are equal on all objectives', () => {
    const a = h({ accuracy: 0.8, speed: 0.8 });
    const b = h({ accuracy: 0.8, speed: 0.8 });
    expect(paretoDominates(a, b, ['accuracy', 'speed'])).toBe(false);
  });

  it('treats missing fitness values as 0', () => {
    const a = h({ accuracy: 0.5 });   // speed missing → 0
    const b = h({ speed: 0.3 });       // accuracy missing → 0
    // a: accuracy=0.5, speed=0  vs  b: accuracy=0, speed=0.3
    // a is better on accuracy, worse on speed — neither dominates
    expect(paretoDominates(a, b, ['accuracy', 'speed'])).toBe(false);
    expect(paretoDominates(b, a, ['accuracy', 'speed'])).toBe(false);
  });

  it('works with a single objective', () => {
    const a = h({ score: 10 });
    const b = h({ score: 5 });
    expect(paretoDominates(a, b, ['score'])).toBe(true);
    expect(paretoDominates(b, a, ['score'])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// paretoFrontier
// ---------------------------------------------------------------------------

describe('paretoFrontier', () => {
  it('returns the single element for a one-member population', () => {
    const population = [h({ accuracy: 0.9 })];
    expect(paretoFrontier(population, ['accuracy'])).toHaveLength(1);
  });

  it('identifies the non-dominated set among four hypotheses', () => {
    // Objective space: accuracy vs speed
    //  A: (0.9, 0.3) — high accuracy, low speed
    //  B: (0.4, 0.9) — low accuracy, high speed
    //  C: (0.5, 0.5) — middle (dominated by D and possibly others)
    //  D: (0.7, 0.7) — dominates C
    const A = h({ accuracy: 0.9, speed: 0.3 }, 'A');
    const B = h({ accuracy: 0.4, speed: 0.9 }, 'B');
    const C = h({ accuracy: 0.5, speed: 0.5 }, 'C');
    const D = h({ accuracy: 0.7, speed: 0.7 }, 'D');

    const frontier = paretoFrontier([A, B, C, D], ['accuracy', 'speed']);
    const ids = frontier.map((x) => x.id).sort();

    // A, B, D are non-dominated. C is dominated by D.
    expect(ids).toEqual(['A', 'B', 'D']);
  });

  it('returns empty array for an empty population', () => {
    expect(paretoFrontier([], ['accuracy'])).toEqual([]);
  });

  it('returns all members when none dominates any other', () => {
    // Each is better on exactly one objective → no one dominates
    const a = h({ x: 1, y: 0 }, 'a');
    const b = h({ x: 0, y: 1 }, 'b');
    const frontier = paretoFrontier([a, b], ['x', 'y']);
    expect(frontier).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// selectSurvivors
// ---------------------------------------------------------------------------

describe('selectSurvivors', () => {
  it('returns at most maxSize members', () => {
    const population = [
      h({ q: 0.9 }, 'p1'),
      h({ q: 0.8 }, 'p2'),
      h({ q: 0.7 }, 'p3'),
      h({ q: 0.6 }, 'p4'),
      h({ q: 0.5 }, 'p5'),
    ];
    const survivors = selectSurvivors(population, ['q'], { q: 1 }, 3);
    expect(survivors.length).toBeLessThanOrEqual(3);
  });

  it('prefers frontier members over non-frontier members', () => {
    // Frontier: p1 (0.9, 0.9)
    // Non-frontier: p2 (0.5, 0.5) and p3 (0.4, 0.4) — both dominated by p1
    const p1 = h({ a: 0.9, b: 0.9 }, 'p1');  // frontier
    const p2 = h({ a: 0.5, b: 0.5 }, 'p2');  // non-frontier
    const p3 = h({ a: 0.4, b: 0.4 }, 'p3');  // non-frontier

    const survivors = selectSurvivors([p1, p2, p3], ['a', 'b'], { a: 1, b: 1 }, 2);
    const ids = survivors.map((x) => x.id);
    expect(ids).toContain('p1');
  });

  it('fills remaining slots from non-frontier ordered by composite fitness', () => {
    const frontierMember = h({ a: 0.9, b: 0.1 }, 'f1');  // frontier
    const highFit       = h({ a: 0.6, b: 0.6 }, 'nf1'); // non-frontier, higher composite
    const lowFit        = h({ a: 0.1, b: 0.1 }, 'nf2'); // non-frontier, lower composite
    // f1 dominates nf1 and nf2 (a=0.9 >= both, b=0.1 < nf1's b=0.6 — wait, let's pick dominated ones)
    // Use three-objective scenario so f1 clearly dominates nf* on all:
    const f1  = h({ a: 0.9, b: 0.9 }, 'f1');
    const nf1 = h({ a: 0.5, b: 0.5 }, 'nf1');  // dominated by f1, composite = 1.0
    const nf2 = h({ a: 0.2, b: 0.2 }, 'nf2');  // dominated by f1, composite = 0.4

    // maxSize=2: f1 + one non-frontier slot → should pick nf1 (higher composite)
    const survivors = selectSurvivors([f1, nf1, nf2], ['a', 'b'], { a: 1, b: 1 }, 2);
    const ids = survivors.map((x) => x.id);
    expect(ids).toContain('f1');
    expect(ids).toContain('nf1');
    expect(ids).not.toContain('nf2');
  });

  it('returns all members when population size <= maxSize', () => {
    const population = [h({ q: 0.5 }, 'p1'), h({ q: 0.3 }, 'p2')];
    const survivors = selectSurvivors(population, ['q'], { q: 1 }, 10);
    expect(survivors).toHaveLength(2);
  });
});

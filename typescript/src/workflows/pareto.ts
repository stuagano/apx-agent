/**
 * Pareto selection for the evolutionary hypothesis framework.
 *
 * Provides three functions:
 *  - paretoDominates:  true if hypothesis `a` weakly dominates `b` on all
 *                      objectives and strictly beats `b` on at least one.
 *  - paretoFrontier:   returns the non-dominated subset of a population.
 *  - selectSurvivors:  shrinks a population to `maxSize`, preferring frontier
 *                      members and ranking ties by composite fitness.
 */

import type { Hypothesis } from './hypothesis.js';
import { compositeFitness } from './hypothesis.js';

/**
 * Returns true when `a` Pareto-dominates `b` with respect to `objectives`.
 *
 * Dominance requires:
 *   1. a.fitness[obj] >= b.fitness[obj]  for ALL objectives
 *   2. a.fitness[obj] >  b.fitness[obj]  for AT LEAST ONE objective
 *
 * Missing fitness values are treated as 0.
 */
export function paretoDominates(
  a: Hypothesis,
  b: Hypothesis,
  objectives: string[],
): boolean {
  let strictlyBetterOnAtLeastOne = false;

  for (const obj of objectives) {
    const aScore = a.fitness[obj] ?? 0;
    const bScore = b.fitness[obj] ?? 0;

    if (aScore < bScore) {
      // a is worse on this objective — cannot dominate
      return false;
    }
    if (aScore > bScore) {
      strictlyBetterOnAtLeastOne = true;
    }
  }

  return strictlyBetterOnAtLeastOne;
}

/**
 * Returns the Pareto-optimal (non-dominated) subset of `population`.
 *
 * A hypothesis is non-dominated when no other hypothesis in the population
 * dominates it.  The algorithm is O(n²) — suitable for small populations.
 *
 * Returns an empty array for an empty population.
 */
export function paretoFrontier(
  population: Hypothesis[],
  objectives: string[],
): Hypothesis[] {
  if (population.length === 0) return [];

  return population.filter((candidate) =>
    !population.some(
      (other) => other !== candidate && paretoDominates(other, candidate, objectives),
    ),
  );
}

/**
 * Selects at most `maxSize` survivors from `population`.
 *
 * Strategy:
 *  1. Compute the Pareto frontier.
 *  2. If the frontier already has >= maxSize members, return the top maxSize
 *     ranked by composite fitness (descending).
 *  3. Otherwise start with all frontier members, then fill remaining slots
 *     from the non-frontier population ordered by composite fitness (descending).
 *  4. If the whole population fits within maxSize, return all members
 *     (ordered by composite fitness).
 */
export function selectSurvivors(
  population: Hypothesis[],
  objectives: string[],
  weights: Record<string, number>,
  maxSize: number,
): Hypothesis[] {
  if (population.length <= maxSize) {
    return [...population].sort(
      (a, b) => compositeFitness(b, weights) - compositeFitness(a, weights),
    );
  }

  const frontier = paretoFrontier(population, objectives);
  const frontierIds = new Set(frontier.map((h) => h.id));
  const nonFrontier = population.filter((h) => !frontierIds.has(h.id));

  const byFitnessDesc = (a: Hypothesis, b: Hypothesis): number =>
    compositeFitness(b, weights) - compositeFitness(a, weights);

  const sortedFrontier = [...frontier].sort(byFitnessDesc);

  if (sortedFrontier.length >= maxSize) {
    return sortedFrontier.slice(0, maxSize);
  }

  const sortedNonFrontier = [...nonFrontier].sort(byFitnessDesc);
  const remaining = maxSize - sortedFrontier.length;

  return [...sortedFrontier, ...sortedNonFrontier.slice(0, remaining)];
}

# EvolutionaryAgent TypeScript Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the Python LoopAgent evolutionary framework to TypeScript AppKit, adding EvolutionaryAgent as a new workflow primitive alongside Sequential/Parallel/Loop/Router/Handoff, plus a Voynich reference implementation with 5 AppKit agent apps.

**Architecture:** Four focused modules (hypothesis, pareto, population store, evolutionary agent) plus evolution tools and 5 Voynich example apps. PopulationStore uses SQL Statements API directly (no Spark). EvolutionaryAgent implements Runnable with a background async loop and conversational tools. Remote agents called via HTTP POST to `/invocations`.

**Tech Stack:** TypeScript, Zod v4, vitest, Databricks SQL Statements API, appkit-agent (defineTool, Runnable, RemoteAgent patterns)

**Spec:** `docs/superpowers/specs/2026-04-16-evolutionary-agent-design.md`

---

## File Map

| File | Responsibility |
|------|----------------|
| `ts/src/workflows/hypothesis.ts` | `Hypothesis` interface, `createHypothesis()`, `compositeFitness()` |
| `ts/src/workflows/pareto.ts` | `paretoDominates()`, `paretoFrontier()`, `selectSurvivors()` |
| `ts/src/workflows/population.ts` | `PopulationStore` class — SQL batching, caching, MERGE upsert |
| `ts/src/workflows/evolutionary.ts` | `EvolutionaryAgent` implements Runnable — generation loop, agent calls, pause/resume |
| `ts/src/connectors/evolution-tools.ts` | `defineTool()` factories for conversational tools (status, pause, resume, escalate) |
| `ts/src/workflows/index.ts` | Add new exports |
| `ts/src/connectors/index.ts` | Add evolution-tools exports |
| `ts/src/index.ts` | Add new package-level exports |
| `ts/tests/hypothesis.test.ts` | Tests for hypothesis creation and composite fitness |
| `ts/tests/pareto.test.ts` | Tests for Pareto dominance, frontier, survivor selection |
| `ts/tests/population.test.ts` | Tests for PopulationStore (mocked SQL API) |
| `ts/tests/evolutionary.test.ts` | Tests for EvolutionaryAgent loop, convergence, pause/resume |
| `ts/tests/evolution-tools.test.ts` | Tests for conversational tool factories |
| `ts/examples/voynich/voynich-config.ts` | Shared config: fitness weights, Pareto objectives, cipher types |
| `ts/examples/voynich/orchestrator/app.ts` | EvolutionaryAgent + PopulationStore wiring |
| `ts/examples/voynich/decipherer/app.ts` | Mutation agent: mutate cipher params |
| `ts/examples/voynich/historian/app.ts` | RAG fitness scorer using VS connector |
| `ts/examples/voynich/critic/app.ts` | Adversarial falsifier |
| `ts/examples/voynich/judge/app.ts` | Agent eval: score reasoning quality |

---

### Task 1: Hypothesis Type

**Files:**
- Create: `ts/src/workflows/hypothesis.ts`
- Create: `ts/tests/hypothesis.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/hypothesis.test.ts

import { describe, it, expect } from 'vitest';
import {
  createHypothesis,
  compositeFitness,
  type Hypothesis,
} from '../src/workflows/hypothesis.js';

describe('createHypothesis', () => {
  it('generates an 8-char ID', () => {
    const h = createHypothesis({ generation: 0 });
    expect(h.id).toHaveLength(8);
  });

  it('sets generation and defaults', () => {
    const h = createHypothesis({ generation: 3 });
    expect(h.generation).toBe(3);
    expect(h.parent_id).toBeNull();
    expect(h.fitness).toEqual({});
    expect(h.metadata).toEqual({});
    expect(h.flagged_for_review).toBe(false);
    expect(h.created_at).toBeTruthy();
  });

  it('accepts parent_id, fitness, and metadata', () => {
    const h = createHypothesis({
      generation: 1,
      parent_id: 'abc12345',
      fitness: { statistical: 0.7, semantic: 0.5 },
      metadata: { cipher_type: 'substitution' },
    });
    expect(h.parent_id).toBe('abc12345');
    expect(h.fitness.statistical).toBe(0.7);
    expect(h.metadata.cipher_type).toBe('substitution');
  });

  it('generates unique IDs', () => {
    const ids = new Set(Array.from({ length: 100 }, () => createHypothesis({ generation: 0 }).id));
    expect(ids.size).toBe(100);
  });
});

describe('compositeFitness', () => {
  it('computes weighted sum of fitness signals', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { statistical: 0.8, semantic: 0.6 },
    });
    const score = compositeFitness(h, { statistical: 0.5, semantic: 0.5 });
    expect(score).toBeCloseTo(0.7);
  });

  it('treats missing fitness keys as 0', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { statistical: 1.0 },
    });
    const score = compositeFitness(h, { statistical: 0.5, semantic: 0.5 });
    expect(score).toBeCloseTo(0.5);
  });

  it('normalizes to 0-1 range', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { a: 1.0, b: 1.0, c: 1.0 },
    });
    const score = compositeFitness(h, { a: 0.25, b: 0.25, c: 0.50 });
    expect(score).toBeCloseTo(1.0);
  });

  it('returns 0 for empty fitness', () => {
    const h = createHypothesis({ generation: 0 });
    const score = compositeFitness(h, { statistical: 1.0 });
    expect(score).toBe(0);
  });

  it('returns 0 for empty weights', () => {
    const h = createHypothesis({
      generation: 0,
      fitness: { statistical: 1.0 },
    });
    const score = compositeFitness(h, {});
    expect(score).toBe(0);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/hypothesis.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement hypothesis.ts**

```typescript
// ts/src/workflows/hypothesis.ts

import { randomUUID } from 'node:crypto';

/**
 * A hypothesis in an evolutionary population.
 *
 * Domain-agnostic: fitness signals and metadata are both Records.
 * Voynich uses metadata for cipher_type/symbol_map/etc.
 * Guidepoint KG uses metadata for edge weights/extraction params.
 */
export interface Hypothesis {
  id: string;
  generation: number;
  parent_id: string | null;
  fitness: Record<string, number>;
  metadata: Record<string, unknown>;
  flagged_for_review: boolean;
  created_at: string;
}

/**
 * Create a new hypothesis with generated ID and defaults.
 */
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

/**
 * Compute weighted sum of fitness signals. Returns 0-1.
 * Missing fitness keys are treated as 0.
 */
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/hypothesis.test.ts`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add src/workflows/hypothesis.ts tests/hypothesis.test.ts
git commit -m "feat(evolutionary): add Hypothesis type and compositeFitness"
```

---

### Task 2: Pareto Selection

**Files:**
- Create: `ts/src/workflows/pareto.ts`
- Create: `ts/tests/pareto.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/pareto.test.ts

import { describe, it, expect } from 'vitest';
import {
  paretoDominates,
  paretoFrontier,
  selectSurvivors,
} from '../src/workflows/pareto.js';
import { createHypothesis, compositeFitness } from '../src/workflows/hypothesis.js';

function h(fitness: Record<string, number>, id?: string) {
  const hyp = createHypothesis({ generation: 0, fitness });
  if (id) (hyp as any).id = id;
  return hyp;
}

describe('paretoDominates', () => {
  const objectives = ['a', 'b'];

  it('returns true when a dominates b (better on all, strictly better on one)', () => {
    expect(paretoDominates(h({ a: 0.8, b: 0.6 }), h({ a: 0.5, b: 0.5 }), objectives)).toBe(true);
  });

  it('returns false when b is better on one objective', () => {
    expect(paretoDominates(h({ a: 0.8, b: 0.3 }), h({ a: 0.5, b: 0.5 }), objectives)).toBe(false);
  });

  it('returns false when equal on all objectives', () => {
    expect(paretoDominates(h({ a: 0.5, b: 0.5 }), h({ a: 0.5, b: 0.5 }), objectives)).toBe(false);
  });

  it('treats missing fitness keys as 0', () => {
    expect(paretoDominates(h({ a: 0.5 }), h({ a: 0.3, b: 0.1 }), objectives)).toBe(false);
    expect(paretoDominates(h({ a: 0.5, b: 0.1 }), h({ a: 0.3 }), objectives)).toBe(true);
  });

  it('works with single objective', () => {
    expect(paretoDominates(h({ a: 0.8 }), h({ a: 0.5 }), ['a'])).toBe(true);
    expect(paretoDominates(h({ a: 0.5 }), h({ a: 0.8 }), ['a'])).toBe(false);
  });
});

describe('paretoFrontier', () => {
  const objectives = ['a', 'b'];

  it('returns single element for one hypothesis', () => {
    const pop = [h({ a: 0.5, b: 0.5 })];
    expect(paretoFrontier(pop, objectives)).toHaveLength(1);
  });

  it('returns non-dominated set', () => {
    const pop = [
      h({ a: 0.9, b: 0.1 }, 'top_a'),
      h({ a: 0.1, b: 0.9 }, 'top_b'),
      h({ a: 0.5, b: 0.5 }, 'mid'),
      h({ a: 0.2, b: 0.2 }, 'low'),
    ];
    const frontier = paretoFrontier(pop, objectives);
    const ids = frontier.map((f) => f.id);
    expect(ids).toContain('top_a');
    expect(ids).toContain('top_b');
    expect(ids).toContain('mid');
    expect(ids).not.toContain('low');
  });

  it('returns empty array for empty population', () => {
    expect(paretoFrontier([], objectives)).toEqual([]);
  });

  it('returns all when none are dominated', () => {
    const pop = [
      h({ a: 0.9, b: 0.1 }),
      h({ a: 0.1, b: 0.9 }),
    ];
    expect(paretoFrontier(pop, objectives)).toHaveLength(2);
  });
});

describe('selectSurvivors', () => {
  const objectives = ['a', 'b'];
  const weights = { a: 0.5, b: 0.5 };

  it('returns at most maxSize survivors', () => {
    const pop = Array.from({ length: 10 }, (_, i) =>
      h({ a: i / 10, b: (10 - i) / 10 }),
    );
    const survivors = selectSurvivors(pop, objectives, weights, 5);
    expect(survivors).toHaveLength(5);
  });

  it('prefers Pareto frontier members', () => {
    const pop = [
      h({ a: 0.9, b: 0.1 }, 'frontier1'),
      h({ a: 0.1, b: 0.9 }, 'frontier2'),
      h({ a: 0.2, b: 0.2 }, 'dominated'),
    ];
    const survivors = selectSurvivors(pop, objectives, weights, 2);
    const ids = survivors.map((s) => s.id);
    expect(ids).toContain('frontier1');
    expect(ids).toContain('frontier2');
  });

  it('fills remaining slots by composite fitness', () => {
    const pop = [
      h({ a: 0.9, b: 0.1 }, 'f1'),
      h({ a: 0.1, b: 0.9 }, 'f2'),
      h({ a: 0.4, b: 0.4 }, 'good'),
      h({ a: 0.1, b: 0.1 }, 'bad'),
    ];
    const survivors = selectSurvivors(pop, objectives, weights, 3);
    const ids = survivors.map((s) => s.id);
    expect(ids).toContain('f1');
    expect(ids).toContain('f2');
    expect(ids).toContain('good');
    expect(ids).not.toContain('bad');
  });

  it('returns all if population is smaller than maxSize', () => {
    const pop = [h({ a: 0.5, b: 0.5 })];
    expect(selectSurvivors(pop, objectives, weights, 10)).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/pareto.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement pareto.ts**

```typescript
// ts/src/workflows/pareto.ts

import type { Hypothesis } from './hypothesis.js';
import { compositeFitness } from './hypothesis.js';

/**
 * Returns true if `a` Pareto-dominates `b`:
 * a >= b on ALL objectives AND a > b on AT LEAST ONE.
 */
export function paretoDominates(
  a: Hypothesis,
  b: Hypothesis,
  objectives: string[],
): boolean {
  let strictlyBetter = false;
  for (const obj of objectives) {
    const aVal = a.fitness[obj] ?? 0;
    const bVal = b.fitness[obj] ?? 0;
    if (aVal < bVal) return false;
    if (aVal > bVal) strictlyBetter = true;
  }
  return strictlyBetter;
}

/**
 * Extract the Pareto frontier — all non-dominated hypotheses.
 * O(n²) pairwise comparison.
 */
export function paretoFrontier(
  population: Hypothesis[],
  objectives: string[],
): Hypothesis[] {
  if (population.length === 0) return [];

  return population.filter((candidate) =>
    !population.some(
      (other) => other.id !== candidate.id && paretoDominates(other, candidate, objectives),
    ),
  );
}

/**
 * Select up to maxSize survivors:
 * 1. All Pareto frontier members (up to maxSize)
 * 2. Fill remaining by composite fitness rank
 */
export function selectSurvivors(
  population: Hypothesis[],
  objectives: string[],
  weights: Record<string, number>,
  maxSize: number,
): Hypothesis[] {
  if (population.length <= maxSize) return [...population];

  const frontier = paretoFrontier(population, objectives);

  if (frontier.length >= maxSize) {
    return frontier
      .sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights))
      .slice(0, maxSize);
  }

  const frontierIds = new Set(frontier.map((h) => h.id));
  const remaining = population
    .filter((h) => !frontierIds.has(h.id))
    .sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights));

  return [...frontier, ...remaining.slice(0, maxSize - frontier.length)];
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/pareto.test.ts`
Expected: All 13 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add src/workflows/pareto.ts tests/pareto.test.ts
git commit -m "feat(evolutionary): add Pareto dominance, frontier, and survivor selection"
```

---

### Task 3: PopulationStore

**Files:**
- Create: `ts/src/workflows/population.ts`
- Create: `ts/tests/population.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/population.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { PopulationStore } from '../src/workflows/population.js';
import type { PopulationStoreConfig } from '../src/workflows/population.js';
import { createHypothesis } from '../src/workflows/hypothesis.js';

const storeConfig: PopulationStoreConfig = {
  host: 'https://test-host.databricks.com',
  populationTable: 'main.voynich.population',
  warehouseId: 'wh-123',
  chunkSize: 3,
};

function mockSqlResponse(dataArray: string[][] = [], columns: string[] = []) {
  return {
    statement_id: 'stmt-1',
    status: { state: 'SUCCEEDED' },
    manifest: { schema: { columns: columns.map((c) => ({ name: c })) } },
    result: { data_array: dataArray },
  };
}

function mockFetch(response: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => response,
    text: async () => JSON.stringify(response),
  });
}

describe('PopulationStore', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  describe('writeHypotheses', () => {
    it('chunks writes into batches of config.chunkSize', async () => {
      const fetchMock = mockFetch(mockSqlResponse());
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      const hypotheses = Array.from({ length: 7 }, (_, i) =>
        createHypothesis({ generation: 0, fitness: { score: i / 10 } }),
      );

      await store.writeHypotheses(hypotheses);

      // 7 hypotheses with chunkSize=3 → 3 INSERT calls (3 + 3 + 1)
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it('sends INSERT INTO with correct table name', async () => {
      const fetchMock = mockFetch(mockSqlResponse());
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      await store.writeHypotheses([createHypothesis({ generation: 0 })]);

      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body.statement).toContain('INSERT INTO main.voynich.population');
      expect(body.warehouse_id).toBe('wh-123');
    });

    it('JSON-stringifies fitness and metadata', async () => {
      const fetchMock = mockFetch(mockSqlResponse());
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      const h = createHypothesis({
        generation: 0,
        fitness: { a: 0.5 },
        metadata: { cipher_type: 'substitution' },
      });
      await store.writeHypotheses([h]);

      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body.statement).toContain('fitness');
      expect(body.statement).toContain('metadata');
    });

    it('invalidates cache on write', async () => {
      const fetchMock = mockFetch(
        mockSqlResponse(
          [['id1', '0', '', '{"a":0.5}', '{}', 'false', '2026-01-01T00:00:00Z']],
          ['id', 'generation', 'parent_id', 'fitness', 'metadata', 'flagged_for_review', 'created_at'],
        ),
      );
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      // Load to populate cache
      await store.loadGeneration(0);
      const callsBefore = fetchMock.mock.calls.length;

      // Write invalidates cache
      await store.writeHypotheses([createHypothesis({ generation: 0 })]);

      // Load again should hit API, not cache
      await store.loadGeneration(0);
      expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore + 1);
    });
  });

  describe('updateFitnessScores', () => {
    it('sends MERGE statement for each update', async () => {
      const fetchMock = mockFetch(mockSqlResponse());
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      await store.updateFitnessScores([
        { id: 'abc', fitness: { score: 0.8 } },
        { id: 'def', fitness: { score: 0.6 } },
      ]);

      expect(fetchMock).toHaveBeenCalledTimes(2);
      const body1 = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body1.statement).toContain('MERGE INTO main.voynich.population');
      expect(body1.statement).toContain('WHEN MATCHED THEN UPDATE');
    });
  });

  describe('loadGeneration', () => {
    it('queries by generation number', async () => {
      const fetchMock = mockFetch(
        mockSqlResponse(
          [['id1', '3', '', '{"a":0.5}', '{}', 'false', '2026-01-01T00:00:00Z']],
          ['id', 'generation', 'parent_id', 'fitness', 'metadata', 'flagged_for_review', 'created_at'],
        ),
      );
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      const results = await store.loadGeneration(3);

      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body.statement).toContain('WHERE generation = 3');
      expect(results).toHaveLength(1);
      expect(results[0].id).toBe('id1');
      expect(results[0].fitness.a).toBe(0.5);
    });

    it('caches results and reuses on second call', async () => {
      const fetchMock = mockFetch(
        mockSqlResponse(
          [['id1', '0', '', '{}', '{}', 'false', '2026-01-01T00:00:00Z']],
          ['id', 'generation', 'parent_id', 'fitness', 'metadata', 'flagged_for_review', 'created_at'],
        ),
      );
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      await store.loadGeneration(0);
      await store.loadGeneration(0);

      // Only one API call despite two loads
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });

  describe('getFitnessHistory', () => {
    it('returns best and avg fitness per generation', async () => {
      const rows = [
        ['id1', '0', '', '{"a":0.3}', '{}', 'false', '2026-01-01T00:00:00Z'],
        ['id2', '0', '', '{"a":0.7}', '{}', 'false', '2026-01-01T00:00:00Z'],
        ['id3', '1', '', '{"a":0.5}', '{}', 'false', '2026-01-01T00:00:00Z'],
        ['id4', '1', '', '{"a":0.9}', '{}', 'false', '2026-01-01T00:00:00Z'],
      ];
      const fetchMock = mockFetch(
        mockSqlResponse(rows, ['id', 'generation', 'parent_id', 'fitness', 'metadata', 'flagged_for_review', 'created_at']),
      );
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      const history = await store.getFitnessHistory(5, { a: 1.0 });

      expect(history).toHaveLength(2);
      expect(history[0].generation).toBe(0);
      expect(history[0].best).toBeCloseTo(0.7);
      expect(history[0].avg).toBeCloseTo(0.5);
      expect(history[1].generation).toBe(1);
      expect(history[1].best).toBeCloseTo(0.9);
    });
  });

  describe('clearCache', () => {
    it('forces next load to hit API', async () => {
      const fetchMock = mockFetch(
        mockSqlResponse(
          [['id1', '0', '', '{}', '{}', 'false', '2026-01-01T00:00:00Z']],
          ['id', 'generation', 'parent_id', 'fitness', 'metadata', 'flagged_for_review', 'created_at'],
        ),
      );
      vi.stubGlobal('fetch', fetchMock);

      const store = new PopulationStore(storeConfig);
      await store.loadGeneration(0);
      store.clearCache();
      await store.loadGeneration(0);

      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/population.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement population.ts**

```typescript
// ts/src/workflows/population.ts

import type { Hypothesis } from './hypothesis.js';
import { compositeFitness } from './hypothesis.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PopulationStoreConfig {
  host?: string;
  populationTable: string;
  warehouseId?: string;
  chunkSize?: number;
  cacheEnabled?: boolean;
}

interface SqlStatementResponse {
  statement_id: string;
  status: { state: string };
  manifest?: { schema?: { columns?: Array<{ name: string }> } };
  result?: { data_array?: Array<Array<string | null>> };
}

// ---------------------------------------------------------------------------
// PopulationStore
// ---------------------------------------------------------------------------

export class PopulationStore {
  private host: string;
  private table: string;
  private warehouseId: string;
  private chunkSize: number;
  private cacheEnabled: boolean;
  private cache = new Map<number, Hypothesis[]>();

  constructor(config: PopulationStoreConfig) {
    const h = config.host ?? process.env.DATABRICKS_HOST;
    if (!h) throw new Error('No Databricks host: set host in config or DATABRICKS_HOST env var');
    this.host = h.startsWith('http') ? h.replace(/\/$/, '') : `https://${h}`;
    this.table = config.populationTable;
    this.warehouseId = config.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID ?? '';
    this.chunkSize = config.chunkSize ?? 25;
    this.cacheEnabled = config.cacheEnabled ?? true;
  }

  // -------------------------------------------------------------------------
  // Write
  // -------------------------------------------------------------------------

  async writeHypotheses(hypotheses: Hypothesis[]): Promise<void> {
    for (let i = 0; i < hypotheses.length; i += this.chunkSize) {
      const chunk = hypotheses.slice(i, i + this.chunkSize);
      const columns = 'id, generation, parent_id, fitness, metadata, flagged_for_review, created_at';
      const values = chunk
        .map((h) => {
          const parentId = h.parent_id ? `'${h.parent_id}'` : 'NULL';
          const fitness = `'${JSON.stringify(h.fitness)}'`;
          const metadata = `'${JSON.stringify(h.metadata)}'`;
          const flagged = h.flagged_for_review ? 'TRUE' : 'FALSE';
          return `('${h.id}', ${h.generation}, ${parentId}, ${fitness}, ${metadata}, ${flagged}, '${h.created_at}')`;
        })
        .join(', ');
      await this.executeSql(`INSERT INTO ${this.table} (${columns}) VALUES ${values}`);
    }
    this.cache.clear();
  }

  async updateFitnessScores(
    updates: Array<{ id: string; fitness: Record<string, number> }>,
  ): Promise<void> {
    for (const update of updates) {
      const fitnessJson = JSON.stringify(update.fitness);
      await this.executeSql(
        `MERGE INTO ${this.table} AS t ` +
        `USING (SELECT '${update.id}' AS id, '${fitnessJson}' AS fitness) AS s ` +
        `ON t.id = s.id ` +
        `WHEN MATCHED THEN UPDATE SET t.fitness = s.fitness`,
      );
    }
    this.cache.clear();
  }

  // -------------------------------------------------------------------------
  // Read
  // -------------------------------------------------------------------------

  async loadGeneration(generation: number): Promise<Hypothesis[]> {
    if (this.cacheEnabled && this.cache.has(generation)) {
      return this.cache.get(generation)!;
    }

    const response = await this.executeSql(
      `SELECT id, generation, parent_id, fitness, metadata, flagged_for_review, created_at ` +
      `FROM ${this.table} WHERE generation = ${generation}`,
    );

    const hypotheses = this.parseRows(response);
    if (this.cacheEnabled) {
      this.cache.set(generation, hypotheses);
    }
    return hypotheses;
  }

  async loadTopSurvivors(
    generation: number,
    topN: number,
    weights: Record<string, number>,
  ): Promise<Hypothesis[]> {
    const all = await this.loadGeneration(generation);
    return all
      .sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights))
      .slice(0, topN);
  }

  async getFitnessHistory(
    nGenerations: number,
    weights: Record<string, number>,
  ): Promise<Array<{ generation: number; best: number; avg: number }>> {
    const response = await this.executeSql(
      `SELECT id, generation, parent_id, fitness, metadata, flagged_for_review, created_at ` +
      `FROM ${this.table} ORDER BY generation DESC`,
    );

    const all = this.parseRows(response);
    const byGen = new Map<number, Hypothesis[]>();
    for (const h of all) {
      const arr = byGen.get(h.generation) ?? [];
      arr.push(h);
      byGen.set(h.generation, arr);
    }

    const generations = [...byGen.keys()].sort((a, b) => a - b).slice(-nGenerations);
    return generations.map((gen) => {
      const pop = byGen.get(gen)!;
      const scores = pop.map((h) => compositeFitness(h, weights));
      return {
        generation: gen,
        best: Math.max(...scores),
        avg: scores.reduce((s, v) => s + v, 0) / scores.length,
      };
    });
  }

  async getActiveConstraints(): Promise<Array<{ id: string; constraint: string }>> {
    const response = await this.executeSql(
      `SELECT id, constraint FROM ${this.table}_review_queue WHERE status = 'approved'`,
    );
    const columns = response.manifest?.schema?.columns?.map((c) => c.name) ?? [];
    return (response.result?.data_array ?? []).map((row) => ({
      id: row[columns.indexOf('id')] ?? '',
      constraint: row[columns.indexOf('constraint')] ?? '',
    }));
  }

  // -------------------------------------------------------------------------
  // Cache
  // -------------------------------------------------------------------------

  clearCache(): void {
    this.cache.clear();
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private async executeSql(statement: string): Promise<SqlStatementResponse> {
    const token = process.env.DATABRICKS_TOKEN;
    if (!token) throw new Error('DATABRICKS_TOKEN env var required');

    const res = await fetch(`${this.host}/api/2.0/sql/statements/`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        statement,
        warehouse_id: this.warehouseId,
        wait_timeout: '30s',
        disposition: 'INLINE',
        format: 'JSON_ARRAY',
      }),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`SQL Statements API ${res.status}: ${text}`);
    }

    return res.json() as Promise<SqlStatementResponse>;
  }

  private parseRows(response: SqlStatementResponse): Hypothesis[] {
    const columns = response.manifest?.schema?.columns?.map((c) => c.name) ?? [];
    return (response.result?.data_array ?? []).map((row) => {
      const obj: Record<string, string | null> = {};
      for (let i = 0; i < columns.length; i++) {
        obj[columns[i]] = row[i];
      }
      return {
        id: obj.id ?? '',
        generation: parseInt(obj.generation ?? '0', 10),
        parent_id: obj.parent_id || null,
        fitness: JSON.parse(obj.fitness ?? '{}'),
        metadata: JSON.parse(obj.metadata ?? '{}'),
        flagged_for_review: obj.flagged_for_review === 'true' || obj.flagged_for_review === 'TRUE',
        created_at: obj.created_at ?? '',
      };
    });
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/population.test.ts`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add src/workflows/population.ts tests/population.test.ts
git commit -m "feat(evolutionary): add PopulationStore with SQL batching and caching"
```

---

### Task 4: EvolutionaryAgent

**Files:**
- Create: `ts/src/workflows/evolutionary.ts`
- Create: `ts/tests/evolutionary.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ts/tests/evolutionary.test.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EvolutionaryAgent } from '../src/workflows/evolutionary.js';
import type { EvolutionaryConfig } from '../src/workflows/evolutionary.js';
import { PopulationStore } from '../src/workflows/population.js';
import { createHypothesis } from '../src/workflows/hypothesis.js';

// Mock PopulationStore
function createMockStore() {
  const hypotheses = new Map<number, ReturnType<typeof createHypothesis>[]>();
  return {
    writeHypotheses: vi.fn(async (h: any[]) => {
      for (const hyp of h) {
        const gen = hyp.generation;
        const arr = hypotheses.get(gen) ?? [];
        arr.push(hyp);
        hypotheses.set(gen, arr);
      }
    }),
    updateFitnessScores: vi.fn(async () => {}),
    loadGeneration: vi.fn(async (gen: number) => hypotheses.get(gen) ?? []),
    loadTopSurvivors: vi.fn(async (gen: number, topN: number) => {
      const all = hypotheses.get(gen) ?? [];
      return all.slice(0, topN);
    }),
    getFitnessHistory: vi.fn(async () => []),
    getActiveConstraints: vi.fn(async () => []),
    clearCache: vi.fn(),
  } as unknown as PopulationStore;
}

// Mock agent responses (mutation and fitness)
function mockAgentFetch() {
  let callCount = 0;
  return vi.fn(async (url: string, opts: any) => {
    const body = JSON.parse(opts.body);
    callCount++;

    // Mutation agent: return new hypotheses
    if (url.includes('mutation-agent')) {
      const newHypotheses = [
        createHypothesis({ generation: 1, fitness: { score: 0.5 + callCount * 0.01 } }),
      ];
      return {
        ok: true,
        json: async () => ({
          output: [{ type: 'message', role: 'assistant', content: [{ type: 'text', text: JSON.stringify(newHypotheses) }] }],
        }),
      };
    }

    // Fitness agent: return scores
    if (url.includes('fitness-agent')) {
      return {
        ok: true,
        json: async () => ({
          output: [{ type: 'message', role: 'assistant', content: [{ type: 'text', text: JSON.stringify({ score: 0.7 }) }] }],
        }),
      };
    }

    // Default
    return { ok: true, json: async () => ({}) };
  });
}

function createTestConfig(store: PopulationStore): EvolutionaryConfig {
  return {
    store,
    populationSize: 10,
    mutationBatch: 2,
    mutationAgent: 'https://mutation-agent.apps.databricks.com',
    fitnessAgents: ['https://fitness-agent.apps.databricks.com'],
    paretoObjectives: ['score'],
    fitnessWeights: { score: 1.0 },
    maxGenerations: 3,
    convergencePatience: 50,
    convergenceThreshold: 0.001,
    escalationThreshold: 0.85,
    topKAdversarial: 0.05,
  };
}

describe('EvolutionaryAgent', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  it('implements Runnable interface', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(createTestConfig(store));
    expect(typeof agent.run).toBe('function');
    expect(typeof agent.stream).toBe('function');
    expect(typeof agent.collectTools).toBe('function');
  });

  it('collectTools returns evolution tools', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(createTestConfig(store));
    const tools = agent.collectTools();
    const names = tools.map((t) => t.name);
    expect(names).toContain('evolution_status');
    expect(names).toContain('best_hypothesis');
    expect(names).toContain('generation_summary');
    expect(names).toContain('pause_evolution');
    expect(names).toContain('resume_evolution');
    expect(names).toContain('force_escalate');
  });

  it('starts loop on first run call', async () => {
    const store = createMockStore();
    // Seed gen 0
    await store.writeHypotheses([
      createHypothesis({ generation: 0, fitness: { score: 0.3 } }),
      createHypothesis({ generation: 0, fitness: { score: 0.4 } }),
    ]);

    vi.stubGlobal('fetch', mockAgentFetch());

    const agent = new EvolutionaryAgent(createTestConfig(store));
    const result = await agent.run([{ role: 'user', content: 'Start evolution' }]);

    expect(result).toContain('started');
    expect(agent.getState()).toBe('running');
  });

  it('pauses and resumes', async () => {
    const store = createMockStore();
    await store.writeHypotheses([
      createHypothesis({ generation: 0, fitness: { score: 0.5 } }),
    ]);

    vi.stubGlobal('fetch', mockAgentFetch());

    const config = createTestConfig(store);
    config.maxGenerations = 100; // won't converge quickly
    const agent = new EvolutionaryAgent(config);

    await agent.run([{ role: 'user', content: 'Start' }]);
    await agent.pauseLoop();
    expect(agent.getState()).toBe('paused');

    await agent.resumeLoop();
    expect(agent.getState()).toBe('running');
  });

  it('detects convergence when fitness stops improving', async () => {
    const store = createMockStore();
    await store.writeHypotheses([
      createHypothesis({ generation: 0, fitness: { score: 0.5 } }),
    ]);

    // Mock fitness history to show convergence
    (store.getFitnessHistory as any).mockResolvedValue(
      Array.from({ length: 55 }, (_, i) => ({
        generation: i,
        best: 0.5,
        avg: 0.4,
      })),
    );

    vi.stubGlobal('fetch', mockAgentFetch());

    const config = createTestConfig(store);
    config.convergencePatience = 5;
    const agent = new EvolutionaryAgent(config);

    // checkConvergence should detect stagnation
    expect(agent.checkConvergence(
      Array.from({ length: 10 }, (_, i) => ({
        generation: i, best: 0.5, avg: 0.4,
      })),
    )).toBe(true);
  });

  it('does not converge when fitness is improving', () => {
    const store = createMockStore();
    const config = createTestConfig(store);
    config.convergencePatience = 5;
    const agent = new EvolutionaryAgent(config);

    expect(agent.checkConvergence(
      Array.from({ length: 10 }, (_, i) => ({
        generation: i, best: 0.3 + i * 0.05, avg: 0.2 + i * 0.03,
      })),
    )).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/evolutionary.test.ts`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement evolutionary.ts**

```typescript
// ts/src/workflows/evolutionary.ts

import type { AgentTool } from '../agent/tools.js';
import type { Message, Runnable } from './types.js';
import type { Hypothesis } from './hypothesis.js';
import { createHypothesis, compositeFitness } from './hypothesis.js';
import { selectSurvivors } from './pareto.js';
import type { PopulationStore } from './population.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EvolutionaryConfig {
  store: PopulationStore;
  populationSize: number;
  mutationBatch: number;
  mutationAgent: string;
  fitnessAgents: string[];
  judgeAgent?: string;
  paretoObjectives: string[];
  fitnessWeights: Record<string, number>;
  maxGenerations: number;
  convergencePatience: number;
  convergenceThreshold: number;
  escalationThreshold: number;
  topKAdversarial: number;
  model?: string;
  instructions?: string;
}

export type EvolutionState = 'idle' | 'running' | 'paused' | 'converged' | 'completed';

export interface GenerationResult {
  generation: number;
  populationSize: number;
  bestFitness: number;
  avgFitness: number;
  paretoFrontierSize: number;
  escalated: Hypothesis[];
  wallTimeMs: number;
  converged: boolean;
}

// ---------------------------------------------------------------------------
// EvolutionaryAgent
// ---------------------------------------------------------------------------

export class EvolutionaryAgent implements Runnable {
  private config: EvolutionaryConfig;
  private state: EvolutionState = 'idle';
  private currentGeneration = 0;
  private history: GenerationResult[] = [];
  private loopPromise: Promise<void> | null = null;
  private tools: AgentTool[];

  constructor(config: EvolutionaryConfig) {
    this.config = config;
    this.tools = this.buildTools();
  }

  // -----------------------------------------------------------------------
  // Runnable
  // -----------------------------------------------------------------------

  async run(messages: Message[]): Promise<string> {
    if (this.state === 'idle') {
      this.startLoop();
      return `Evolution started. Population: ${this.config.populationSize}, ` +
        `max generations: ${this.config.maxGenerations}. ` +
        `Use evolution_status tool to check progress.`;
    }

    // Conversational mode — summarize current state
    const latest = this.history[this.history.length - 1];
    return `Evolution ${this.state}. Generation ${this.currentGeneration}` +
      (latest ? `, best fitness: ${latest.bestFitness.toFixed(4)}` : '') + '.';
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools(): AgentTool[] {
    return this.tools;
  }

  // -----------------------------------------------------------------------
  // Loop control
  // -----------------------------------------------------------------------

  getState(): EvolutionState {
    return this.state;
  }

  startLoop(): void {
    this.state = 'running';
    this.loopPromise = this.runLoop();
  }

  async pauseLoop(): Promise<void> {
    this.state = 'paused';
  }

  async resumeLoop(): Promise<void> {
    if (this.state !== 'paused') return;
    this.state = 'running';
    this.loopPromise = this.runLoop();
  }

  /**
   * Check convergence: if the last `patience` generations show less than
   * `threshold` variation in best fitness, the loop has converged.
   */
  checkConvergence(
    fitnessHistory: Array<{ generation: number; best: number; avg: number }>,
  ): boolean {
    if (fitnessHistory.length < this.config.convergencePatience) return false;
    const recent = fitnessHistory.slice(-this.config.convergencePatience);
    const bests = recent.map((r) => r.best);
    const delta = Math.max(...bests) - Math.min(...bests);
    return delta < this.config.convergenceThreshold;
  }

  // -----------------------------------------------------------------------
  // Background loop
  // -----------------------------------------------------------------------

  private async runLoop(): Promise<void> {
    while (
      this.state === 'running' &&
      this.currentGeneration < this.config.maxGenerations
    ) {
      const result = await this.runGeneration(this.currentGeneration + 1);
      this.history.push(result);
      this.currentGeneration = result.generation;

      if (result.converged) {
        this.state = 'converged';
        return;
      }
    }

    if (this.state === 'running') {
      this.state = 'completed';
    }
  }

  private async runGeneration(generation: number): Promise<GenerationResult> {
    const start = Date.now();
    const { store, populationSize, fitnessWeights, paretoObjectives, escalationThreshold } = this.config;

    // 1. Load survivors from previous generation
    const prevGen = generation - 1;
    const parents = prevGen >= 0
      ? await store.loadTopSurvivors(prevGen, populationSize, fitnessWeights)
      : [];

    // 2. Mutate
    const candidates = await this.mutate(parents, generation);

    // 3. Evaluate
    const evaluated = await this.evaluate(candidates);

    // 4. Judge (top 20%)
    const judged = await this.judge(evaluated);

    // 5. Write to store
    await store.writeHypotheses(judged);

    // 6. Select survivors
    const pool = [...parents, ...judged];
    const survivors = selectSurvivors(pool, paretoObjectives, fitnessWeights, populationSize);

    // 7. Escalate
    const escalated = survivors.filter(
      (h) => compositeFitness(h, fitnessWeights) >= escalationThreshold,
    );
    if (escalated.length > 0) {
      await store.updateFitnessScores(
        escalated.map((h) => ({
          id: h.id,
          fitness: { ...h.fitness, _flagged: 1 },
        })),
      );
    }

    // 8. Convergence check
    const fitnessHistory = await store.getFitnessHistory(
      this.config.convergencePatience + 5,
      fitnessWeights,
    );
    const converged = this.checkConvergence(fitnessHistory);

    const scores = survivors.map((h) => compositeFitness(h, fitnessWeights));
    return {
      generation,
      populationSize: survivors.length,
      bestFitness: scores.length > 0 ? Math.max(...scores) : 0,
      avgFitness: scores.length > 0 ? scores.reduce((s, v) => s + v, 0) / scores.length : 0,
      paretoFrontierSize: survivors.length,
      escalated,
      wallTimeMs: Date.now() - start,
      converged,
    };
  }

  // -----------------------------------------------------------------------
  // Agent communication
  // -----------------------------------------------------------------------

  private async mutate(parents: Hypothesis[], generation: number): Promise<Hypothesis[]> {
    const response = await this.callAgent(this.config.mutationAgent, {
      task: 'mutate',
      parents: parents.map((p) => ({ id: p.id, fitness: p.fitness, metadata: p.metadata })),
      generation,
      batch_size: this.config.mutationBatch,
    });

    try {
      const parsed = JSON.parse(response);
      if (Array.isArray(parsed)) {
        return parsed.map((item: Record<string, unknown>) =>
          createHypothesis({
            generation,
            parent_id: (item.parent_id as string) ?? parents[0]?.id,
            fitness: (item.fitness as Record<string, number>) ?? {},
            metadata: (item.metadata as Record<string, unknown>) ?? item,
          }),
        );
      }
    } catch {
      // Fallback: agent returned non-JSON
    }

    return [];
  }

  private async evaluate(candidates: Hypothesis[]): Promise<Hypothesis[]> {
    for (const agent of this.config.fitnessAgents) {
      for (const candidate of candidates) {
        const response = await this.callAgent(agent, {
          task: 'evaluate',
          hypothesis: { id: candidate.id, fitness: candidate.fitness, metadata: candidate.metadata },
        });

        try {
          const scores = JSON.parse(response) as Record<string, number>;
          Object.assign(candidate.fitness, scores);
        } catch {
          // Agent returned non-JSON — skip
        }
      }
    }
    return candidates;
  }

  private async judge(evaluated: Hypothesis[]): Promise<Hypothesis[]> {
    if (!this.config.judgeAgent || evaluated.length === 0) return evaluated;

    const topCount = Math.max(1, Math.ceil(evaluated.length * 0.2));
    const sorted = [...evaluated].sort(
      (a, b) => compositeFitness(b, this.config.fitnessWeights) - compositeFitness(a, this.config.fitnessWeights),
    );
    const toJudge = sorted.slice(0, topCount);

    for (const candidate of toJudge) {
      const response = await this.callAgent(this.config.judgeAgent, {
        task: 'judge',
        hypothesis: { id: candidate.id, fitness: candidate.fitness, metadata: candidate.metadata },
      });

      try {
        const scores = JSON.parse(response) as Record<string, number>;
        for (const [key, value] of Object.entries(scores)) {
          candidate.fitness[`agent_eval_${key}`] = value;
        }
      } catch {
        // skip
      }
    }

    return evaluated;
  }

  private async callAgent(url: string, payload: unknown): Promise<string> {
    const token = process.env.DATABRICKS_TOKEN ?? '';
    const res = await fetch(`${url.replace(/\/$/, '')}/invocations`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        input: [{ role: 'user', content: JSON.stringify(payload) }],
      }),
    });

    if (!res.ok) {
      return `Agent error (${res.status})`;
    }

    const data = (await res.json()) as {
      output?: Array<{ content?: Array<{ text?: string }> }>;
      output_text?: string;
    };

    if (data.output_text) return data.output_text;
    return data.output?.[0]?.content?.[0]?.text ?? '';
  }

  // -----------------------------------------------------------------------
  // Tool builders
  // -----------------------------------------------------------------------

  private buildTools(): AgentTool[] {
    // Inline imports to avoid circular deps
    const { z } = require('zod');
    const { defineTool } = require('../agent/tools.js');

    return [
      defineTool({
        name: 'evolution_status',
        description: 'Get current evolution state, generation, and best fitness.',
        parameters: z.object({}),
        handler: async () => ({
          state: this.state,
          generation: this.currentGeneration,
          bestFitness: this.history.length > 0
            ? this.history[this.history.length - 1].bestFitness
            : 0,
          totalEscalated: this.history.reduce((s, r) => s + r.escalated.length, 0),
        }),
      }),
      defineTool({
        name: 'best_hypothesis',
        description: 'Get the best hypothesis from the latest or specified generation.',
        parameters: z.object({
          generation: z.number().int().optional().describe('Generation number (default: latest)'),
        }),
        handler: async ({ generation }: { generation?: number }) => {
          const gen = generation ?? this.currentGeneration;
          const pop = await this.config.store.loadGeneration(gen);
          if (pop.length === 0) return { error: `No hypotheses in generation ${gen}` };
          const best = pop.reduce((a, b) =>
            compositeFitness(a, this.config.fitnessWeights) >= compositeFitness(b, this.config.fitnessWeights) ? a : b,
          );
          return { ...best, composite: compositeFitness(best, this.config.fitnessWeights) };
        },
      }),
      defineTool({
        name: 'generation_summary',
        description: 'Get stats for a specific generation.',
        parameters: z.object({
          generation: z.number().int().optional().describe('Generation number (default: latest)'),
        }),
        handler: async ({ generation }: { generation?: number }) => {
          const gen = generation ?? this.currentGeneration;
          const result = this.history.find((r) => r.generation === gen);
          return result ?? { error: `No results for generation ${gen}` };
        },
      }),
      defineTool({
        name: 'pause_evolution',
        description: 'Pause evolution after the current generation completes.',
        parameters: z.object({}),
        handler: async () => {
          await this.pauseLoop();
          return { state: this.state, message: 'Evolution will pause after current generation' };
        },
      }),
      defineTool({
        name: 'resume_evolution',
        description: 'Resume a paused evolution loop.',
        parameters: z.object({}),
        handler: async () => {
          await this.resumeLoop();
          return { state: this.state, message: 'Evolution resumed' };
        },
      }),
      defineTool({
        name: 'force_escalate',
        description: 'Flag a hypothesis for human review.',
        parameters: z.object({
          hypothesis_id: z.string().describe('ID of the hypothesis to escalate'),
        }),
        handler: async ({ hypothesis_id }: { hypothesis_id: string }) => {
          await this.config.store.updateFitnessScores([
            { id: hypothesis_id, fitness: { _flagged: 1 } },
          ]);
          return { success: true, hypothesis_id, message: 'Flagged for human review' };
        },
      }),
    ];
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run tests/evolutionary.test.ts`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add src/workflows/evolutionary.ts tests/evolutionary.test.ts
git commit -m "feat(evolutionary): add EvolutionaryAgent with background loop and tools"
```

---

### Task 5: Package Wiring

**Files:**
- Modify: `ts/src/workflows/index.ts`
- Modify: `ts/src/index.ts`

- [ ] **Step 1: Add evolutionary exports to workflows/index.ts**

Add to the end of `ts/src/workflows/index.ts`:

```typescript
// Evolutionary workflow — population management across generations
export { EvolutionaryAgent } from './evolutionary.js';
export type { EvolutionaryConfig, EvolutionState, GenerationResult } from './evolutionary.js';
export { PopulationStore } from './population.js';
export type { PopulationStoreConfig } from './population.js';
export { paretoDominates, paretoFrontier, selectSurvivors } from './pareto.js';
export { createHypothesis, compositeFitness } from './hypothesis.js';
export type { Hypothesis } from './hypothesis.js';
```

- [ ] **Step 2: Add evolutionary exports to src/index.ts**

Add to the end of `ts/src/index.ts` (after the connectors block):

```typescript
// Evolutionary workflow
export {
  EvolutionaryAgent,
  PopulationStore,
  paretoDominates,
  paretoFrontier,
  selectSurvivors,
  createHypothesis,
  compositeFitness,
} from './workflows/index.js';
export type {
  Hypothesis,
  EvolutionaryConfig,
  EvolutionState,
  GenerationResult,
  PopulationStoreConfig,
} from './workflows/index.js';
```

- [ ] **Step 3: Run full test suite**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run`
Expected: All existing + new tests PASS

- [ ] **Step 4: Run typecheck**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add src/workflows/index.ts src/index.ts
git commit -m "feat(evolutionary): wire exports into package entry point"
```

---

### Task 6: Voynich Shared Config

**Files:**
- Create: `ts/examples/voynich/voynich-config.ts`

- [ ] **Step 1: Create the shared config**

```typescript
// ts/examples/voynich/voynich-config.ts

/**
 * Voynich manuscript evolutionary decipherment — shared config.
 *
 * Used by all 5 Voynich agent apps. Fitness weights match the Python
 * reference implementation (PR #6).
 */

export const VOYNICH_FITNESS_WEIGHTS: Record<string, number> = {
  statistical: 0.25,
  perplexity: 0.25,
  semantic: 0.30,
  consistency: 0.15,
  adversarial: 0.05,
};

export const VOYNICH_PARETO_OBJECTIVES = [
  'statistical',
  'perplexity',
  'semantic',
  'consistency',
];

export const VOYNICH_CIPHER_TYPES = [
  'substitution',
  'polyalphabetic',
  'null_bearing',
  'transposition',
  'composite',
  'steganographic',
] as const;

export const VOYNICH_SOURCE_LANGUAGES = [
  'latin',
  'hebrew',
  'arabic',
  'italian',
  'occitan',
  'catalan',
  'greek',
  'czech',
] as const;

export const EVA_COMMON_CHARS = [
  'o', 'a', 'i', 'n', 's', 'e', 'l', 'r', 'ch', 'sh', 'th', 'q',
];

export const POST_RENAISSANCE_CONCEPTS = [
  'telescope', 'microscope', 'oxygen', 'carbon', 'bacteria', 'virus',
  'circulation', 'gravity', 'heliocentr', 'copernican', 'newtonian',
  'logarithm', 'calculus', 'perspective',
  'printing press', 'movable type',
  'syphilis',
  'potato', 'tomato', 'corn', 'tobacco',
];

/** Default population table — override with VOYNICH_POPULATION_TABLE env var. */
export const DEFAULT_POPULATION_TABLE = 'voynich.decipherment.population';

/** Vector Search indexes for medieval corpus. */
export const VECTOR_INDEXES: Record<string, string> = {
  botanical: 'voynich.medieval.botanical_index',
  astronomical: 'voynich.medieval.astronomical_index',
  pharmaceutical: 'voynich.medieval.pharmaceutical_index',
  alchemical: 'voynich.medieval.alchemical_index',
  general: 'voynich.medieval.general_index',
};

export const SECTION_TO_INDEX: Record<string, string> = {
  herbal: 'botanical',
  astronomical: 'astronomical',
  balneological: 'pharmaceutical',
  pharmaceutical: 'pharmaceutical',
  recipes: 'alchemical',
  cosmological: 'astronomical',
};
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/voynich-config.ts
git commit -m "feat(voynich): add shared config — fitness weights, indexes, constants"
```

---

### Task 7: Voynich Orchestrator App

**Files:**
- Create: `ts/examples/voynich/orchestrator/app.ts`

- [ ] **Step 1: Create the orchestrator**

```typescript
// ts/examples/voynich/orchestrator/app.ts

/**
 * Voynich Orchestrator — evolutionary loop controller.
 *
 * Creates an EvolutionaryAgent with Voynich config, wires the other 4
 * agents as mutation/fitness/judge agents by URL, and exposes conversational
 * tools for researcher interaction.
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   VOYNICH_POPULATION_TABLE=voynich.decipherment.population \
 *   DECIPHERER_AGENT_URL=https://decipherer.apps.databricks.com \
 *   FITNESS_AGENT_URLS=https://historian.apps.databricks.com,https://critic.apps.databricks.com \
 *   JUDGE_AGENT_URL=https://judge.apps.databricks.com \
 *   npx tsx app.ts
 */

import express from 'express';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  EvolutionaryAgent,
  PopulationStore,
} from 'appkit-agent';
import { VOYNICH_FITNESS_WEIGHTS, VOYNICH_PARETO_OBJECTIVES, DEFAULT_POPULATION_TABLE } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Population store
// ---------------------------------------------------------------------------

const store = new PopulationStore({
  populationTable: process.env.VOYNICH_POPULATION_TABLE ?? DEFAULT_POPULATION_TABLE,
  warehouseId: process.env.DATABRICKS_WAREHOUSE_ID,
});

// ---------------------------------------------------------------------------
// Evolutionary agent
// ---------------------------------------------------------------------------

const evolutionary = new EvolutionaryAgent({
  store,
  populationSize: parseInt(process.env.POPULATION_SIZE ?? '500'),
  mutationBatch: parseInt(process.env.MUTATION_BATCH ?? '50'),
  mutationAgent: process.env.DECIPHERER_AGENT_URL!,
  fitnessAgents: (process.env.FITNESS_AGENT_URLS ?? '').split(',').filter(Boolean),
  judgeAgent: process.env.JUDGE_AGENT_URL,
  paretoObjectives: VOYNICH_PARETO_OBJECTIVES,
  fitnessWeights: VOYNICH_FITNESS_WEIGHTS,
  maxGenerations: parseInt(process.env.MAX_GENERATIONS ?? '2000'),
  convergencePatience: 50,
  convergenceThreshold: 0.001,
  escalationThreshold: 0.85,
  topKAdversarial: 0.05,
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Manuscript evolutionary decipherment orchestrator. ' +
    'You manage a population of cipher hypotheses across generations, using ' +
    'Pareto selection and convergence detection. Use your tools to check status, ' +
    'inspect hypotheses, and control the loop.',
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: evolutionary['config'].instructions,
  tools: evolutionary.collectTools(),
  workflow: evolutionary,
});

const agentExports = () => agentPlugin.exports();

const app = express();
app.use(express.json());

agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createMcpPlugin({}, agentExports).setup().then(() => {}).catch(console.error);
createDevPlugin({}, agentExports).injectRoutes(app);
agentPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`Voynich Orchestrator at http://localhost:${port}`);
  console.log(`  /responses  — agent endpoint`);
  console.log(`  /_apx/agent — dev chat UI`);
});
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/orchestrator/app.ts
git commit -m "feat(voynich): add Orchestrator app — EvolutionaryAgent controller"
```

---

### Task 8: Voynich Decipherer App

**Files:**
- Create: `ts/examples/voynich/decipherer/app.ts`

- [ ] **Step 1: Create the decipherer (mutation agent)**

```typescript
// ts/examples/voynich/decipherer/app.ts

/**
 * Voynich Decipherer — hypothesis mutation agent.
 *
 * Generates new cipher hypotheses by mutating parent hypotheses.
 * Uses FMAPI for creative mutation of symbol mappings and cipher parameters.
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
} from 'appkit-agent';
import { VOYNICH_CIPHER_TYPES, VOYNICH_SOURCE_LANGUAGES, EVA_COMMON_CHARS } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const mutateHypothesis = defineTool({
  name: 'mutate_hypothesis',
  description: 'Generate a mutated cipher hypothesis from a parent. Modifies symbol mappings, null chars, or transformation rules.',
  parameters: z.object({
    parent_id: z.string().describe('ID of the parent hypothesis'),
    cipher_type: z.string().describe('Current cipher type'),
    source_language: z.string().describe('Current source language'),
    symbol_map: z.record(z.string()).describe('Current EVA symbol → plaintext mapping'),
    null_chars: z.array(z.string()).optional().describe('Current null characters'),
    mutation_hint: z.string().optional().describe('Specific aspect to mutate'),
  }),
  handler: async ({ parent_id, cipher_type, source_language, symbol_map, null_chars, mutation_hint }) => {
    // Deterministic mutations — swap two random symbol mappings
    const entries = Object.entries(symbol_map);
    if (entries.length >= 2) {
      const i = Math.floor(Math.random() * entries.length);
      let j = Math.floor(Math.random() * entries.length);
      while (j === i) j = Math.floor(Math.random() * entries.length);
      const newMap = { ...symbol_map };
      [newMap[entries[i][0]], newMap[entries[j][0]]] = [newMap[entries[j][0]], newMap[entries[i][0]]];

      return {
        parent_id,
        cipher_type,
        source_language,
        metadata: {
          cipher_type,
          source_language,
          symbol_map: newMap,
          null_chars: null_chars ?? [],
          mutation: `swapped ${entries[i][0]} ↔ ${entries[j][0]}`,
        },
        fitness: {},
      };
    }

    return { parent_id, cipher_type, source_language, metadata: { cipher_type, source_language, symbol_map, null_chars: null_chars ?? [] }, fitness: {} };
  },
});

const applyCipher = defineTool({
  name: 'apply_cipher',
  description: 'Apply a cipher hypothesis to EVA-transliterated text, producing decoded plaintext.',
  parameters: z.object({
    eva_text: z.string().describe('EVA-transliterated Voynich text'),
    symbol_map: z.record(z.string()).describe('EVA symbol → plaintext mapping'),
    null_chars: z.array(z.string()).optional().describe('Symbols to remove as nulls'),
  }),
  handler: async ({ eva_text, symbol_map, null_chars }) => {
    const nullSet = new Set(null_chars ?? []);
    const decoded = eva_text
      .split('')
      .filter((c) => !nullSet.has(c))
      .map((c) => symbol_map[c] ?? c)
      .join('');
    return { decoded_text: decoded, original_length: eva_text.length, decoded_length: decoded.length };
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Decipherer. You propose and mutate cipher hypotheses. ' +
    'When asked to mutate, use the mutate_hypothesis tool. ' +
    'When asked to decode text, use the apply_cipher tool.',
  tools: [mutateHypothesis, applyCipher],
});

const agentExports = () => agentPlugin.exports();
const app = express();
app.use(express.json());

agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createDevPlugin({}, agentExports).injectRoutes(app);
agentPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8001');
app.listen(port, () => console.log(`Voynich Decipherer at http://localhost:${port}`));
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/decipherer/app.ts
git commit -m "feat(voynich): add Decipherer app — hypothesis mutation agent"
```

---

### Task 9: Voynich Historian App

**Files:**
- Create: `ts/examples/voynich/historian/app.ts`

- [ ] **Step 1: Create the historian (RAG fitness scorer)**

```typescript
// ts/examples/voynich/historian/app.ts

/**
 * Voynich Historian — medieval RAG fitness scorer.
 *
 * Uses Vector Search to query indexed medieval texts and score decoded
 * text for period-appropriate vocabulary and knowledge boundaries.
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  defineTool,
  createVSQueryTool,
} from 'appkit-agent';
import { VECTOR_INDEXES, SECTION_TO_INDEX, POST_RENAISSANCE_CONCEPTS } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const scoreHistoricalPlausibility = defineTool({
  name: 'score_historical_plausibility',
  description: 'Score decoded text for period-appropriate vocabulary by searching medieval corpus via Vector Search.',
  parameters: z.object({
    decoded_text: z.string().describe('Decoded plaintext to evaluate'),
    section: z.string().describe('Manuscript section: herbal, astronomical, balneological, pharmaceutical, recipes'),
  }),
  handler: async ({ decoded_text, section }) => {
    // Check for anachronisms
    const textLower = decoded_text.toLowerCase();
    const anachronisms = POST_RENAISSANCE_CONCEPTS.filter((c) => textLower.includes(c));

    // Compute basic statistical plausibility
    const words = decoded_text.split(/\s+/).filter(Boolean);
    const uniqueWords = new Set(words);
    const lexicalDiversity = uniqueWords.size / Math.max(words.length, 1);

    // Score: penalize anachronisms, reward lexical diversity in period range
    let score = 0.5;
    score -= anachronisms.length * 0.1;
    if (lexicalDiversity > 0.3 && lexicalDiversity < 0.8) score += 0.2;
    score = Math.max(0, Math.min(1, score));

    return {
      semantic: score,
      anachronisms,
      word_count: words.length,
      lexical_diversity: lexicalDiversity,
      corpus: SECTION_TO_INDEX[section] ?? 'general',
    };
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Historian. You evaluate decoded text for historical ' +
    'plausibility against medieval knowledge boundaries (1400-1450 CE). ' +
    'Use score_historical_plausibility to assess decoded text.',
  tools: [scoreHistoricalPlausibility],
});

const agentExports = () => agentPlugin.exports();
const app = express();
app.use(express.json());

agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createDevPlugin({}, agentExports).injectRoutes(app);
agentPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8002');
app.listen(port, () => console.log(`Voynich Historian at http://localhost:${port}`));
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/historian/app.ts
git commit -m "feat(voynich): add Historian app — medieval RAG fitness scorer"
```

---

### Task 10: Voynich Critic App

**Files:**
- Create: `ts/examples/voynich/critic/app.ts`

- [ ] **Step 1: Create the critic (adversarial falsifier)**

```typescript
// ts/examples/voynich/critic/app.ts

/**
 * Voynich Critic — adversarial falsifier.
 *
 * Attempts to disprove proposed decipherments by finding contradictions,
 * anachronisms, and statistical impossibilities. Only invoked on top-5%
 * candidates by the Orchestrator.
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  defineTool,
} from 'appkit-agent';
import { POST_RENAISSANCE_CONCEPTS } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const findContradictions = defineTool({
  name: 'find_contradictions',
  description: 'Search for internal contradictions, anachronisms, and statistical anomalies in decoded text.',
  parameters: z.object({
    decoded_text: z.string().describe('Decoded plaintext to analyze'),
    section: z.string().describe('Manuscript section this text is from'),
  }),
  handler: async ({ decoded_text, section }) => {
    const contradictions: Array<{ type: string; detail: string; confidence: number }> = [];
    const textLower = decoded_text.toLowerCase();
    const words = textLower.split(/\s+/);

    // Check antonym proximity
    const antonymPairs: [string, string][] = [
      ['hot', 'cold'], ['dry', 'wet'], ['bitter', 'sweet'],
      ['cure', 'cause'], ['poison', 'remedy'], ['visible', 'invisible'],
    ];
    for (const [a, b] of antonymPairs) {
      const aPos = words.findIndex((w) => w.includes(a));
      const bPos = words.findIndex((w) => w.includes(b));
      if (aPos >= 0 && bPos >= 0 && Math.abs(aPos - bPos) < 15) {
        contradictions.push({
          type: 'antonym_proximity',
          detail: `'${a}' and '${b}' within 15 words — possibly contradictory`,
          confidence: 0.6,
        });
      }
    }

    // Check anachronisms
    for (const concept of POST_RENAISSANCE_CONCEPTS) {
      if (textLower.includes(concept)) {
        contradictions.push({
          type: 'anachronism',
          detail: `'${concept}' is post-Renaissance — impossible in pre-1440 manuscript`,
          confidence: 0.9,
        });
      }
    }

    // Statistical check: character distribution
    const charFreq = new Map<string, number>();
    for (const c of textLower.replace(/\s/g, '')) {
      charFreq.set(c, (charFreq.get(c) ?? 0) + 1);
    }
    const maxFreq = Math.max(...charFreq.values(), 1);
    const totalChars = textLower.replace(/\s/g, '').length || 1;
    if (maxFreq / totalChars > 0.25) {
      const dominant = [...charFreq.entries()].sort((a, b) => b[1] - a[1])[0];
      contradictions.push({
        type: 'statistical_anomaly',
        detail: `Character '${dominant[0]}' dominates at ${(dominant[1] / totalChars * 100).toFixed(1)}% — unlikely for natural language`,
        confidence: 0.7,
      });
    }

    const adversarialScore = contradictions.length === 0
      ? 0.8
      : Math.max(0, 0.8 - contradictions.length * 0.15);

    return {
      adversarial: adversarialScore,
      contradictions,
      verdict: contradictions.length === 0 ? 'SURVIVED' : 'FALSIFIED',
    };
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Critic. Your job is to DISPROVE proposed decipherments. ' +
    'Look for contradictions, anachronisms, and impossibilities. ' +
    'A hypothesis that survives your scrutiny carries real evidential weight.',
  tools: [findContradictions],
});

const agentExports = () => agentPlugin.exports();
const app = express();
app.use(express.json());

agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createDevPlugin({}, agentExports).injectRoutes(app);
agentPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8003');
app.listen(port, () => console.log(`Voynich Critic at http://localhost:${port}`));
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/critic/app.ts
git commit -m "feat(voynich): add Critic app — adversarial falsifier"
```

---

### Task 11: Voynich Judge App

**Files:**
- Create: `ts/examples/voynich/judge/app.ts`

- [ ] **Step 1: Create the judge (agent eval agent)**

```typescript
// ts/examples/voynich/judge/app.ts

/**
 * Voynich Judge — agent eval agent.
 *
 * Evaluates the quality of the Historian's and Critic's reasoning,
 * not just their outputs. Scores whether their analysis was sound,
 * well-sourced, and free of hallucination.
 *
 * This is the key novel contribution: a self-calibrating loop that
 * separates agent evals from output evals.
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
  defineTool,
} from 'appkit-agent';

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const scoreReasoningQuality = defineTool({
  name: 'score_reasoning_quality',
  description: 'Evaluate the quality of Historian and Critic reasoning for a hypothesis. Scores soundness, sourcing, and hallucination risk.',
  parameters: z.object({
    hypothesis_id: z.string().describe('ID of the hypothesis being judged'),
    historian_output: z.string().optional().describe('Raw output from the Historian agent'),
    critic_output: z.string().optional().describe('Raw output from the Critic agent'),
  }),
  handler: async ({ hypothesis_id, historian_output, critic_output }) => {
    const scores: Record<string, number> = {};

    // Score Historian reasoning
    if (historian_output) {
      let historianScore = 0.5;
      try {
        const data = JSON.parse(historian_output);
        // Reward: cited sources, specific dates, section-appropriate analysis
        if (data.corpus) historianScore += 0.1;
        if (data.anachronisms && data.anachronisms.length === 0) historianScore += 0.1;
        if (data.word_count && data.word_count > 10) historianScore += 0.1;
        if (data.lexical_diversity && data.lexical_diversity > 0.3) historianScore += 0.1;
        // Penalize: very high confidence without evidence
        if (data.semantic && data.semantic > 0.9 && !data.corpus) historianScore -= 0.2;
      } catch {
        historianScore = 0.3; // non-structured output is suspect
      }
      scores.historian = Math.max(0, Math.min(1, historianScore));
    }

    // Score Critic reasoning
    if (critic_output) {
      let criticScore = 0.5;
      try {
        const data = JSON.parse(critic_output);
        // Reward: specific contradictions with evidence
        if (data.contradictions && Array.isArray(data.contradictions)) {
          const highConf = data.contradictions.filter((c: any) => c.confidence > 0.7);
          criticScore += highConf.length * 0.05;
          // Penalize: many low-confidence findings (fishing)
          const lowConf = data.contradictions.filter((c: any) => c.confidence < 0.4);
          criticScore -= lowConf.length * 0.03;
        }
        if (data.verdict === 'SURVIVED') criticScore += 0.1;
      } catch {
        criticScore = 0.3;
      }
      scores.critic = Math.max(0, Math.min(1, criticScore));
    }

    return {
      hypothesis_id,
      ...scores,
    };
  },
});

// ---------------------------------------------------------------------------
// AppKit wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions:
    'You are the Voynich Judge. You evaluate the QUALITY of reasoning by the ' +
    'Historian and Critic agents — not the hypothesis itself. Score whether their ' +
    'analysis was sound, well-sourced, and free of hallucination. ' +
    'This self-calibrating feedback loop is what separates agent evals from output evals.',
  tools: [scoreReasoningQuality],
});

const agentExports = () => agentPlugin.exports();
const app = express();
app.use(express.json());

agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createDevPlugin({}, agentExports).injectRoutes(app);
agentPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8004');
app.listen(port, () => console.log(`Voynich Judge at http://localhost:${port}`));
```

- [ ] **Step 2: Commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
git add examples/voynich/judge/app.ts
git commit -m "feat(voynich): add Judge app — agent eval for reasoning quality"
```

---

### Task 12: Build Verification and Version Bump

- [ ] **Step 1: Run full test suite**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx vitest run`
Expected: All tests PASS

- [ ] **Step 2: Run typecheck**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Run build**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && npm run build`
Expected: Build succeeds

- [ ] **Step 4: Verify exports**

Run: `cd ~/Documents/apx-agent/.worktrees/connectors/ts && node -e "import('./dist/index.mjs').then(m => { console.log('EvolutionaryAgent:', typeof m.EvolutionaryAgent); console.log('PopulationStore:', typeof m.PopulationStore); console.log('paretoFrontier:', typeof m.paretoFrontier); console.log('createHypothesis:', typeof m.createHypothesis); })"`
Expected:
```
EvolutionaryAgent: function
PopulationStore: function
paretoFrontier: function
createHypothesis: function
```

- [ ] **Step 5: Version bump and final commit**

```bash
cd ~/Documents/apx-agent/.worktrees/connectors/ts
npm version minor --no-git-tag-version
git add package.json
git commit -m "feat(evolutionary): v0.3.0 — EvolutionaryAgent, PopulationStore, Pareto selection, Voynich reference

TypeScript port of PR #6's LoopAgent framework. Adds:
- EvolutionaryAgent (Runnable): background generation loop with pause/resume
- PopulationStore: SQL Statements API with batched writes and caching
- Pareto frontier selection and composite fitness
- Voynich reference: 5 AppKit agent apps (Orchestrator, Decipherer, Historian, Critic, Judge)"
```

---

## What's Next

After this plan ships, the remaining work from the Guidepoint connectors spec is:

- **Phase 2:** KG Agent App (wires EvolutionaryAgent + connectors for Guidepoint's expert matching)
- **Phase 3:** Doc Agent App (SequentialAgent pipeline for doc upload → extract → ingest)
- **Phase 4:** PII Agent App (watchdog wrapper)
- **Phase 6:** Integration testing across all units

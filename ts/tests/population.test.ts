import { describe, it, expect, vi, beforeEach } from 'vitest';
import { PopulationStore } from '../src/workflows/population.js';
import { createHypothesis } from '../src/workflows/hypothesis.js';

// ---------------------------------------------------------------------------
// Mock helpers
// ---------------------------------------------------------------------------

function makeSuccessResponse(
  columns: string[],
  rows: Array<Array<string | null>>,
) {
  return {
    statement_id: 'stmt-1',
    status: { state: 'SUCCEEDED' },
    manifest: {
      schema: {
        columns: columns.map((name) => ({ name })),
      },
    },
    result: { data_array: rows },
  };
}

const HYPOTHESIS_COLUMNS = [
  'id',
  'generation',
  'parent_id',
  'fitness',
  'metadata',
  'flagged_for_review',
  'created_at',
];

function makeHypothesisRow(
  id: string,
  generation: number,
  fitness: Record<string, number>,
  metadata: Record<string, unknown> = {},
  flagged = false,
): Array<string | null> {
  return [
    id,
    String(generation),
    '',
    JSON.stringify(fitness),
    JSON.stringify(metadata),
    String(flagged),
    '2026-01-01T00:00:00Z',
  ];
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const storeConfig = {
  host: 'https://test-host.databricks.com',
  populationTable: 'main.voynich.population',
  warehouseId: 'wh-123',
  chunkSize: 3,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PopulationStore', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    process.env.DATABRICKS_TOKEN = 'test-token';
  });

  // -------------------------------------------------------------------------
  // writeHypotheses
  // -------------------------------------------------------------------------

  describe('writeHypotheses', () => {
    it('chunks inserts into batches of chunkSize (7 hypotheses → 3 API calls)', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse([], []),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const hypotheses = Array.from({ length: 7 }, (_, i) =>
        createHypothesis({ generation: 0, fitness: { score: i * 0.1 } }),
      );

      await store.writeHypotheses(hypotheses);

      // 7 items with chunkSize=3 → 3 batches (3+3+1)
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });

    it('sends INSERT INTO with the correct table name', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse([], []),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const hypotheses = [createHypothesis({ generation: 0 })];

      await store.writeHypotheses(hypotheses);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { statement: string };
      expect(body.statement).toContain('INSERT INTO main.voynich.population');
    });

    it('JSON-stringifies fitness and metadata in the INSERT', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse([], []),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const h = createHypothesis({
        generation: 1,
        fitness: { accuracy: 0.9, recall: 0.8 },
        metadata: { note: 'test' },
      });

      await store.writeHypotheses([h]);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { statement: string };
      expect(body.statement).toContain('"accuracy"');
      expect(body.statement).toContain('"note"');
    });

    it('invalidates cache after writing (next loadGeneration hits API)', async () => {
      // First response: loadGeneration → 1 row
      const loadResp = makeSuccessResponse(
        HYPOTHESIS_COLUMNS,
        [makeHypothesisRow('id1', 0, { a: 0.5 })],
      );
      // Write response: empty
      const writeResp = makeSuccessResponse([], []);
      // Second load response after invalidation: same row
      const loadResp2 = makeSuccessResponse(
        HYPOTHESIS_COLUMNS,
        [makeHypothesisRow('id1', 0, { a: 0.7 })],
      );

      const mockFetch = vi.fn()
        .mockResolvedValueOnce({ ok: true, json: async () => loadResp })    // load #1
        .mockResolvedValueOnce({ ok: true, json: async () => writeResp })   // write
        .mockResolvedValueOnce({ ok: true, json: async () => loadResp2 });  // load #2

      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);

      // Load gen 0 — populates cache
      const first = await store.loadGeneration(0);
      expect(first[0].fitness).toEqual({ a: 0.5 });
      expect(mockFetch).toHaveBeenCalledTimes(1);

      // Write — invalidates cache
      const newH = createHypothesis({ generation: 0 });
      await store.writeHypotheses([newH]);
      expect(mockFetch).toHaveBeenCalledTimes(2);

      // Load gen 0 again — cache is gone, must call API
      const second = await store.loadGeneration(0);
      expect(second[0].fitness).toEqual({ a: 0.7 });
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });
  });

  // -------------------------------------------------------------------------
  // updateFitnessScores
  // -------------------------------------------------------------------------

  describe('updateFitnessScores', () => {
    it('sends one MERGE per update', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse([], []),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const updates = [
        { id: 'abc', fitness: { score: 0.9 } },
        { id: 'def', fitness: { score: 0.8 } },
      ];

      await store.updateFitnessScores(updates);

      expect(mockFetch).toHaveBeenCalledTimes(2);

      const body0 = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { statement: string };
      expect(body0.statement).toContain('MERGE INTO main.voynich.population');
      expect(body0.statement).toContain('abc');

      const body1 = JSON.parse(mockFetch.mock.calls[1][1].body as string) as { statement: string };
      expect(body1.statement).toContain('def');
    });
  });

  // -------------------------------------------------------------------------
  // loadGeneration
  // -------------------------------------------------------------------------

  describe('loadGeneration', () => {
    it('queries by generation number and returns parsed Hypotheses', async () => {
      const rows = [
        makeHypothesisRow('id1', 0, { a: 0.5 }),
        makeHypothesisRow('id2', 0, { a: 0.6 }),
      ];
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse(HYPOTHESIS_COLUMNS, rows),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const results = await store.loadGeneration(0);

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { statement: string };
      expect(body.statement).toContain('WHERE generation = 0');

      expect(results).toHaveLength(2);
      expect(results[0].id).toBe('id1');
      expect(results[0].generation).toBe(0);
      expect(results[0].fitness).toEqual({ a: 0.5 });
      expect(results[1].id).toBe('id2');
    });

    it('caches results — second call to same generation skips API', async () => {
      const rows = [makeHypothesisRow('id1', 2, { b: 0.3 })];
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse(HYPOTHESIS_COLUMNS, rows),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);

      const first = await store.loadGeneration(2);
      const second = await store.loadGeneration(2);

      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(first).toBe(second); // same reference from cache
    });
  });

  // -------------------------------------------------------------------------
  // getFitnessHistory
  // -------------------------------------------------------------------------

  describe('getFitnessHistory', () => {
    it('returns best and avg composite fitness per generation', async () => {
      const rows = [
        makeHypothesisRow('h1', 0, { score: 0.5 }),
        makeHypothesisRow('h2', 0, { score: 0.9 }),
        makeHypothesisRow('h3', 1, { score: 0.7 }),
      ];
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse(HYPOTHESIS_COLUMNS, rows),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const history = await store.getFitnessHistory(10, { score: 1.0 });

      expect(history).toHaveLength(2);

      const gen0 = history.find((h) => h.generation === 0)!;
      expect(gen0.best).toBeCloseTo(0.9);
      expect(gen0.avg).toBeCloseTo(0.7);

      const gen1 = history.find((h) => h.generation === 1)!;
      expect(gen1.best).toBeCloseTo(0.7);
      expect(gen1.avg).toBeCloseTo(0.7);
    });

    it('returns only the last nGenerations entries', async () => {
      const rows = [
        makeHypothesisRow('h1', 0, { score: 0.5 }),
        makeHypothesisRow('h2', 1, { score: 0.6 }),
        makeHypothesisRow('h3', 2, { score: 0.7 }),
        makeHypothesisRow('h4', 3, { score: 0.8 }),
      ];
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse(HYPOTHESIS_COLUMNS, rows),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);
      const history = await store.getFitnessHistory(2, { score: 1.0 });

      expect(history).toHaveLength(2);
      expect(history[0].generation).toBe(2);
      expect(history[1].generation).toBe(3);
    });
  });

  // -------------------------------------------------------------------------
  // clearCache
  // -------------------------------------------------------------------------

  describe('clearCache', () => {
    it('forces next loadGeneration to hit the API', async () => {
      const rows = [makeHypothesisRow('id1', 5, { x: 1.0 })];
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeSuccessResponse(HYPOTHESIS_COLUMNS, rows),
      });
      vi.stubGlobal('fetch', mockFetch);

      const store = new PopulationStore(storeConfig);

      // Prime the cache
      await store.loadGeneration(5);
      expect(mockFetch).toHaveBeenCalledTimes(1);

      // Second call — from cache
      await store.loadGeneration(5);
      expect(mockFetch).toHaveBeenCalledTimes(1);

      // Clear cache — next call must hit API
      store.clearCache();
      await store.loadGeneration(5);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });
  });
});

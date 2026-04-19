/**
 * Durable-execution tests for EvolutionaryAgent.
 *
 * Phase 2 of durable workflows: verify that an agent constructed with an
 * existing runId resumes from the last completed generation without
 * re-invoking already-completed phase handlers, and that pause state
 * survives a simulated process restart.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EvolutionaryAgent } from '../src/workflows/evolutionary.js';
import type { EvolutionaryConfig } from '../src/workflows/evolutionary.js';
import type { PopulationStore } from '../src/workflows/population.js';
import type { Hypothesis } from '../src/workflows/hypothesis.js';
import { createHypothesis } from '../src/workflows/hypothesis.js';
import { InMemoryEngine } from '../src/workflows/engine-memory.js';

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

interface MockStore {
  store: PopulationStore;
  writeCount: { value: number };
  loadTopCount: { value: number };
  flagCount: { value: number };
}

function createInstrumentedStore(): MockStore {
  const hypotheses = new Map<number, Hypothesis[]>();
  const writeCount = { value: 0 };
  const loadTopCount = { value: 0 };
  const flagCount = { value: 0 };

  const store = {
    writeHypotheses: vi.fn(async (h: Hypothesis[]) => {
      writeCount.value++;
      for (const hyp of h) {
        const arr = hypotheses.get(hyp.generation) ?? [];
        arr.push(hyp);
        hypotheses.set(hyp.generation, arr);
      }
    }),
    updateFitnessScores: vi.fn(async () => {}),
    flagForReview: vi.fn(async () => {
      flagCount.value++;
    }),
    loadGeneration: vi.fn(async (gen: number) => hypotheses.get(gen) ?? []),
    loadTopSurvivors: vi.fn(async (gen: number, topN: number) => {
      loadTopCount.value++;
      // Seed generation 0 with a parent so mutation has something to operate on.
      if (gen === 0 && !hypotheses.has(0)) {
        hypotheses.set(0, [
          createHypothesis({ generation: 0, fitness: { score: 0.5 } }),
        ]);
      }
      return (hypotheses.get(gen) ?? []).slice(0, topN);
    }),
    getFitnessHistory: vi.fn(async () => []),
    getActiveConstraints: vi.fn(async () => []),
    clearCache: vi.fn(),
  } as unknown as PopulationStore;

  return { store, writeCount, loadTopCount, flagCount };
}

function makeConfig(
  store: PopulationStore,
  overrides?: Partial<EvolutionaryConfig>,
): EvolutionaryConfig {
  return {
    store,
    populationSize: 3,
    mutationBatch: 2,
    mutationAgent: 'http://mutation-agent',
    fitnessAgents: ['http://fitness-agent'],
    paretoObjectives: ['score'],
    fitnessWeights: { score: 1.0 },
    maxGenerations: 3,
    convergencePatience: 50,
    convergenceThreshold: 0.001,
    ...overrides,
  };
}

/**
 * Wait until the agent's loop promise settles, or the state becomes terminal.
 * Needed because startLoop returns synchronously but the loop is async.
 */
async function waitForTerminalState(agent: EvolutionaryAgent, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (['completed', 'converged', 'failed', 'cancelled', 'paused'].includes(agent.getState())) {
      if (agent.loopPromise) await agent.loopPromise;
      return;
    }
    await new Promise((r) => setTimeout(r, 5));
  }
  if (agent.loopPromise) await agent.loopPromise;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('EvolutionaryAgent — durable execution', () => {
  beforeEach(() => {
    vi.restoreAllMocks();

    // Stub fetch: mutation returns a new hypothesis; fitness agents return scores.
    let fetchCount = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      fetchCount++;
      if (url.includes('mutation-agent')) {
        return {
          ok: true,
          json: async () => [
            createHypothesis({ generation: 1, fitness: { score: 0.7 } }),
            createHypothesis({ generation: 1, fitness: { score: 0.8 } }),
          ],
        };
      }
      return {
        ok: true,
        json: async () => ({ score: 0.75 + (fetchCount % 5) * 0.01 }),
      };
    }));
  });

  it('persists a runId and tracks it through the engine', async () => {
    const engine = new InMemoryEngine();
    const { store } = createInstrumentedStore();

    const agent = new EvolutionaryAgent(
      makeConfig(store, { engine, maxGenerations: 1 }),
    );

    await agent.run([{ role: 'user', content: 'start' }]);
    await waitForTerminalState(agent);

    const runs = await engine.listRuns({ workflowName: 'evolutionary' });
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toMatch(/completed|converged/);
  });

  it('resume with same runId skips completed generations', async () => {
    const engine = new InMemoryEngine();
    const { store: store1, writeCount: writes1 } = createInstrumentedStore();

    const agent1 = new EvolutionaryAgent(
      makeConfig(store1, { engine, maxGenerations: 2 }),
    );
    await agent1.run([{ role: 'user', content: 'start' }]);
    await waitForTerminalState(agent1);

    const runs = await engine.listRuns();
    const runId = runs[0].runId;
    const writesDuringAgent1 = writes1.value;
    const historyFromAgent1 = [...agent1.history];

    expect(historyFromAgent1.length).toBeGreaterThan(0);
    expect(writesDuringAgent1).toBeGreaterThan(0);

    // Simulate a restart: fresh store instance (with fresh counters), same
    // engine + runId. Real resumption would hit a shared PopulationStore
    // table; here we just confirm the agent does NOT re-call writeHypotheses
    // for already-finalized generations, because those step outputs replay
    // from the engine cache.
    const { store: store2, writeCount: writes2 } = createInstrumentedStore();

    const agent2 = new EvolutionaryAgent(
      makeConfig(store2, { engine, runId, maxGenerations: 2 }),
    );

    // Read state by invoking run() which triggers ensureInitialized.
    const message = await agent2.run([{ role: 'user', content: 'status' }]);
    await waitForTerminalState(agent2);

    // History rebuilt from persisted finalize-* steps.
    expect(agent2.history.map((h) => h.generation)).toEqual(
      historyFromAgent1.map((h) => h.generation),
    );
    // Since both generations had already been finalized on agent1, agent2's
    // loop has nothing left to run → zero new writes to the store.
    expect(writes2.value).toBe(0);
    // Terminal summary should reflect resumption, not a fresh start.
    expect(message.toLowerCase()).not.toContain('generation 0');
  });

  it('persists step log entries for each generation phase', async () => {
    const engine = new InMemoryEngine();
    const { store } = createInstrumentedStore();

    const agent = new EvolutionaryAgent(
      makeConfig(store, { engine, maxGenerations: 1 }),
    );
    await agent.run([{ role: 'user', content: 'start' }]);
    await waitForTerminalState(agent);

    const runId = (await engine.listRuns())[0].runId;
    const snapshot = await engine.getRun(runId);
    const stepKeys = snapshot?.steps.map((s) => s.stepKey).sort() ?? [];

    // Each completed generation should produce load/mutate/evaluate/judge/
    // write/finalize entries. Failures in any phase would leave gaps.
    for (const phase of ['load-0', 'mutate-0', 'evaluate-0', 'judge-0', 'write-0', 'finalize-0']) {
      expect(stepKeys).toContain(phase);
    }
  });

  it('pause state persists across restart', async () => {
    const engine = new InMemoryEngine();
    const { store } = createInstrumentedStore();

    const agent1 = new EvolutionaryAgent(
      makeConfig(store, { engine, maxGenerations: 1 }),
    );
    await agent1.run([{ role: 'user', content: 'start' }]);
    await waitForTerminalState(agent1);

    // Simulate an external pause after the run has completed once, then
    // reopen. The agent2 instance should see the persisted state.
    const runId = (await engine.listRuns())[0].runId;
    await engine.finishRun(runId, 'paused');

    const snapshot = await engine.getRun(runId);
    expect(snapshot?.status).toBe('paused');

    const agent2 = new EvolutionaryAgent(
      makeConfig(store, { engine, runId, maxGenerations: 1 }),
    );
    // Initialize agent2 without triggering a new loop.
    await agent2.run([{ role: 'user', content: 'status' }]);
    expect(agent2.history).toHaveLength(1);
  });

  it('defaults to a fresh InMemoryEngine when none provided', async () => {
    const { store } = createInstrumentedStore();

    const agent = new EvolutionaryAgent(
      makeConfig(store, { maxGenerations: 1 }),
    );
    expect(agent.getState()).toBe('idle');

    await agent.run([{ role: 'user', content: 'start' }]);
    await waitForTerminalState(agent);

    // No engine passed, but ensureInitialized still runs and persists to the
    // default in-memory engine. Agent should reach a terminal state.
    expect(['completed', 'converged']).toContain(agent.getState());
  });
});

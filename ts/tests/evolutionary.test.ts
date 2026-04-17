/**
 * Tests for EvolutionaryAgent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EvolutionaryAgent } from '../src/workflows/evolutionary.js';
import type { EvolutionaryConfig } from '../src/workflows/evolutionary.js';
import type { PopulationStore } from '../src/workflows/population.js';
import type { Hypothesis } from '../src/workflows/hypothesis.js';
import { createHypothesis } from '../src/workflows/hypothesis.js';

// ---------------------------------------------------------------------------
// Mock PopulationStore
// ---------------------------------------------------------------------------

function createMockStore() {
  const hypotheses = new Map<number, Hypothesis[]>();
  return {
    writeHypotheses: vi.fn(async (h: Hypothesis[]) => {
      for (const hyp of h) {
        const arr = hypotheses.get(hyp.generation) ?? [];
        arr.push(hyp);
        hypotheses.set(hyp.generation, arr);
      }
    }),
    updateFitnessScores: vi.fn(async () => {}),
    loadGeneration: vi.fn(async (gen: number) => hypotheses.get(gen) ?? []),
    loadTopSurvivors: vi.fn(async (gen: number, topN: number) =>
      (hypotheses.get(gen) ?? []).slice(0, topN),
    ),
    getFitnessHistory: vi.fn(async () => []),
    getActiveConstraints: vi.fn(async () => []),
    clearCache: vi.fn(),
  } as unknown as PopulationStore;
}

// ---------------------------------------------------------------------------
// Minimal config factory
// ---------------------------------------------------------------------------

function makeConfig(store: PopulationStore, overrides?: Partial<EvolutionaryConfig>): EvolutionaryConfig {
  return {
    store,
    populationSize: 5,
    mutationBatch: 3,
    mutationAgent: 'http://mutation-agent',
    fitnessAgents: ['http://fitness-agent'],
    paretoObjectives: ['score'],
    fitnessWeights: { score: 1.0 },
    maxGenerations: 3,
    convergencePatience: 5,
    convergenceThreshold: 0.001,
    escalationThreshold: 0.85,
    topKAdversarial: 0.05,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('EvolutionaryAgent', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('implements Runnable interface', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(makeConfig(store));

    expect(typeof agent.run).toBe('function');
    expect(typeof agent.stream).toBe('function');
    expect(typeof agent.collectTools).toBe('function');
  });

  it('collectTools returns 6 evolution tools', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(makeConfig(store));

    const tools = agent.collectTools();
    expect(tools).toHaveLength(6);

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

    // Seed gen 0 with some hypotheses
    const seedHypotheses = [
      createHypothesis({ generation: 0, fitness: { score: 0.5 } }),
      createHypothesis({ generation: 0, fitness: { score: 0.6 } }),
    ];
    (store.loadTopSurvivors as ReturnType<typeof vi.fn>).mockResolvedValue(seedHypotheses);

    // Mock fetch for agent calls
    const mutatedHypothesis = createHypothesis({ generation: 1, fitness: { score: 0.7 } });
    const mockFetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => [mutatedHypothesis],
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ score: 0.75 }),
      });
    vi.stubGlobal('fetch', mockFetch);

    const agent = new EvolutionaryAgent(makeConfig(store));
    expect(agent.getState()).toBe('idle');

    await agent.run([{ role: 'user', content: 'start' }]);

    // State should transition to running (or completed/converged after rapid finish)
    const state = agent.getState();
    expect(['running', 'completed', 'converged']).toContain(state);
  });

  it('pauses and resumes', async () => {
    const store = createMockStore();

    // Return no survivors to keep generations quick
    (store.loadTopSurvivors as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    const agent = new EvolutionaryAgent(makeConfig(store, { maxGenerations: 100 }));

    // Start the loop
    agent.startLoop();
    expect(['running', 'completed', 'converged']).toContain(agent.getState());

    // Pause
    agent.pauseLoop();
    expect(agent.getState()).toBe('paused');

    // Resume
    agent.resumeLoop();
    expect(agent.getState()).toBe('running');
  });

  it('checkConvergence returns true when fitness is stagnant', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(makeConfig(store, { convergencePatience: 3, convergenceThreshold: 0.001 }));

    const stagnant = [
      { generation: 0, best: 0.9, avg: 0.8 },
      { generation: 1, best: 0.9, avg: 0.8 },
      { generation: 2, best: 0.9, avg: 0.8 },
    ];

    expect(agent.checkConvergence(stagnant)).toBe(true);
  });

  it('checkConvergence returns false when fitness is improving', () => {
    const store = createMockStore();
    const agent = new EvolutionaryAgent(makeConfig(store, { convergencePatience: 3, convergenceThreshold: 0.001 }));

    const improving = [
      { generation: 0, best: 0.5, avg: 0.4 },
      { generation: 1, best: 0.7, avg: 0.6 },
      { generation: 2, best: 0.9, avg: 0.8 },
    ];

    expect(agent.checkConvergence(improving)).toBe(false);
  });
});

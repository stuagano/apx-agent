/**
 * Durable-execution tests for LoopAgent (Phase 3).
 */

import { describe, it, expect } from 'vitest';
import { LoopAgent } from '../src/workflows/loop.js';
import { InMemoryEngine } from '../src/workflows/engine-memory.js';
import type { Runnable, Message } from '../src/workflows/types.js';

function countingAgent(label: string): { agent: Runnable; calls: number[] } {
  const calls: number[] = [];
  const agent: Runnable = {
    run: async () => {
      calls.push(calls.length);
      return `${label}-${calls.length}`;
    },
  };
  return { agent, calls };
}

const START: Message[] = [{ role: 'user', content: 'start' }];

describe('LoopAgent — durable execution', () => {
  it('persists each iteration as an engine step', async () => {
    const engine = new InMemoryEngine();
    const { agent } = countingAgent('r');

    const loop = new LoopAgent(agent, { engine, maxIterations: 3 });
    await loop.run(START);

    const runs = await engine.listRuns({ workflowName: 'loop' });
    expect(runs).toHaveLength(1);

    const snapshot = await engine.getRun(runs[0].runId);
    const iterSteps = (snapshot?.steps ?? [])
      .filter((s) => s.stepKey.startsWith('iter-'))
      .map((s) => s.stepKey)
      .sort();
    expect(iterSteps).toEqual(['iter-0', 'iter-1', 'iter-2']);
  });

  it('resume with same runId replays completed iterations without re-invoking', async () => {
    const engine = new InMemoryEngine();
    const { agent: agent1, calls: calls1 } = countingAgent('r');

    const loop1 = new LoopAgent(agent1, { engine, maxIterations: 3, runId: 'loop-A' });
    const result1 = await loop1.run(START);

    expect(calls1).toHaveLength(3);
    expect(result1).toBe('r-3');

    // Second LoopAgent, fresh agent instance, same engine+runId.
    const { agent: agent2, calls: calls2 } = countingAgent('r');
    const loop2 = new LoopAgent(agent2, { engine, maxIterations: 3, runId: 'loop-A' });
    const result2 = await loop2.run(START);

    // agent2 was never invoked — all iterations replayed from cache.
    expect(calls2).toHaveLength(0);
    expect(result2).toBe('r-3');
  });

  it('resume continues from first uncompleted iteration when maxIterations grows', async () => {
    const engine = new InMemoryEngine();
    const { agent: agent1 } = countingAgent('r');

    const loop1 = new LoopAgent(agent1, { engine, maxIterations: 2, runId: 'loop-B' });
    await loop1.run(START);

    // Now bump maxIterations. agent2 only runs for the *new* iterations.
    const { agent: agent2, calls: calls2 } = countingAgent('r');
    const loop2 = new LoopAgent(agent2, { engine, maxIterations: 5, runId: 'loop-B' });
    const result2 = await loop2.run(START);

    expect(calls2).toHaveLength(3); // iters 2, 3, 4 — not 0 and 1
    expect(result2).toBe('r-3');
  });

  it('stopWhen replay short-circuits on cached results', async () => {
    const engine = new InMemoryEngine();
    let counter = 0;
    const agent: Runnable = {
      run: async () => `iter-${++counter}`,
    };

    const loop1 = new LoopAgent(agent, {
      engine,
      runId: 'loop-C',
      maxIterations: 10,
      stopWhen: (result) => result.includes('3'),
    });
    const result1 = await loop1.run(START);
    expect(result1).toBe('iter-3');

    // Resume with the same stop predicate — replay should stop at the cached
    // iter-3 without invoking agent at all.
    const agent2Calls: number[] = [];
    const agent2: Runnable = {
      run: async () => {
        agent2Calls.push(1);
        return 'should-not-run';
      },
    };
    const loop2 = new LoopAgent(agent2, {
      engine,
      runId: 'loop-C',
      maxIterations: 10,
      stopWhen: (result) => result.includes('3'),
    });
    const result2 = await loop2.run(START);
    expect(result2).toBe('iter-3');
    expect(agent2Calls).toHaveLength(0);
  });

  it('defaults to a fresh InMemoryEngine when none provided', async () => {
    const { agent } = countingAgent('r');
    const loop = new LoopAgent(agent, { maxIterations: 2 });
    const result = await loop.run(START);
    expect(result).toBe('r-2');
  });
});

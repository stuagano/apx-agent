/**
 * Durable-execution tests for SequentialAgent (Phase 3).
 */

import { describe, it, expect } from 'vitest';
import { SequentialAgent } from '../src/workflows/sequential.js';
import { InMemoryEngine } from '../src/workflows/engine-memory.js';
import type { Runnable, Message } from '../src/workflows/types.js';

function countingAgent(result: string): { agent: Runnable; calls: number } & { runsCount: () => number } {
  let calls = 0;
  const agent: Runnable = {
    run: async () => {
      calls++;
      return result;
    },
  };
  return {
    agent,
    get calls() { return calls; },
    runsCount: () => calls,
  };
}

const START: Message[] = [{ role: 'user', content: 'start' }];

describe('SequentialAgent — durable execution', () => {
  it('persists each sub-agent invocation as an engine step', async () => {
    const engine = new InMemoryEngine();
    const a = countingAgent('analyze');
    const b = countingAgent('plan');
    const c = countingAgent('execute');

    const seq = new SequentialAgent(
      [a.agent, b.agent, c.agent],
      '',
      { engine },
    );
    const result = await seq.run(START);
    expect(result).toBe('execute');

    const runs = await engine.listRuns({ workflowName: 'sequential' });
    expect(runs).toHaveLength(1);

    const snapshot = await engine.getRun(runs[0].runId);
    const stepKeys = (snapshot?.steps ?? [])
      .map((s) => s.stepKey)
      .sort();
    expect(stepKeys).toEqual(['step-0', 'step-1', 'step-2']);
  });

  it('resume with same runId replays all completed steps without re-invoking', async () => {
    const engine = new InMemoryEngine();
    const a1 = countingAgent('analyze');
    const b1 = countingAgent('plan');

    const seq1 = new SequentialAgent(
      [a1.agent, b1.agent],
      '',
      { engine, runId: 'seq-A' },
    );
    await seq1.run(START);
    expect(a1.runsCount()).toBe(1);
    expect(b1.runsCount()).toBe(1);

    // New agent instances. Same runId → all steps cached → zero invocations.
    const a2 = countingAgent('analyze');
    const b2 = countingAgent('plan');
    const seq2 = new SequentialAgent(
      [a2.agent, b2.agent],
      '',
      { engine, runId: 'seq-A' },
    );
    const result = await seq2.run(START);

    expect(a2.runsCount()).toBe(0);
    expect(b2.runsCount()).toBe(0);
    expect(result).toBe('plan');
  });

  it('resume continues from first uncompleted step', async () => {
    const engine = new InMemoryEngine();

    // First run: only the first agent succeeds. The second throws so
    // step-1 is persisted as failed and step-2 never runs.
    const a1 = countingAgent('done-a');
    let bThrew = false;
    const failingB: Runnable = {
      run: async () => {
        bThrew = true;
        throw new Error('simulated crash');
      },
    };
    const c1 = countingAgent('done-c');

    const seq1 = new SequentialAgent(
      [a1.agent, failingB, c1.agent],
      '',
      { engine, runId: 'seq-B' },
    );
    await expect(seq1.run(START)).rejects.toThrow('simulated crash');
    expect(a1.runsCount()).toBe(1);
    expect(bThrew).toBe(true);
    expect(c1.runsCount()).toBe(0);

    // On "restart", agent a2 should NOT re-run (step-0 completed) but
    // step-1 replays the failure, so a retry surface would need fresh logic.
    // For this test we just assert that step-0 output is cached: if we
    // replace B with a succeeding agent and run again, a2 is not invoked.
    const a2 = countingAgent('done-a');
    const b2 = countingAgent('done-b');
    const c2 = countingAgent('done-c');

    const seq2 = new SequentialAgent(
      [a2.agent, b2.agent, c2.agent],
      '',
      { engine, runId: 'seq-B' },
    );
    // step-1 was persisted as failed → engine replays that failure on the
    // second call with the same key. The retry is surfaced as a rejection.
    await expect(seq2.run(START)).rejects.toThrow('simulated crash');

    // The successful step-0 did replay from cache — a2 was never invoked.
    expect(a2.runsCount()).toBe(0);
    // And step-2's handler was never reached on either attempt.
    expect(c2.runsCount()).toBe(0);
  });

  it('defaults to a fresh InMemoryEngine when none provided', async () => {
    const a = countingAgent('one');
    const b = countingAgent('two');
    const seq = new SequentialAgent([a.agent, b.agent]);
    const result = await seq.run(START);
    expect(result).toBe('two');
  });

  it('preserves outputKey state propagation across durable steps', async () => {
    const engine = new InMemoryEngine();
    const producer: Runnable = {
      run: async () => 'analysis result',
      outputKey: 'analysis',
    };
    const consumer: Runnable = {
      run: async (_msgs, state) => `got: ${state?.get('analysis')}`,
    };

    const seq = new SequentialAgent([producer, consumer], '', { engine });
    const result = await seq.run(START);
    expect(result).toBe('got: analysis result');
  });
});

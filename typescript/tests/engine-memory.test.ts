/**
 * Tests for InMemoryEngine — the default WorkflowEngine backend.
 *
 * The contract tests here also serve as the reference suite that
 * `DeltaEngine` (Phase 4) must pass.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { InMemoryEngine } from '../src/workflows/engine-memory.js';
import { StepFailedError } from '../src/workflows/engine.js';

describe('InMemoryEngine', () => {
  let engine: InMemoryEngine;

  beforeEach(() => {
    engine = new InMemoryEngine();
  });

  describe('startRun', () => {
    it('creates a run and returns a new runId', async () => {
      const runId = await engine.startRun('wf', { seed: 1 });
      expect(runId).toBeTruthy();

      const snap = await engine.getRun(runId);
      expect(snap?.workflowName).toBe('wf');
      expect(snap?.status).toBe('running');
      expect(snap?.input).toEqual({ seed: 1 });
    });

    it('reuses a provided runId for a fresh run', async () => {
      const runId = await engine.startRun('wf', {}, { runId: 'custom-id' });
      expect(runId).toBe('custom-id');
    });

    it('reopens an existing run and resets status to running', async () => {
      const runId = await engine.startRun('wf', {});
      await engine.finishRun(runId, 'paused');

      const resumed = await engine.startRun('wf', {}, { runId });
      expect(resumed).toBe(runId);

      const snap = await engine.getRun(runId);
      expect(snap?.status).toBe('running');
    });
  });

  describe('step', () => {
    it('invokes the handler on cache miss and persists the output', async () => {
      const runId = await engine.startRun('wf', {});
      let invocations = 0;

      const result = await engine.step(runId, 'a', async () => {
        invocations++;
        return { value: 42 };
      });

      expect(result).toEqual({ value: 42 });
      expect(invocations).toBe(1);

      const snap = await engine.getRun(runId);
      expect(snap?.steps).toHaveLength(1);
      expect(snap?.steps[0]).toMatchObject({
        stepKey: 'a',
        status: 'completed',
        output: { value: 42 },
      });
    });

    it('returns cached output on replay without re-invoking the handler', async () => {
      const runId = await engine.startRun('wf', {});
      let invocations = 0;
      const handler = async () => {
        invocations++;
        return { value: invocations };
      };

      const first = await engine.step(runId, 'a', handler);
      const second = await engine.step(runId, 'a', handler);

      expect(first).toEqual({ value: 1 });
      expect(second).toEqual({ value: 1 });
      expect(invocations).toBe(1);
    });

    it('persists failures and re-throws StepFailedError on replay', async () => {
      const runId = await engine.startRun('wf', {});
      let invocations = 0;
      const handler = async () => {
        invocations++;
        throw new Error('boom');
      };

      await expect(engine.step(runId, 'a', handler)).rejects.toThrow('boom');
      await expect(engine.step(runId, 'a', handler)).rejects.toBeInstanceOf(StepFailedError);
      expect(invocations).toBe(1);
    });

    it('distinguishes steps by key within the same run', async () => {
      const runId = await engine.startRun('wf', {});

      const a = await engine.step(runId, 'a', async () => 1);
      const b = await engine.step(runId, 'b', async () => 2);

      expect(a).toBe(1);
      expect(b).toBe(2);

      const snap = await engine.getRun(runId);
      expect(snap?.steps.map((s) => s.stepKey).sort()).toEqual(['a', 'b']);
    });

    it('isolates steps across runs', async () => {
      const run1 = await engine.startRun('wf', {});
      const run2 = await engine.startRun('wf', {});

      await engine.step(run1, 'shared', async () => 'one');
      const result = await engine.step(run2, 'shared', async () => 'two');

      expect(result).toBe('two');
    });

    it('throws for unknown runId', async () => {
      await expect(
        engine.step('ghost', 'a', async () => 1),
      ).rejects.toThrow(/unknown runid/i);
    });

    it('returns cloned output so callers cannot mutate cached state', async () => {
      const runId = await engine.startRun('wf', {});
      const first = await engine.step<{ count: number }>(runId, 'a', async () => ({ count: 1 }));
      first.count = 99;

      const second = await engine.step<{ count: number }>(runId, 'a', async () => ({ count: 1 }));
      expect(second.count).toBe(1);
    });
  });

  describe('finishRun', () => {
    it('updates status and stores output', async () => {
      const runId = await engine.startRun('wf', {});
      await engine.finishRun(runId, 'completed', { final: true });

      const snap = await engine.getRun(runId);
      expect(snap?.status).toBe('completed');
      expect(snap?.output).toEqual({ final: true });
    });

    it('throws for unknown runId', async () => {
      await expect(engine.finishRun('ghost', 'completed')).rejects.toThrow(/unknown runid/i);
    });
  });

  describe('listRuns', () => {
    it('returns all runs when no filter is given', async () => {
      await engine.startRun('wf-a', {});
      await engine.startRun('wf-b', {});

      const runs = await engine.listRuns();
      expect(runs).toHaveLength(2);
    });

    it('filters by workflowName', async () => {
      await engine.startRun('wf-a', {});
      await engine.startRun('wf-b', {});

      const runs = await engine.listRuns({ workflowName: 'wf-a' });
      expect(runs).toHaveLength(1);
      expect(runs[0].workflowName).toBe('wf-a');
    });

    it('filters by status', async () => {
      const r1 = await engine.startRun('wf', {});
      const r2 = await engine.startRun('wf', {});
      await engine.finishRun(r1, 'completed');

      const running = await engine.listRuns({ status: 'running' });
      expect(running.map((r) => r.runId)).toEqual([r2]);
    });

    it('respects limit', async () => {
      for (let i = 0; i < 5; i++) await engine.startRun('wf', { i });

      const runs = await engine.listRuns({ limit: 2 });
      expect(runs).toHaveLength(2);
    });
  });

  describe('getRun', () => {
    it('returns null for unknown runId', async () => {
      expect(await engine.getRun('ghost')).toBeNull();
    });
  });
});

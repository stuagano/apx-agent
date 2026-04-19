/**
 * InMemoryEngine — default WorkflowEngine backend.
 *
 * Stores runs and step records in a process-local Map. Preserves the
 * workflow API's step-caching and replay semantics so tests can exercise
 * resumption without a SQL warehouse, but loses all state on process exit.
 * Use `DeltaEngine` (Phase 4) for real durability.
 */

import { randomUUID } from 'node:crypto';
import type {
  RunFilter,
  RunSnapshot,
  RunStatus,
  RunSummary,
  StepRecord,
  WorkflowEngine,
} from './engine.js';
import { StepFailedError } from './engine.js';

interface MutableRun {
  runId: string;
  workflowName: string;
  status: RunStatus;
  input: unknown;
  output?: unknown;
  startedAt: string;
  updatedAt: string;
  steps: Map<string, StepRecord>;
}

export class InMemoryEngine implements WorkflowEngine {
  private runs = new Map<string, MutableRun>();

  async startRun(
    workflowName: string,
    input: unknown,
    opts?: { runId?: string },
  ): Promise<string> {
    const now = new Date().toISOString();
    const existing = opts?.runId ? this.runs.get(opts.runId) : undefined;

    if (existing) {
      existing.status = 'running';
      existing.updatedAt = now;
      return existing.runId;
    }

    const runId = opts?.runId ?? randomUUID();
    this.runs.set(runId, {
      runId,
      workflowName,
      status: 'running',
      input: structuredClone(input),
      startedAt: now,
      updatedAt: now,
      steps: new Map(),
    });
    return runId;
  }

  async step<T>(
    runId: string,
    stepKey: string,
    handler: () => Promise<T>,
  ): Promise<T> {
    const run = this.runs.get(runId);
    if (!run) {
      throw new Error(`Unknown runId: ${runId}`);
    }

    const cached = run.steps.get(stepKey);
    if (cached) {
      if (cached.status === 'completed') {
        return structuredClone(cached.output) as T;
      }
      throw new StepFailedError(stepKey, cached.error ?? 'step failed');
    }

    const start = Date.now();
    try {
      const result = await handler();
      const record: StepRecord = {
        stepKey,
        status: 'completed',
        output: structuredClone(result),
        durationMs: Date.now() - start,
        recordedAt: new Date().toISOString(),
      };
      run.steps.set(stepKey, record);
      run.updatedAt = record.recordedAt;
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const record: StepRecord = {
        stepKey,
        status: 'failed',
        error: message,
        durationMs: Date.now() - start,
        recordedAt: new Date().toISOString(),
      };
      run.steps.set(stepKey, record);
      run.updatedAt = record.recordedAt;
      throw err;
    }
  }

  async finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void> {
    const run = this.runs.get(runId);
    if (!run) {
      throw new Error(`Unknown runId: ${runId}`);
    }
    run.status = status;
    if (output !== undefined) {
      run.output = structuredClone(output);
    }
    run.updatedAt = new Date().toISOString();
  }

  async getRun(runId: string): Promise<RunSnapshot | null> {
    const run = this.runs.get(runId);
    if (!run) return null;
    return {
      runId: run.runId,
      workflowName: run.workflowName,
      status: run.status,
      input: structuredClone(run.input),
      output: run.output === undefined ? undefined : structuredClone(run.output),
      startedAt: run.startedAt,
      updatedAt: run.updatedAt,
      steps: Array.from(run.steps.values()).map((s) => ({ ...s, output: structuredClone(s.output) })),
    };
  }

  async listRuns(filter?: RunFilter): Promise<RunSummary[]> {
    let results = Array.from(this.runs.values());
    if (filter?.workflowName) {
      results = results.filter((r) => r.workflowName === filter.workflowName);
    }
    if (filter?.status) {
      results = results.filter((r) => r.status === filter.status);
    }
    results.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
    if (filter?.limit !== undefined) {
      results = results.slice(0, filter.limit);
    }
    return results.map((r) => ({
      runId: r.runId,
      workflowName: r.workflowName,
      status: r.status,
      startedAt: r.startedAt,
      updatedAt: r.updatedAt,
    }));
  }
}

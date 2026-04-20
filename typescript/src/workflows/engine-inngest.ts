/**
 * InngestEngine — optional adapter that routes WorkflowEngine step() calls
 * onto an Inngest step runner.
 *
 * This is a thin shim: it expects the caller to pass an Inngest `step` object
 * (the one received inside an Inngest function handler) at engine construction
 * time. That means this engine only makes sense *inside* an Inngest function —
 * not as a standalone backend. If you want standalone durability without
 * Inngest, use `DeltaEngine`.
 *
 * The run lifecycle methods (startRun / finishRun / getRun / listRuns) are
 * intentionally minimal — Inngest owns the run lifecycle, so those calls
 * just record metadata in-process for observability. If you need to list or
 * query runs, use the Inngest dashboard.
 *
 * @example
 * import { serve } from 'inngest/next';
 * import { Inngest } from 'inngest';
 * import { InngestEngine, EvolutionaryAgent } from 'appkit-agent';
 *
 * const inngest = new Inngest({ id: 'my-app' });
 *
 * const evolveFn = inngest.createFunction(
 *   { id: 'evolve' },
 *   { event: 'evolve/start' },
 *   async ({ event, step }) => {
 *     const engine = new InngestEngine(step);
 *     const agent = new EvolutionaryAgent({ ...config, engine, runId: event.data.runId });
 *     await agent.run([]);
 *   },
 * );
 */

import type {
  RunFilter,
  RunSnapshot,
  RunStatus,
  RunSummary,
  WorkflowEngine,
} from './engine.js';

/** Minimal shape of Inngest's `step` object that this adapter needs. */
export interface InngestStep {
  run<T>(id: string, handler: () => Promise<T>): Promise<T>;
}

export class InngestEngine implements WorkflowEngine {
  private step$: InngestStep;
  private runs = new Map<string, { workflowName: string; status: RunStatus; startedAt: string; updatedAt: string; input: unknown; output?: unknown }>();

  constructor(step: InngestStep) {
    this.step$ = step;
  }

  async startRun(
    workflowName: string,
    input: unknown,
    opts?: { runId?: string },
  ): Promise<string> {
    const runId = opts?.runId ?? `inngest-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const now = new Date().toISOString();
    const existing = this.runs.get(runId);
    if (existing) {
      existing.status = 'running';
      existing.updatedAt = now;
    } else {
      this.runs.set(runId, {
        workflowName,
        status: 'running',
        startedAt: now,
        updatedAt: now,
        input,
      });
    }
    return runId;
  }

  async step<T>(_runId: string, stepKey: string, handler: () => Promise<T>): Promise<T> {
    // Defer caching and replay to Inngest — `step.run` already provides
    // exactly the durability semantics this interface needs.
    return this.step$.run(stepKey, handler);
  }

  async finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void> {
    const run = this.runs.get(runId);
    if (!run) return;
    run.status = status;
    if (output !== undefined) run.output = output;
    run.updatedAt = new Date().toISOString();
  }

  async getRun(runId: string): Promise<RunSnapshot | null> {
    const run = this.runs.get(runId);
    if (!run) return null;
    return {
      runId,
      workflowName: run.workflowName,
      status: run.status,
      input: run.input,
      output: run.output,
      startedAt: run.startedAt,
      updatedAt: run.updatedAt,
      steps: [], // authoritative step log lives in Inngest
    };
  }

  async listRuns(filter?: RunFilter): Promise<RunSummary[]> {
    let results = Array.from(this.runs.entries()).map(([runId, r]) => ({
      runId,
      workflowName: r.workflowName,
      status: r.status,
      startedAt: r.startedAt,
      updatedAt: r.updatedAt,
    }));
    if (filter?.workflowName) results = results.filter((r) => r.workflowName === filter.workflowName);
    if (filter?.status) results = results.filter((r) => r.status === filter.status);
    if (filter?.limit !== undefined) results = results.slice(0, filter.limit);
    return results;
  }
}

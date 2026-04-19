/**
 * WorkflowEngine — durable execution primitive for workflow agents.
 *
 * Each step of a workflow is wrapped in `engine.step(runId, stepKey, handler)`.
 * The engine persists the step's output (or failure) keyed by `(runId, stepKey)`,
 * so a subsequent call with the same key returns the cached result instead of
 * re-invoking the handler. This is what lets a workflow resume after a crash,
 * redeploy, or pause — the completed steps replay from persistence, and the
 * first uncompleted step runs fresh.
 *
 * The interface is intentionally small. Callers invoke `step()` inline around
 * any expensive or non-deterministic operation; there is no decorator, DSL, or
 * build step. This matches the shape of `step.run()` in Inngest and `@DBOS.step`
 * in DBOS.
 *
 * See `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`.
 */

/** Lifecycle status of a workflow run. */
export type RunStatus =
  | 'running'
  | 'paused'
  | 'completed'
  | 'converged'
  | 'failed'
  | 'cancelled';

/** Persisted record of a single step invocation. */
export interface StepRecord {
  stepKey: string;
  status: 'completed' | 'failed';
  output?: unknown;
  error?: string;
  durationMs: number;
  recordedAt: string;
}

/** Full snapshot of a run, including its step log. */
export interface RunSnapshot {
  runId: string;
  workflowName: string;
  status: RunStatus;
  input: unknown;
  output?: unknown;
  startedAt: string;
  updatedAt: string;
  steps: StepRecord[];
}

/** Compact summary returned by listRuns(). */
export interface RunSummary {
  runId: string;
  workflowName: string;
  status: RunStatus;
  startedAt: string;
  updatedAt: string;
}

/** Filter options for listRuns(). */
export interface RunFilter {
  workflowName?: string;
  status?: RunStatus;
  limit?: number;
}

/**
 * Thrown when a handler raised an error that the engine persisted. Replay of
 * a previously failed step re-throws this so callers see the same failure
 * they would have seen originally.
 */
export class StepFailedError extends Error {
  readonly stepKey: string;
  constructor(stepKey: string, message: string) {
    super(message);
    this.name = 'StepFailedError';
    this.stepKey = stepKey;
  }
}

/**
 * Pluggable backend for durable workflow execution.
 *
 * Implementations:
 * - `InMemoryEngine` — per-process Map, default, used in tests and dev.
 * - `DeltaEngine` (Phase 4) — SQL Statements API against a Delta table.
 * - `InngestEngine` (Phase 5) — adapter onto Inngest's step runner.
 */
export interface WorkflowEngine {
  /**
   * Start a new run, or re-open an existing one.
   *
   * If `opts.runId` is provided and an existing run is found, the run is
   * re-opened: status is set back to `running` and subsequent `step()` calls
   * replay from the persisted log. Otherwise, a new run is created.
   *
   * Returns the run's ID.
   */
  startRun(
    workflowName: string,
    input: unknown,
    opts?: { runId?: string },
  ): Promise<string>;

  /**
   * Execute a checkpointed step.
   *
   * - On cache hit with `status = 'completed'`: returns the persisted output
   *   without invoking `handler`.
   * - On cache hit with `status = 'failed'`: re-throws a `StepFailedError`
   *   without invoking `handler`.
   * - On cache miss: invokes `handler`, persists the result (or failure),
   *   then returns or throws.
   *
   * `stepKey` must be stable across replays — e.g. `mutate-${generation}`.
   */
  step<T>(
    runId: string,
    stepKey: string,
    handler: () => Promise<T>,
  ): Promise<T>;

  /** Mark a run finished with a terminal or paused status. */
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;

  /** Read the full snapshot of a run. Returns null if not found. */
  getRun(runId: string): Promise<RunSnapshot | null>;

  /** List runs matching the given filter. */
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}

# Durable Workflow Execution — Design Spec

**Date:** 2026-04-19
**Status:** Draft
**Author:** Stuart Gano
**Depends on:** Existing workflow agents (`SequentialAgent`, `LoopAgent`, `EvolutionaryAgent`, `ParallelAgent`, `RouterAgent`, `HandoffAgent`)

## Overview

Today all workflow agents run **in-process** inside a Databricks App. A generation loop in `EvolutionaryAgent`, a multi-step `SequentialAgent` pipeline, or an iterative `LoopAgent` lives entirely in the FastAPI/Node process that received the triggering request. If the app crashes, redeploys, is scaled down, or the user closes the tab, all intermediate state is lost. Completed work inside a half-finished run is thrown away.

This spec proposes adding a **durable execution layer** — the same pattern Temporal, Inngest, DBOS, and Vercel Workflows implement — underneath the existing `Runnable` interface. Each step of a workflow agent becomes a checkpointable unit whose inputs, outputs, and status persist to a backing store. A crashed workflow resumes from the last successful step; a paused workflow survives restarts; a long-running evolution keeps its generation history across deploys.

The user-facing API (the `Runnable` interface and the existing `SequentialAgent` / `LoopAgent` / `EvolutionaryAgent` classes) does not change. Durability is opt-in via a `WorkflowEngine` passed at construction time.

### Scope

- `WorkflowEngine` interface: pluggable backend for step persistence, replay, and resumption
- Reference backends: `InMemoryEngine` (dev/test, today's behavior), `DeltaEngine` (Delta-backed checkpoints via SQL Statements API), `InngestEngine` (adapter to Inngest/Vercel Workflows)
- Retrofit of `SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` onto the engine — these benefit most
- Public `executeStep()` primitive for custom workflow authors
- Resumption semantics: workflow IDs, replay detection, idempotency keys
- Python and TypeScript parity

### Out of Scope

- Durable execution for short-lived interactive agent loops (`Agent.run()` single turn). Those are request-scoped; durability there would mostly mean HTTP retry logic, which is better handled by the caller.
- `ParallelAgent` / `RouterAgent` / `HandoffAgent`. These are fan-out or routing shapes whose individual branches are themselves workflows — they get durability transitively if their children use the engine, but don't need dedicated checkpointing themselves.
- Cross-region replication, multi-tenant orchestration, or workflow versioning / migration. Out of scope for v1.
- A new workflow DSL. We are not introducing graph builders, decorators, or a compile step. Existing imperative workflow code stays imperative.

## Motivation

The current workflow agents have three weaknesses that compound as workflows get longer:

**1. Lost work on crash.** `EvolutionaryAgent.runLoop()` in `typescript/src/workflows/evolutionary.ts:149` holds `currentGeneration`, `history[]`, and `state` in instance fields. A 500-generation evolution that crashes at generation 347 throws away the full history — only the persisted `PopulationStore` rows survive, and the agent has no way to rebuild the derived `history` array from them on restart.

**2. Redeploys kill in-flight runs.** Databricks Apps restart on every deploy. Any user who kicked off a long-running `SequentialAgent` pipeline (analyze → plan → execute with a 3-minute plan step) sees it silently abort when someone pushes a code change.

**3. Pause/resume doesn't survive process death.** `EvolutionaryAgent.pauseLoop()` flips `this.state = 'paused'`, but that state vanishes with the process. The `pause_evolution` conversational tool becomes a lie on any restart.

Durable execution solves all three by moving the workflow state machine out of process memory and into a store that outlives the process.

## Landscape

We surveyed five candidate execution models. Each column below is a real tradeoff, not a vendor pitch.

| Option | Where it runs | Persistence | Databricks fit | Pulls in |
|---|---|---|---|---|
| **Temporal** | Dedicated Temporal cluster (self-hosted or Cloud) | Built-in event history | External service; SDK-heavy | Temporal SDK, worker processes |
| **Inngest** | Inngest Cloud + HTTP webhooks | Event history in Inngest | SaaS, network-bound; simple model | `inngest` SDK, outbound HTTP only |
| **Vercel Workflows** | Vercel infra | Event history in Vercel | Tied to Vercel deploy target | Next.js-adjacent |
| **DBOS** | Postgres + app process | App writes to Postgres directly | Runs in-process; needs Postgres (Lakebase works) | `dbos-transact` SDK |
| **Delta + cron** | App process; Delta table for state | Delta table via SQL Statements API | Native; already used by `PopulationStore` | Nothing new |

**Our read:** Vercel Workflows is a great product but specific to Vercel's deploy target. Databricks Apps is our actual deploy target, so we cannot adopt it directly. The closest model we *can* adopt is either (a) a thin Delta-backed engine using the SQL Statements API the framework already speaks, or (b) an Inngest adapter for teams that want to offload orchestration entirely.

**Recommendation:** Ship two backends in v1.

- `DeltaEngine` — default, zero new infra, uses the same SQL Statements API as `PopulationStore`. Good enough for minute-to-hour workflows with dozens to hundreds of steps.
- `InngestEngine` — optional adapter, for teams that want battle-tested orchestration semantics (millisecond step replay, rich retry policies, fan-out at scale).

An abstraction boundary at the right place lets us add Temporal / DBOS / others later without changing the workflow classes.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  User code                                                  │
│    new EvolutionaryAgent({ store, engine: deltaEngine })    │
└─────────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────────┐
│  Workflow agent (Runnable)                                  │
│    runGeneration()                                          │
│      └─► engine.step('mutate', inputs, () => mutate(...))   │
│      └─► engine.step('evaluate', ...)                       │
│      └─► engine.step('judge', ...)                          │
└─────────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────────┐
│  WorkflowEngine (pluggable)                                 │
│    • Checks: has this step already completed?               │
│    • If yes: return persisted output (replay)               │
│    • If no:  run handler → persist result → return          │
└─────────────────────────────────────────────────────────────┘
                           │
      ┌────────────────────┴────────────────────┐
      │                                         │
┌──────────────┐                     ┌─────────────────────┐
│ DeltaEngine  │                     │ InngestEngine       │
│  SQL Stmt    │                     │  HTTP + webhook     │
│  API inserts │                     │  callback           │
│  per step    │                     │                     │
└──────────────┘                     └─────────────────────┘
```

### The `WorkflowEngine` interface

```typescript
export interface WorkflowEngine {
  /**
   * Execute a checkpointed step within a workflow run.
   *
   * Contract:
   * - If (runId, stepKey) already has a persisted result, return it without
   *   invoking `handler`. This is how replay works.
   * - Otherwise, invoke `handler()`, persist the result, and return it.
   * - On handler failure, persist the failure and surface the error. Subsequent
   *   calls with the same stepKey return the failure (idempotent), unless the
   *   caller explicitly retries.
   *
   * `stepKey` must be stable across replays. Callers typically use
   * `${phase}-${generation}` or a hash of inputs.
   */
  step<T>(
    runId: string,
    stepKey: string,
    handler: () => Promise<T>,
  ): Promise<T>;

  /** Start a new run, or resume an existing one. Returns the runId. */
  startRun(workflowName: string, input: unknown, opts?: { runId?: string }): Promise<string>;

  /** Mark a run as completed, converged, cancelled, or failed. */
  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;

  /** Read the full step log for a run — for observability and replay debugging. */
  getRun(runId: string): Promise<RunSnapshot>;

  /** List runs by workflow name and/or status. */
  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}

type RunStatus = 'running' | 'paused' | 'completed' | 'converged' | 'failed' | 'cancelled';
```

The `step()` primitive is intentionally small. It is *not* a decorator, not a generator, not a DSL. Authors call it inline — exactly like Inngest's `step.run()` or DBOS's `@DBOS.step`. Everything else (retries, timeouts, human-in-loop waits) is layered on top.

### Retrofit example: `EvolutionaryAgent`

The current `runGeneration()` ([evolutionary.ts:166](../../typescript/src/workflows/evolutionary.ts)) becomes:

```typescript
private async runGeneration(gen: number): Promise<GenerationResult> {
  const parents = await this.engine.step(this.runId, `load-${gen}`,
    () => this.config.store.loadTopSurvivors(gen - 1, this.config.populationSize, this.config.fitnessWeights));

  const candidates = await this.engine.step(this.runId, `mutate-${gen}`,
    () => this.mutate(parents, gen));

  const evaluated = await this.engine.step(this.runId, `evaluate-${gen}`,
    () => this.evaluate(candidates));

  const judged = await this.engine.step(this.runId, `judge-${gen}`,
    () => this.judge(evaluated));

  await this.engine.step(this.runId, `write-${gen}`,
    () => this.config.store.writeHypotheses(judged));

  // ... select, escalate, convergence check — each wrapped similarly
}
```

The diff is mechanical: every `await this.X(...)` that does meaningful work becomes `await this.engine.step(runId, stepKey, () => this.X(...))`. Nothing about mutation, evaluation, or judging changes. Developers still write imperative async code.

On restart, `startRun(..., { runId: existingRunId })` is called with the same `runId`. The first `step()` that was previously completed returns cached output instantly; the first uncompleted step re-runs. The agent resumes exactly where it stopped.

### Resumption model

We adopt the **event-sourced replay** model used by Temporal, Inngest, and DBOS. A run's persisted log of step results is the source of truth. Replay is deterministic as long as:

1. Step keys are stable (same generation → same key).
2. Non-deterministic ops (random IDs, `Date.now()`, external API calls) happen *inside* a `step()` so their outputs are pinned.

This is the same discipline durable execution frameworks universally require. Violations produce replay divergence — which the engine detects by comparing expected vs observed step keys on replay and failing loudly.

### `DeltaEngine` schema

Two tables, both partitioned by `workflow_name`:

```sql
CREATE TABLE apx_agent.workflow_runs (
  run_id        STRING NOT NULL,
  workflow_name STRING NOT NULL,
  status        STRING NOT NULL,    -- running | paused | completed | ...
  input         STRING,             -- JSON
  output        STRING,             -- JSON
  started_at    TIMESTAMP NOT NULL,
  updated_at    TIMESTAMP NOT NULL
);

CREATE TABLE apx_agent.workflow_steps (
  run_id    STRING NOT NULL,
  step_key  STRING NOT NULL,
  status    STRING NOT NULL,        -- completed | failed
  output    STRING,                 -- JSON
  error     STRING,
  duration_ms BIGINT,
  recorded_at TIMESTAMP NOT NULL,
  PRIMARY KEY (run_id, step_key)
);
```

Writes go through the SQL Statements API using the same chunked / cached pattern `PopulationStore` already uses ([typescript/src/workflows/population.ts](../../typescript/src/workflows/population.ts)). Reads for `step()` replay are point lookups on `(run_id, step_key)` — expected to hit the cache on warm runs.

### `InngestEngine` adapter

A thin wrapper that maps `engine.step(runId, key, handler)` onto Inngest's `step.run(key, handler)` and exposes Inngest event delivery as the transport. Teams that already run Inngest pay the SDK cost; teams that don't, don't.

## Python parity

The Python workflow primitives live in `python/src/apx_agent/workflow/`. The same interface ships there — `WorkflowEngine` protocol, `execute_step()` context manager, `DeltaEngine` using the same SQL Statements API. `LoopAgent` in `python/src/apx_agent/workflow/loop_agent.py` gets the retrofit first since it's the largest consumer.

## OBO and auth

Durable workflows interact with the `resolveToken()` fallback chain described in the README. For durable runs, the **M2M fallback** is the right path: a run that resumes hours later cannot count on the original user's OBO token still being valid. Workflow authors should either:

- Store the user identity on the run record and let downstream calls resolve M2M credentials (Databricks service-principal OAuth), or
- Snapshot any auth-derived data (workspace client results) inside an early `step()` so the persisted output can be replayed without re-authenticating.

This matches how the existing `EvolutionaryAgent` already operates — it runs as a service principal during background generations, with OBO propagation only on the conversational tool calls.

## Migration strategy

1. Ship `WorkflowEngine` interface and `InMemoryEngine` as a no-op default. Zero behavior change for existing users.
2. Ship `DeltaEngine` behind an opt-in constructor arg. Retrofit `EvolutionaryAgent` first — highest leverage, lowest risk (it already has a durable store for its core data).
3. Retrofit `LoopAgent` and `SequentialAgent`.
4. Ship `InngestEngine` as an optional package.
5. Leave `ParallelAgent`, `RouterAgent`, `HandoffAgent` alone unless a concrete use case surfaces.

No breaking changes at any step. Existing code keeps working with `InMemoryEngine`.

## Open questions

- **Fan-out within a durable step.** `EvolutionaryAgent.evaluate()` calls N fitness agents in parallel. Do we wrap each call as its own `step()`, or the whole parallel phase as one `step()`? Tradeoff: per-call granularity gives finer replay but explodes step-log volume at large populations. Leaning toward one-step-per-phase with internal retries, but worth revisiting after the first real run.
- **Step log retention.** Completed runs accumulate. Do we cap step-log rows per run, or archive to a cold table after N days? Defer to usage patterns.
- **Human-in-loop waits.** The Pareto escalation path flags hypotheses for human review. Do we add a `waitForSignal(runId, signal)` primitive, or stay imperative and let the caller poll? Leaning toward polling for v1, signals later.
- **Cancellation propagation.** If a user cancels a run mid-step, do we interrupt the in-flight handler? Inngest says no (steps run to completion, cancellation takes effect between steps). We should follow the same rule — simpler and matches durable-execution norms.

## Success criteria

- An `EvolutionaryAgent` run can survive an app redeploy and resume from the last completed generation's last completed phase, with zero code changes in the agent class beyond the `engine.step(...)` retrofit.
- `pause_evolution` persists across restart.
- A workflow author writes a custom multi-step flow using `engine.step()` with the same ergonomics as today's imperative code — no decorators, no generators, no build step.
- The in-memory default keeps dev UX unchanged: no SQL warehouse required to run tests.

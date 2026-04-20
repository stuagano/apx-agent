# Durable Workflow Execution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pluggable durable execution layer beneath the existing workflow agents so runs survive app restarts, crashes, and redeploys. Retrofit `EvolutionaryAgent`, `LoopAgent`, and `SequentialAgent` onto the new layer without changing their public API.

**Architecture:** A small `WorkflowEngine` interface with a single `step<T>()` primitive. Ship two backends: `InMemoryEngine` (default, preserves today's semantics) and `DeltaEngine` (uses existing SQL Statements API — same transport `PopulationStore` already uses). Workflow agents call `engine.step(runId, stepKey, handler)` around any expensive or non-deterministic operation.

**Tech Stack:** TypeScript, Zod v4, vitest, Databricks SQL Statements API, `appkit-agent`. Python parity follows in Phase 6.

**Spec:** `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`

---

## File Map

### Core Engine (TypeScript)

| File | Responsibility |
|------|----------------|
| `typescript/src/workflows/engine.ts` | `WorkflowEngine` interface, `RunStatus`, `RunSnapshot`, `RunFilter`, `ReplayDivergenceError` |
| `typescript/src/workflows/engine-memory.ts` | `InMemoryEngine` — default, per-process Map-backed store |
| `typescript/src/workflows/engine-delta.ts` | `DeltaEngine` — SQL Statements API backend, reuses `PopulationStore`'s chunking + caching patterns |
| `typescript/src/workflows/engine-inngest.ts` | `InngestEngine` — optional adapter, only loaded if `inngest` is installed |

### Retrofits (TypeScript)

| File | Change |
|------|--------|
| `typescript/src/workflows/evolutionary.ts` | Accept optional `engine` in config; wrap each generation phase in `engine.step()`; persist `runId` and rebuild `history` on resume |
| `typescript/src/workflows/loop.ts` | Accept optional `engine`; wrap each iteration in `engine.step()` |
| `typescript/src/workflows/sequential.ts` | Accept optional `engine`; wrap each sub-agent invocation in `engine.step()` |
| `typescript/src/workflows/index.ts` | Export new engine types and classes |
| `typescript/src/index.ts` | Add package-level exports |

### Tests (TypeScript)

| File | Covers |
|------|--------|
| `typescript/tests/engine-memory.test.ts` | Interface contract: step dedupe, replay semantics, failure persistence |
| `typescript/tests/engine-delta.test.ts` | SQL backend with mocked SQL Statements API |
| `typescript/tests/evolutionary-durable.test.ts` | `EvolutionaryAgent` resume-across-restart scenario |
| `typescript/tests/loop-durable.test.ts` | `LoopAgent` resume scenario |
| `typescript/tests/sequential-durable.test.ts` | `SequentialAgent` resume scenario |

### Python parity

| File | Responsibility |
|------|----------------|
| `python/src/apx_agent/workflow/engine.py` | `WorkflowEngine` protocol |
| `python/src/apx_agent/workflow/engine_memory.py` | `InMemoryEngine` |
| `python/src/apx_agent/workflow/engine_delta.py` | `DeltaEngine` |
| `python/src/apx_agent/workflow/loop_agent.py` | Retrofit onto engine |
| `python/tests/test_workflow_engine.py` | Contract tests |

---

## Phase 1: Engine Interface + InMemoryEngine

Establish the abstraction and ship a no-op default. Zero behavior change for existing callers.

### Task 1.1: Define types

**Files:**
- Create: `typescript/src/workflows/engine.ts`

- [ ] **Step 1: Write the interface**

```typescript
// typescript/src/workflows/engine.ts

export type RunStatus =
  | 'running'
  | 'paused'
  | 'completed'
  | 'converged'
  | 'failed'
  | 'cancelled';

export interface StepRecord {
  stepKey: string;
  status: 'completed' | 'failed';
  output?: unknown;
  error?: string;
  durationMs: number;
  recordedAt: string;
}

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

export interface RunSummary {
  runId: string;
  workflowName: string;
  status: RunStatus;
  startedAt: string;
  updatedAt: string;
}

export interface RunFilter {
  workflowName?: string;
  status?: RunStatus;
  limit?: number;
}

export class ReplayDivergenceError extends Error {
  constructor(runId: string, expected: string, observed: string) {
    super(`Replay divergence on run ${runId}: expected step ${expected}, saw ${observed}`);
    this.name = 'ReplayDivergenceError';
  }
}

export interface WorkflowEngine {
  startRun(
    workflowName: string,
    input: unknown,
    opts?: { runId?: string },
  ): Promise<string>;

  step<T>(
    runId: string,
    stepKey: string,
    handler: () => Promise<T>,
  ): Promise<T>;

  finishRun(runId: string, status: RunStatus, output?: unknown): Promise<void>;

  getRun(runId: string): Promise<RunSnapshot | null>;

  listRuns(filter?: RunFilter): Promise<RunSummary[]>;
}
```

### Task 1.2: InMemoryEngine

**Files:**
- Create: `typescript/src/workflows/engine-memory.ts`
- Create: `typescript/tests/engine-memory.test.ts`

- [ ] **Step 1: Write the failing tests**

Cover the contract:
- `startRun` returns a new `runId` if none provided; reuses the provided one if given.
- First call to `step(runId, 'a', fn)` invokes `fn` and caches; second call returns cached output without invoking.
- If `fn` throws, the failure is persisted. Subsequent calls with the same `stepKey` re-throw the same error.
- `finishRun` updates status and `output`.
- `getRun` returns the full snapshot including step log.
- `listRuns` filters by `workflowName` and `status`.

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement `InMemoryEngine`**

Simple Map-backed store. Use `structuredClone` on persisted values to avoid aliasing bugs (same pattern as `InMemorySessionStore`).

- [ ] **Step 4: Run tests to verify they pass**

### Task 1.3: Wire into exports

**Files:**
- Edit: `typescript/src/workflows/index.ts`
- Edit: `typescript/src/index.ts`

- [ ] **Step 1: Add exports**

Export `WorkflowEngine`, `InMemoryEngine`, `RunStatus`, `RunSnapshot`, `RunSummary`, `RunFilter`, `ReplayDivergenceError`, `StepRecord`.

- [ ] **Step 2: Run `npm run build` to verify**

---

## Phase 2: Retrofit EvolutionaryAgent

Highest-leverage target. Already has a durable store (`PopulationStore`) for its primary data, so the step log is purely about progression state.

### Task 2.1: Accept engine in config

**Files:**
- Edit: `typescript/src/workflows/evolutionary.ts`

- [ ] **Step 1: Extend `EvolutionaryConfig`**

Add `engine?: WorkflowEngine` and `runId?: string` to the config interface. Default `engine` to a new `InMemoryEngine()` if absent.

- [ ] **Step 2: Call `startRun` in the constructor or on first `run()`**

Store the returned `runId` on the instance. If the caller passed an existing `runId`, `startRun` resumes rather than starts fresh.

### Task 2.2: Wrap generation phases

**Files:**
- Edit: `typescript/src/workflows/evolutionary.ts`

- [ ] **Step 1: Wrap each phase of `runGeneration()`**

Five `engine.step()` calls per generation:
- `load-${gen}` — `store.loadTopSurvivors(...)`
- `mutate-${gen}` — `this.mutate(parents, gen)`
- `evaluate-${gen}` — `this.evaluate(candidates)`
- `judge-${gen}` — `this.judge(evaluated)`
- `write-${gen}` — `store.writeHypotheses(judged)` + downstream selection/escalation/convergence as a single step since they're pure functions over the persisted data

- [ ] **Step 2: Rebuild `history[]` on resume**

On construction, if `runId` is a resume, call `engine.getRun(runId)` and reconstruct `currentGeneration` and `history` from the persisted step records. The `runGeneration` result payload contains everything `history` needs.

- [ ] **Step 3: Persist state transitions**

`pauseLoop()` and `resumeLoop()` call `engine.finishRun(runId, 'paused')` / `startRun(..., { runId })`. Convergence / completion call `finishRun` with the appropriate status.

### Task 2.3: Tests

**Files:**
- Create: `typescript/tests/evolutionary-durable.test.ts`

- [ ] **Step 1: Write resume-across-restart test**

1. Create `agent1` with `InMemoryEngine` (the same instance is shared across the two "processes").
2. Run 3 generations. Capture `runId`.
3. Mock the agent calls to track invocation counts.
4. Create `agent2` with the same `engine` and `runId`.
5. Resume. Verify:
   - `mutate`, `evaluate`, `judge` handlers are *not* re-invoked for the 3 already-completed generations.
   - `history` on `agent2` matches `history` on `agent1`.
   - Generation 4 onward runs normally.

- [ ] **Step 2: Write pause-survives-restart test**

Pause on `agent1`, reconstruct `agent2` with same `runId`, verify state is `paused`.

- [ ] **Step 3: Run tests to verify they pass**

---

## Phase 3: Retrofit LoopAgent + SequentialAgent

Mechanical changes. Keeps the whole workflow surface on the engine.

### Task 3.1: LoopAgent

**Files:**
- Edit: `typescript/src/workflows/loop.ts`
- Create: `typescript/tests/loop-durable.test.ts`

- [ ] **Step 1: Accept `engine` and `runId` options**

- [ ] **Step 2: Wrap each iteration in `engine.step(runId, \`iter-${i}\`, () => agent.run(context))`**

- [ ] **Step 3: Write resume test** — kill mid-loop, resume, verify the completed iterations replay from cache.

### Task 3.2: SequentialAgent

**Files:**
- Edit: `typescript/src/workflows/sequential.ts`
- Create: `typescript/tests/sequential-durable.test.ts`

- [ ] **Step 1: Accept `engine` and `runId` options**

- [ ] **Step 2: Wrap each sub-agent call** — step key is the agent index or its `outputKey` if defined.

- [ ] **Step 3: Write resume test**

---

## Phase 4: DeltaEngine

### Task 4.1: Schema + migrations

**Files:**
- Create: `typescript/src/workflows/engine-delta.ts`

- [ ] **Step 1: Define DDL constants**

```sql
CREATE TABLE IF NOT EXISTS {table_prefix}_runs (
  run_id STRING NOT NULL,
  workflow_name STRING NOT NULL,
  status STRING NOT NULL,
  input STRING,
  output STRING,
  started_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {table_prefix}_steps (
  run_id STRING NOT NULL,
  step_key STRING NOT NULL,
  status STRING NOT NULL,
  output STRING,
  error STRING,
  duration_ms BIGINT,
  recorded_at TIMESTAMP NOT NULL
) USING DELTA;
```

No true PK in Delta; enforce uniqueness at write time via `MERGE`.

- [ ] **Step 2: Implement constructor with schema bootstrap**

Accept `{ warehouseId, tablePrefix, host? }`. On first use, idempotently run the `CREATE TABLE IF NOT EXISTS` statements via the SQL Statements API (same path `PopulationStore` already uses).

### Task 4.2: Implement interface methods

- [ ] **Step 1: `startRun`** — `MERGE` on `run_id`; if the row exists, update `status` to `running` and `updated_at`; else insert.

- [ ] **Step 2: `step()`**

   - Point-select on `(run_id, step_key)`. Cache the result on the instance keyed by `runId` so consecutive steps in the same process don't round-trip.
   - If hit and `status = 'completed'`: return parsed `output`.
   - If hit and `status = 'failed'`: re-throw the stored error.
   - If miss: invoke `handler`, time it, `INSERT` the step record with the result (or failure), return / rethrow.

- [ ] **Step 3: `finishRun`, `getRun`, `listRuns`** — straightforward `UPDATE` / `SELECT`.

### Task 4.3: Tests with mocked SQL API

**Files:**
- Create: `typescript/tests/engine-delta.test.ts`

- [ ] **Step 1: Mock the SQL Statements transport**

Mirror the pattern in `typescript/tests/population.test.ts`.

- [ ] **Step 2: Run the same contract suite as `engine-memory.test.ts`**

Easiest to share: extract the contract into a helper `describeWorkflowEngineContract(makeEngine)` and call it from both test files.

---

## Phase 5: Optional InngestEngine

Ship as a peer-dependency adapter. Skip if no concrete user asks for it — the Delta backend handles the expected use cases.

- [ ] **Step 1: Create `typescript/src/workflows/engine-inngest.ts`**

- [ ] **Step 2: Map `engine.step(runId, key, handler)` onto Inngest's `step.run(key, handler)`**

- [ ] **Step 3: Add `inngest` as an optional peer dep**

- [ ] **Step 4: Smoke test against Inngest dev server**

---

## Phase 6: Python parity

Port interface + both backends. `LoopAgent` in `python/src/apx_agent/workflow/loop_agent.py` is the main consumer.

- [ ] **Step 1: `python/src/apx_agent/workflow/engine.py`** — `WorkflowEngine` protocol mirroring the TS shape.

- [ ] **Step 2: `python/src/apx_agent/workflow/engine_memory.py`** — dict-backed.

- [ ] **Step 3: `python/src/apx_agent/workflow/engine_delta.py`** — reuse SQL Statements client from `_sql.py`.

- [ ] **Step 4: Retrofit `LoopAgent`** — accept optional `engine` and `run_id`, wrap iterations.

- [ ] **Step 5: Contract tests** in `python/tests/test_workflow_engine.py`.

---

## Phase 7: Documentation

- [ ] **Step 1: Update `README.md`**

Add a short "Durable workflows" subsection under "Workflow agents" pointing at the spec.

- [ ] **Step 2: Add usage example** to `typescript/examples/voynich/orchestrator/app.ts`

Show `EvolutionaryAgent` constructed with a `DeltaEngine`.

- [ ] **Step 3: Migration note** — call out that `InMemoryEngine` is the default and no action is required for existing code.

---

## Rollout order

1. **Phase 1** (engine interface + in-memory) — standalone, safe.
2. **Phase 2** (EvolutionaryAgent) — highest value, validates the interface on the hardest case.
3. **Phase 3** (LoopAgent, SequentialAgent) — mechanical.
4. **Phase 4** (DeltaEngine) — unlocks real durability.
5. **Phase 6** (Python parity) — can start in parallel with Phase 4.
6. **Phase 5** (Inngest) — only if demand appears.
7. **Phase 7** (docs) — after Phase 4 lands.

Each phase is shippable independently.

## Out of scope for this plan

- `ParallelAgent`, `RouterAgent`, `HandoffAgent` retrofit. Defer until a real use case surfaces.
- Workflow versioning, cross-region replication, or multi-tenant orchestration.
- A workflow DSL or decorator layer. The `engine.step()` primitive is the whole API.
- Step-log retention policy. Revisit after observing real usage.

## Verification checklist

- [ ] Existing tests pass with no changes (default `InMemoryEngine` preserves current behavior).
- [ ] A mid-run `EvolutionaryAgent` can be reconstructed from `runId` alone and resumes on the next generation without re-invoking the previously completed phase handlers.
- [ ] `pauseLoop()` / `resumeLoop()` round-trips cleanly through the engine.
- [ ] `DeltaEngine` passes the same contract suite as `InMemoryEngine`.
- [ ] `npm run build` and `npm test` are green.
- [ ] `uv run pytest` is green for Python parity.

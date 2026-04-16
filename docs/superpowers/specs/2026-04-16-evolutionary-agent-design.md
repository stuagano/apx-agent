# EvolutionaryAgent — TypeScript Port Design Spec

**Date:** 2026-04-16
**Status:** Draft
**Author:** Stuart Gano
**Depends on:** PR #7 (connectors), PR #6 (Python LoopAgent reference)

## Overview

Port PR #6's LoopAgent evolutionary framework from Python to TypeScript AppKit. The EvolutionaryAgent manages populations of hypotheses across generations, using Pareto-frontier selection and convergence detection. It fits alongside SequentialAgent, ParallelAgent, RouterAgent, etc. in the existing workflow vocabulary.

This means the Guidepoint Workflow Orchestrator can be a TypeScript AppKit app rather than a separate Python DABs job — keeping the entire stack in one language.

### Scope

- Generic EvolutionaryAgent framework (population management, Pareto selection, convergence)
- PopulationStore class (SQL Statements API with batching and caching)
- Hypothesis type with configurable fitness signals
- Conversational tools (status, pause, resume, escalate)
- Voynich reference implementation (5 AppKit apps, structural parity with PR #6)

### Out of Scope

- Voynich data prep (corpus loading, VS index creation — stays in Python notebooks)
- Spark write path (TypeScript only has SQL Statements API)
- Human review gate UI (separate concern, can use existing AppKit dev UI)

## Architecture

```
EvolutionaryAgent (implements Runnable)
│
├── Background loop (async)
│   ├── mutate()  ──POST──> Mutation Agent App (/invocations)
│   ├── evaluate() ──POST──> Fitness Agent Apps (/invocations, parallel)
│   ├── judge()   ──POST──> Judge Agent App (/invocations)
│   ├── select()  ──────── paretoFrontier() + selectSurvivors()
│   └── converge? ──────── getFitnessHistory() delta check
│
├── PopulationStore (SQL Statements API)
│   ├── writeHypotheses()      — chunked INSERT (25/batch)
│   ├── updateFitnessScores()  — MERGE upsert (batched)
│   ├── loadGeneration()       — cached SELECT
│   ├── loadTopSurvivors()     — SELECT ORDER BY composite DESC
│   └── getFitnessHistory()    — last N generations max/avg
│
└── Conversational tools (via collectTools())
    ├── evolution_status
    ├── best_hypothesis
    ├── generation_summary
    ├── pause_evolution / resume_evolution
    └── force_escalate
```

Fitness/mutation/judge agents are regular AppKit apps — they don't need to know they're part of an evolutionary loop. The EvolutionaryAgent calls them via HTTP POST to their `/invocations` endpoints with OBO header forwarding.

## File Map

### Core Framework (workflows/)

| File | Responsibility | Est. Lines |
|------|---------------|------------|
| `workflows/hypothesis.ts` | Hypothesis type, `createHypothesis()`, `compositeFitness()` | ~50 |
| `workflows/pareto.ts` | `paretoDominates()`, `paretoFrontier()`, `selectSurvivors()` | ~60 |
| `workflows/population.ts` | `PopulationStore` class — SQL batching, caching, MERGE | ~200 |
| `workflows/evolutionary.ts` | `EvolutionaryAgent` implements Runnable — generation loop, agent calls | ~150 |

### Tools (connectors/)

| File | Responsibility | Est. Lines |
|------|---------------|------------|
| `connectors/evolution-tools.ts` | `defineTool()` factories for conversational tools | ~100 |

### Voynich Example (examples/voynich/)

| File | Responsibility | Est. Lines |
|------|---------------|------------|
| `examples/voynich/voynich-config.ts` | Shared config: fitness weights, Pareto objectives, VoynichHypothesis type | ~40 |
| `examples/voynich/orchestrator/app.ts` | EvolutionaryAgent + PopulationStore + Voynich config | ~120 |
| `examples/voynich/decipherer/app.ts` | Mutation agent: mutate cipher params, apply cipher | ~80 |
| `examples/voynich/historian/app.ts` | RAG fitness scorer: VS query against medieval corpus | ~70 |
| `examples/voynich/critic/app.ts` | Adversarial falsifier: find contradictions in decoded text | ~60 |
| `examples/voynich/judge/app.ts` | Agent eval: score Historian/Critic reasoning quality | ~70 |

### Tests

| File | Covers | Est. Lines |
|------|--------|------------|
| `tests/hypothesis.test.ts` | createHypothesis, compositeFitness, serialization | ~60 |
| `tests/pareto.test.ts` | paretoDominates, paretoFrontier, selectSurvivors | ~100 |
| `tests/population.test.ts` | PopulationStore: write, read, merge, cache | ~150 |
| `tests/evolutionary.test.ts` | EvolutionaryAgent: generation loop, convergence, pause/resume | ~200 |
| `tests/evolution-tools.test.ts` | Conversational tool factories | ~80 |

## Detailed Design

### Hypothesis (`workflows/hypothesis.ts`)

```typescript
interface Hypothesis {
  id: string;                          // randomUUID() truncated to 8 chars
  generation: number;
  parent_id: string | null;
  fitness: Record<string, number>;     // named fitness signals (domain-specific)
  metadata: Record<string, unknown>;   // domain-specific fields
  flagged_for_review: boolean;
  created_at: string;                  // ISO timestamp
}

function compositeFitness(h: Hypothesis, weights: Record<string, number>): number
// Weighted sum of h.fitness[key] * weights[key]. Normalizes to 0-1.
// Missing fitness keys treated as 0.

function createHypothesis(opts: {
  generation: number;
  parent_id?: string;
  fitness?: Record<string, number>;
  metadata?: Record<string, unknown>;
}): Hypothesis
// Generates truncated UUID, sets defaults (empty fitness, empty metadata,
// flagged_for_review=false, created_at=now).
```

**Design decision:** `fitness` and `metadata` are both `Record` types. No Voynich-specific fields (cipher_type, source_language, symbol_map) in the base type. The Voynich example extends via metadata. Guidepoint's schema.yaml drives what goes into metadata and fitness.

### Pareto Selection (`workflows/pareto.ts`)

```typescript
function paretoDominates(
  a: Hypothesis, b: Hypothesis, objectives: string[]
): boolean
// a.fitness[obj] >= b.fitness[obj] for ALL objectives
// AND a.fitness[obj] > b.fitness[obj] for AT LEAST ONE
// Missing fitness values treated as 0.

function paretoFrontier(
  population: Hypothesis[], objectives: string[]
): Hypothesis[]
// O(n²) non-dominated set extraction.
// Returns all hypotheses not dominated by any other.

function selectSurvivors(
  population: Hypothesis[],
  objectives: string[],
  weights: Record<string, number>,
  maxSize: number
): Hypothesis[]
// 1. Compute Pareto frontier
// 2. If frontier.length >= maxSize, rank by compositeFitness, take top maxSize
// 3. If frontier.length < maxSize, add remaining non-frontier members ranked by
//    compositeFitness until maxSize is reached
```

Pure functions, zero I/O. Direct port of Python logic.

### PopulationStore (`workflows/population.ts`)

```typescript
interface PopulationStoreConfig {
  host?: string;             // defaults to DATABRICKS_HOST
  populationTable: string;   // fully qualified Delta table name (e.g., main.voynich.population)
  warehouseId?: string;      // defaults to DATABRICKS_WAREHOUSE_ID env
  chunkSize?: number;        // INSERT batch size (default 25)
  cacheEnabled?: boolean;    // in-memory read cache (default true)
}

class PopulationStore {
  constructor(config: PopulationStoreConfig)

  // --- Write ---

  async writeHypotheses(hypotheses: Hypothesis[]): Promise<void>
  // Chunks into batches of config.chunkSize.
  // Each batch: INSERT INTO {table} (id, generation, parent_id, fitness, metadata,
  //   flagged_for_review, created_at) VALUES (:p0_id, :p0_gen, ...), (:p1_id, ...)
  // fitness and metadata stored as JSON strings.

  async updateFitnessScores(
    updates: Array<{ id: string; fitness: Record<string, number> }>
  ): Promise<void>
  // For each update:
  //   MERGE INTO {table} AS t
  //   USING (SELECT :id AS id, :fitness_json AS fitness) AS s
  //   ON t.id = s.id
  //   WHEN MATCHED THEN UPDATE SET t.fitness = s.fitness
  // Batched — one MERGE statement per update.

  // --- Read ---

  async loadGeneration(generation: number): Promise<Hypothesis[]>
  // SELECT * FROM {table} WHERE generation = :gen
  // Result cached in Map<number, Hypothesis[]>. Invalidated on write.

  async loadTopSurvivors(generation: number, topN: number): Promise<Hypothesis[]>
  // Requires computing composite fitness. Two strategies:
  // 1. Load generation, compute compositeFitness in-memory, sort, take topN
  // 2. Store composite as a column on write and ORDER BY in SQL
  // Using strategy 1 (compute in-memory) to avoid schema coupling.

  async getFitnessHistory(
    nGenerations: number
  ): Promise<Array<{ generation: number; best: number; avg: number }>>
  // SELECT generation, MAX(composite) as best, AVG(composite) as avg
  //   FROM (SELECT generation, fitness FROM {table} WHERE generation > :min_gen)
  //   GROUP BY generation ORDER BY generation
  // Note: composite is computed from the JSON fitness column in-memory after
  // fetching raw fitness values, since SQL can't parse the JSON fitness weights.
  // Actually: load last N generations, compute composite per hypothesis in TS, aggregate.

  async getActiveConstraints(): Promise<Array<{ id: string; constraint: string }>>
  // SELECT * FROM {table}_review_queue WHERE status = 'approved'
  // Returns constraints injected by human reviewers.

  // --- Cache ---

  clearCache(): void
  // Clears the in-memory generation cache.

  // --- Internal ---

  private async executeSql(statement: string, params?: SqlParam[]): Promise<StatementResponse>
  // POST to /api/2.0/sql/statements/ with warehouseId, wait_timeout, INLINE disposition.
  // Same pattern as Lakebase connector's internal helper but owned by PopulationStore.

  private parseHypothesis(row: Record<string, string>): Hypothesis
  // Parse JSON fitness/metadata strings back into objects.
}
```

**Write batching:** 25 rows per INSERT chunk matches the Python SQL fallback path. Each chunk is a single SQL statement with positional parameters.

**MERGE for fitness updates:** One MERGE per hypothesis (SQL Statements API doesn't support multi-row VALUES in MERGE source). Acceptable because fitness updates happen after evaluation — typically 50-500 hypotheses per generation, not thousands.

**Caching:** `loadGeneration` caches in `Map<number, Hypothesis[]>`. Writes invalidate all cache entries. `getFitnessHistory` is not cached (called once per generation for convergence check).

**No Spark path.** TypeScript only has SQL Statements API.

### EvolutionaryAgent (`workflows/evolutionary.ts`)

```typescript
interface EvolutionaryConfig {
  // Population
  store: PopulationStore;
  populationSize: number;                // max survivors per generation
  mutationBatch: number;                 // new hypotheses per generation (default 50)

  // Agents (URLs of AppKit apps)
  mutationAgent: string;                 // POST /invocations → mutated hypotheses
  fitnessAgents: string[];               // POST /invocations → fitness scores (parallel)
  judgeAgent?: string;                   // POST /invocations → agent eval scores

  // Selection
  paretoObjectives: string[];            // fitness signal names for Pareto
  fitnessWeights: Record<string, number>; // for composite fitness

  // Convergence
  maxGenerations: number;
  convergencePatience: number;           // generations without improvement (default 50)
  convergenceThreshold: number;          // min fitness delta (default 0.001)

  // Escalation
  escalationThreshold: number;           // composite → flag for review (default 0.85)
  topKAdversarial: number;               // fraction for adversarial eval (default 0.05)

  // Agent identity (for conversational responses)
  model?: string;
  instructions?: string;
}

type EvolutionState = 'idle' | 'running' | 'paused' | 'converged' | 'completed';

interface GenerationResult {
  generation: number;
  populationSize: number;
  bestFitness: number;
  avgFitness: number;
  paretoFrontierSize: number;
  escalated: Hypothesis[];
  wallTimeMs: number;
  converged: boolean;
}

class EvolutionaryAgent implements Runnable {
  private state: EvolutionState = 'idle';
  private currentGeneration: number = 0;
  private history: GenerationResult[] = [];
  private loopPromise: Promise<void> | null = null;

  constructor(config: EvolutionaryConfig)

  // --- Runnable interface ---

  async run(messages: Message[]): Promise<string>
  // If state is 'idle', startLoop() and return "Evolution started..."
  // If state is 'running' or 'paused', handle conversationally using
  //   model + instructions + tools (status queries, pause/resume commands).
  // Uses the same runViaSDK pattern as other AppKit agents.

  async *stream(messages: Message[]): AsyncGenerator<string>
  // Same logic as run() but streams the conversational response.

  collectTools(): AgentTool[]
  // Returns evolution-tools factories bound to this agent's state and store.

  // --- Loop control ---

  async startLoop(): Promise<void>
  // Sets state = 'running'. Spawns background loop as unresolved Promise.
  // The loop runs runGeneration() in sequence until convergence, max gen, or pause.

  async pauseLoop(): Promise<void>
  // Sets state = 'paused'. Current generation completes, then loop exits.

  async resumeLoop(): Promise<void>
  // Sets state = 'running'. Re-enters loop from currentGeneration + 1.

  // --- Generation pipeline (private) ---

  private async runGeneration(generation: number): Promise<GenerationResult>
  // 1. Load survivors from previous gen (store.loadTopSurvivors)
  // 2. mutate(survivors, generation) → new candidates
  // 3. evaluate(candidates) → candidates with fitness scores
  // 4. judge(top 20% of evaluated) → candidates with agent eval scores
  // 5. store.writeHypotheses(candidates)
  // 6. Pool: survivors + candidates → selectSurvivors(pool, objectives, weights, populationSize)
  // 7. Escalate: flag hypotheses with compositeFitness >= escalationThreshold
  // 8. store.updateFitnessScores for escalated (set flagged_for_review = true)
  // 9. Check convergence via getFitnessHistory
  // 10. Return GenerationResult

  private async mutate(
    parents: Hypothesis[], generation: number
  ): Promise<Hypothesis[]>
  // POST to config.mutationAgent /invocations:
  //   { input: [{ role: 'user', content: JSON.stringify({ parents, generation }) }] }
  // Parse response as Hypothesis[] (mutation agent returns new candidates).
  // OBO headers forwarded.

  private async evaluate(candidates: Hypothesis[]): Promise<Hypothesis[]>
  // Parallel POST to each config.fitnessAgents[]:
  //   { input: [{ role: 'user', content: JSON.stringify({ hypothesis }) }] }
  // Each agent returns { fitness_signal_name: score }.
  // Merge all scores into candidate.fitness.
  // For adversarial agent (last in list): only send top topKAdversarial fraction.

  private async judge(evaluated: Hypothesis[]): Promise<Hypothesis[]>
  // If no judgeAgent configured, return as-is.
  // POST to config.judgeAgent /invocations with top 20% by composite fitness.
  // Returns agent eval scores added to hypothesis.fitness (prefixed 'agent_eval_').

  private checkConvergence(): boolean
  // Load last convergencePatience generations from history.
  // If max(best) - min(best) < convergenceThreshold → converged.
}
```

**Background loop pattern:** `startLoop()` sets `this.loopPromise = this.runLoop()` where `runLoop` is an async method containing the while-loop. The Promise is never awaited in the request handler — it runs in the background of the Node.js event loop. `pauseLoop()` sets a flag checked between generations; the loop exits cleanly after the current generation completes.

**Agent communication:** `mutate()`, `evaluate()`, and `judge()` all POST to the target agent's `/invocations` endpoint using the same HTTP pattern as `RemoteAgent`. OBO headers from the original request are forwarded so the downstream agents inherit the user's identity.

**Conversational mode:** When a user sends a message while the loop is running, `run()` delegates to `runViaSDK()` with the conversational tools. The model can answer "what's the current generation?", "show me the best hypothesis", etc. This is the same pattern as any other AppKit agent — the evolutionary loop is just background state.

### Evolution Tools (`connectors/evolution-tools.ts`)

Six `defineTool()` factories, each taking closures or references (no circular dependency on EvolutionaryAgent):

```typescript
function createEvolutionStatusTool(
  getState: () => { state: EvolutionState; generation: number; bestFitness: number; totalEscalated: number }
): AgentTool
// name: 'evolution_status'
// parameters: z.object({})
// Returns current state as JSON.

function createBestHypothesisTool(
  store: PopulationStore,
  weights: Record<string, number>
): AgentTool
// name: 'best_hypothesis'
// parameters: z.object({ generation: z.number().optional() })
// Loads generation (default: latest), computes composite, returns best.

function createGenerationSummaryTool(
  getHistory: () => GenerationResult[]
): AgentTool
// name: 'generation_summary'
// parameters: z.object({ generation: z.number().optional() })
// Returns GenerationResult for specified generation (default: latest).

function createPauseEvolutionTool(
  pause: () => Promise<void>
): AgentTool
// name: 'pause_evolution'
// parameters: z.object({})
// Calls pause closure. Returns confirmation.

function createResumeEvolutionTool(
  resume: () => Promise<void>
): AgentTool
// name: 'resume_evolution'
// parameters: z.object({})
// Calls resume closure. Returns confirmation.

function createForceEscalateTool(
  store: PopulationStore
): AgentTool
// name: 'force_escalate'
// parameters: z.object({ hypothesis_id: z.string() })
// Sets flagged_for_review=true via store.updateFitnessScores.
```

### Voynich Reference Implementation

Five AppKit apps in `examples/voynich/`. Each follows the `basic-agent` pattern from the existing examples.

#### Shared Config (`examples/voynich/voynich-config.ts`)

```typescript
export const VOYNICH_FITNESS_WEIGHTS: Record<string, number> = {
  statistical: 0.25,
  perplexity: 0.25,
  semantic: 0.30,
  consistency: 0.15,
  adversarial: 0.05,
};

export const VOYNICH_PARETO_OBJECTIVES = ['statistical', 'perplexity', 'semantic', 'consistency'];

export const VOYNICH_CIPHER_TYPES = [
  'substitution', 'polyalphabetic', 'nomenclator', 'transposition', 'null_cipher',
] as const;

export const VOYNICH_SOURCE_LANGUAGES = [
  'latin', 'german', 'italian', 'nahuatl', 'occitan',
] as const;

// VoynichHypothesis is just a Hypothesis where metadata contains:
//   cipher_type: string
//   source_language: string
//   symbol_map: Record<string, string>
//   null_chars: string[]
//   transformation_rules: string[]
//   decoded_sample?: string
```

#### Orchestrator (`examples/voynich/orchestrator/app.ts`)

~120 lines. Creates `PopulationStore` + `EvolutionaryAgent` with Voynich config. Wires the other 4 agents by URL (from env vars). Exposes conversational tools. This is the entry point researchers interact with.

```typescript
const store = new PopulationStore({
  populationTable: process.env.VOYNICH_POPULATION_TABLE!,
  warehouseId: process.env.DATABRICKS_WAREHOUSE_ID,
});

const evolutionaryAgent = new EvolutionaryAgent({
  store,
  populationSize: 500,
  mutationBatch: 50,
  mutationAgent: process.env.DECIPHERER_AGENT_URL!,
  fitnessAgents: process.env.FITNESS_AGENT_URLS!.split(','),
  judgeAgent: process.env.JUDGE_AGENT_URL,
  paretoObjectives: VOYNICH_PARETO_OBJECTIVES,
  fitnessWeights: VOYNICH_FITNESS_WEIGHTS,
  maxGenerations: 2000,
  convergencePatience: 50,
  convergenceThreshold: 0.001,
  escalationThreshold: 0.85,
  topKAdversarial: 0.05,
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'You are the Voynich Manuscript evolutionary decipherment orchestrator...',
});

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: '...',
  tools: evolutionaryAgent.collectTools(),
  workflow: evolutionaryAgent,
});
```

#### Decipherer (`examples/voynich/decipherer/app.ts`)

~80 lines. Mutation agent. Tools:
- `mutate_hypothesis` — takes parent hypothesis, applies creative mutation to cipher params (swap symbol mappings, add/remove null chars, change transformation rules). Uses FMAPI for creative mutation.
- `apply_cipher` — takes hypothesis + EVA text, applies the cipher to produce decoded text. Pure logic (no LLM call).

#### Historian (`examples/voynich/historian/app.ts`)

~70 lines. RAG fitness scorer. Tools:
- `score_historical_plausibility` — takes decoded text, runs VS query against medieval corpus (Dioscorides, Hildegard, Ptolemy), scores semantic similarity. Uses `createVSQueryTool` from PR #7 connectors.

#### Critic (`examples/voynich/critic/app.ts`)

~60 lines. Adversarial falsifier. Tools:
- `find_contradictions` — takes decoded text + hypothesis metadata, attempts to find linguistic impossibilities, anachronisms, or statistical anomalies that disprove the decipherment.

#### Judge (`examples/voynich/judge/app.ts`)

~70 lines. Agent eval agent. Tools:
- `score_reasoning_quality` — evaluates whether the Historian's plausibility assessment and the Critic's falsification attempts used sound reasoning. Scores via MLflow trace inspection (reads span data from the fitness evaluation step). Returns `agent_eval_historian` and `agent_eval_critic` scores.

## Build Sequence

### Phase 1: Pure Logic (no I/O)
1. `workflows/hypothesis.ts` + tests
2. `workflows/pareto.ts` + tests

### Phase 2: PopulationStore
3. `workflows/population.ts` + tests (mock SQL Statements API)

### Phase 3: EvolutionaryAgent
4. `workflows/evolutionary.ts` + tests (mock agent calls + store)
5. `connectors/evolution-tools.ts` + tests

### Phase 4: Package Wiring
6. Update `workflows/index.ts`, `connectors/index.ts`, `src/index.ts`
7. Build + typecheck + full test suite

### Phase 5: Voynich Examples
8. `examples/voynich/voynich-config.ts`
9. `examples/voynich/orchestrator/app.ts`
10. `examples/voynich/decipherer/app.ts`
11. `examples/voynich/historian/app.ts`
12. `examples/voynich/critic/app.ts`
13. `examples/voynich/judge/app.ts`

## Testing Strategy

**Unit tests (pure logic):**
- Hypothesis creation, composite fitness calculation, serialization
- Pareto dominance, frontier extraction, survivor selection with edge cases (ties, single objective, empty population)

**Unit tests (mocked I/O):**
- PopulationStore: mock SQL Statements API, verify INSERT chunking, MERGE upsert, cache behavior
- EvolutionaryAgent: mock store + mock agent HTTP calls, verify generation pipeline order, convergence detection, pause/resume state machine
- Evolution tools: verify each tool returns correct data from closures

**Voynich examples:**
- Structural verification: each app file imports correctly, creates a valid agentPlugin
- Not end-to-end tested (requires deployed agents + populated Delta tables)

# Voynich Statistical EA — Design Spec

**Date:** 2026-05-04
**Status:** Approved
**Scope:** Extend the voynich-orchestrator with a third EA route that generates, executes, and evolves statistical hypotheses about EVA structure, scored by the existing LLM critic.

---

## Background

`typescript/deploy/voynich-orchestrator/FINDINGS.md` documents four lines of evidence that EVA vocabulary clusters by botanical family. The current orchestrator tests *decoding theories* (cipher + params → decoded text → critic). This spec adds a parallel loop that tests *statistical hypotheses* (feature + method + family → computed result → critic).

The critic scores findings qualitatively (as it already does for decoded text), so no new fitness infrastructure is needed — only a new data schema, two new lightweight agent apps, and a third route in the existing `RouterAgent`.

---

## Architecture

```
RouterAgent (app.ts — extended)
├── Route 1: EA Management        (existing EvolutionaryAgent)
├── Route 2: Theory Investigation (existing TheoryInvestigator)
└── Route 3: Statistical Analysis (new StatEvolutionaryAgent)
         ├── stat-mutation     — LLM generates/mutates HypothesisSpec structs
         ├── stat-executor     — runs SQL + stats against Databricks warehouse
         └── critic (adapted)  — existing critic, new "statistical" mode prompt
```

Two new apps deployed alongside the existing orchestrator and critic:
- `deploy/voynich-stat-mutation/` — stateless LLM app
- `deploy/voynich-stat-executor/` — stateful, needs `DATABRICKS_*` env vars

Population stored in `voynich.stat_findings` Delta table, separate from `voynich.theories`.

---

## Data Model

### HypothesisSpec

What the mutation agent generates — the description of a test to run:

```typescript
interface HypothesisSpec {
  feature: string;        // e.g. "qo-prefix rate", "word entropy", "bigram frequency"
  method: string;         // e.g. "rank-biserial", "jaccard-lift", "permutation-test"
  family_a: string;       // e.g. "solanaceae"
  family_b: string;       // e.g. "all-botanical" | "thistle" | specific family
  folio_filter?: string;  // optional SQL WHERE clause to restrict folios
  null_model?: string;    // e.g. "label-shuffle" | "within-quire-shuffle"
  rationale: string;      // LLM's stated reason for proposing this test
}
```

### StatFinding

What the executor returns after running the test:

```typescript
interface StatFinding {
  spec: HypothesisSpec;
  effect_size: number;       // r, lift, or equivalent
  p_value: number;
  n_samples: number;
  result_table?: string;     // markdown summary of output
  interpretation: string;    // one-paragraph plain-English summary
}
```

### Delta Table: `voynich.stat_findings`

Mirrors the `voynich.theories` schema with a `batch_label` column for idempotent runs:

```
id | generation | spec (JSON) | finding (JSON) | critic_score | critic_feedback | batch_label | created_at
```

`critic_score` is a 0.0–1.0 float. Same structure as existing critic output — `EvolutionaryAgent`'s fitness logic consumes it without changes.

---

## Agent Roles

### stat-mutation (`deploy/voynich-stat-mutation/`)

Stateless LLM app. Receives top-N findings from the population table plus critic feedback; returns a new `HypothesisSpec`.

Mutation strategies (LLM selects based on feedback):
- **Feature expansion**: if `qo-prefix` is productive, try `oq-prefix`, `qok-` trigram, `q-` initial rate
- **Family cross-product**: if solanaceae vs. all-botanical works, try thistle vs. plantago, or one-vs-one across all families
- **Method substitution**: swap rank-biserial for Mann-Whitney U or KS test on the same feature
- **Null model strengthening**: escalate from label-shuffle → within-quire-shuffle → full folio permutation

The mutation prompt seeds with a summary of FINDINGS.md so the LLM avoids re-proposing already-established results.

### stat-executor (`deploy/voynich-stat-executor/`)

Runs one `HypothesisSpec` against `serverless_stable_qh44kx_catalog.voynich`. Steps:
1. Build SQL to pull per-folio feature values for `family_a` and `family_b` folios
2. Compute the specified statistic (rank-biserial r, Jaccard lift, or permutation p-value) in TypeScript
3. Return a `StatFinding` with effect size, p-value, n, and a markdown result table

For permutation tests: 1,000 label shuffles inline (same approach as `morpho-axis-permutation.ts`).

### critic (adapted)

The existing critic app gets a second system prompt path selected by a `mode: "statistical"` parameter. The statistical prompt scores a `StatFinding` on three dimensions:

- **Validity** (0–1): is the test appropriate for the data? is the p-value computed on the right null?
- **Novelty** (0–1): does this extend beyond the four lines of evidence in FINDINGS.md?
- **Interpretability** (0–1): can the result be stated as a clear, falsifiable claim?

`critic_score` = weighted average of the three dimensions. Weights are a config the EA can evolve over time.

---

## Routing and Integration

### `app.ts` changes

Add `StatEvolutionaryAgent` as Route 3; extend `RouterAgent` with deterministic keyword conditions:

```typescript
const statMutationUrl = process.env.STAT_MUTATION_AGENT_URL;
const statExecutorUrl = process.env.STAT_EXECUTOR_AGENT_URL;

const statAgent = new StatEvolutionaryAgent({
  mutationAgentUrl: statMutationUrl,
  executorAgentUrl: statExecutorUrl,   // decoder-equivalent: HypothesisSpec → StatFinding
  fitnessAgentUrls: [criticAgentUrl],  // scorer: StatFinding → critic_score
  populationTable: process.env.STAT_POPULATION_TABLE
    ?? 'serverless_stable_qh44kx_catalog.voynich.stat_findings',
  store,
});

// Router conditions: "analyze" | "test hypothesis" | "statistical" | "run stat" | "findings" | "hypothesis"
```

### New file: `stat-evolutionary-agent.ts`

Thin wrapper around `EvolutionaryAgent` that:
1. Passes `HypothesisSpec` JSON as the "theory" payload to the mutation agent
2. Calls `stat-executor` to convert `HypothesisSpec` → `StatFinding` (decoder-equivalent step)
3. Passes `StatFinding` JSON to the critic fitness agent for scoring
4. Reads/writes `voynich.stat_findings` instead of `voynich.theories`

Executor and critic communicate via the same A2A HTTP pattern as existing fitness agents — no new protocol.

### New env vars (`app.yaml`)

```yaml
STAT_MUTATION_AGENT_URL: <url>
STAT_EXECUTOR_AGENT_URL: <url>
STAT_POPULATION_TABLE: serverless_stable_qh44kx_catalog.voynich.stat_findings
```

---

## Testing and Validation

### Calibration script (`stat-calibrate.ts`)

Runs before any EA loop. Sends three hand-crafted `HypothesisSpec` fixtures through the full pipeline:

1. **Known-good**: replicate the qo-prefix result from FINDINGS.md — should score ≥ 0.6
2. **Known-weak**: random EVA feature with no expected family signal — should score ≤ 0.4
3. **Malformed spec**: missing `family_b` — executor must return a structured error, not crash

### Mutation smoke test

Send the top-3 pre-seeded findings (the four FINDINGS.md results) to the mutation agent. Verify:
- Returned `HypothesisSpec` is valid JSON
- Proposed feature is not already in FINDINGS.md
- `rationale` is non-empty

### Regression guard

After each EA generation, assert `critic_score` of the population's best finding is non-decreasing. A drop logs a warning (not a hard stop) — signals mutation is de-evolving.

### Manual review gate

After 10 generations, `StatEvolutionaryAgent` exposes a `list_findings` tool surfacing the top-5 findings with scores and critic feedback. Human reviews before committing results to `FINDINGS.md`.

---

## Files to Create

| File | Purpose |
|------|---------|
| `typescript/deploy/voynich-stat-mutation/app.ts` | Mutation agent app |
| `typescript/deploy/voynich-stat-mutation/package.json` | Dependencies |
| `typescript/deploy/voynich-stat-mutation/app.yaml` | Databricks app config |
| `typescript/deploy/voynich-stat-executor/app.ts` | Executor agent app |
| `typescript/deploy/voynich-stat-executor/package.json` | Dependencies |
| `typescript/deploy/voynich-stat-executor/app.yaml` | Databricks app config |
| `typescript/deploy/voynich-orchestrator/stat-evolutionary-agent.ts` | StatEvolutionaryAgent wrapper |
| `typescript/deploy/voynich-orchestrator/stat-calibrate.ts` | Calibration script |
| `typescript/deploy/voynich-orchestrator/stat-types.ts` | HypothesisSpec + StatFinding interfaces |

### Files to Modify

| File | Change |
|------|--------|
| `typescript/deploy/voynich-orchestrator/app.ts` | Add Route 3, new env vars |
| `typescript/deploy/voynich-critic/` | Add `mode: "statistical"` prompt path |

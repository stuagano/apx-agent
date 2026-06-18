# `apx-agent label` — Judge-Alignment Loop Design

**Date:** 2026-06-17
**Status:** Approved (brainstorming → ready for implementation plan)
**Author:** Stuart Gano (with Claude)

## Summary

Add labeling-service support to `apx-agent`: a CLI command group that drives the
MLflow 3 GenAI judge-alignment loop against a *deployed apx agent*. A subject-matter
expert (SME) rates a sample of the agent's traces in the MLflow Review App, and those
ratings are distilled (via MemAlign) into the agent's LLM judge so the judge scores
the way the domain expert does.

The loop spans a human-in-the-loop pause (SMEs label out-of-band, often over days),
so it is surfaced as **two CLI commands** separated by that pause:

- `apx-agent label start` — provision the labeling session, print the SME share URL and a run-id.
- `apx-agent label align` — after SMEs finish, pull the labeled traces and align the judge.

The user **brings their own registered judge** (`make_judge().register(...)`). apx's
value-add is owning the parts that are error-prone or mechanical: deriving the label
schema from the judge so the schema name can never drift from the judge name (the
single most-documented failure mode), selecting/scoring the right traces, building the
dataset, attaching the Review App, running alignment, and registering the result.

## Background / grounding

The mechanics mirror Patterns 2–5 of
`python/examples/databricks-skills/databricks-mlflow-evaluation/references/patterns-judge-alignment.md`.
The critical correctness constraint from that doc and `GOTCHAS.md`:

> The label schema `name` MUST match the judge `name` used in `evaluate()`. This is how
> `align()` pairs SME feedback with the corresponding LLM judge scores on the same traces.
> If the names differ, alignment fails or produces incorrect results.

A second correctness fact: `align()` pairs SME labels with the **judge's own LLM scores
on the same traces** — so every trace in the labeling session must *already* carry the
judge's score before SMEs label it.

### Verified facts (mlflow 3.12.0, pinned in this repo)

- `from mlflow.genai import create_labeling_session, get_review_app, label_schemas` — **imports OK**.
- `from mlflow.genai.scorers import get_scorer` / `from mlflow.genai.judges import make_judge` — **OK**.
- `MemAlignOptimizer` import path exists but **raises `MlflowException('DSPy library is required but not installed')`** — MemAlign needs `dspy`. The labeling (`start`) side does **not** need dspy; only `align` does.
- A registered judge **exposes** `name`, `instructions`, and `feedback_value_type` (e.g. `<class 'float'>`) as readable attributes. It does **not** expose numeric bounds (min/max) or categorical options — those live only in the instruction-rubric text. Therefore apx can derive the schema **name**, **instruction**, and **input family** from the judge, and must take the **scale/options** as a CLI input.
- Judge `model` must be a `provider:/name` URI (e.g. `databricks:/databricks-claude-sonnet-4-6`).

### Existing code to reuse (from recon)

- **Agent selector** — `_fleet_resolve(ws, catalog, schema, name_glob, where_exprs, uc_names)` in `_fleet.py` resolves apx agents by UC name / catalog / name-glob / tag predicates, returning `ResolvedAgent(uc_name, name, model, app_name, tags, labels)`.
- **Evaluate path** — `_eval.evaluate(...)`, `_eval.eval_against_endpoint(...)`, and `_eval.app_predict_fn(url, token)` already wrap `mlflow.genai.evaluate` against a deployed endpoint.
- **Trace search** — `_mlflow_tracing.search_traces_for_experiment(exp_name_or_id, **kwargs)` resolves an experiment to its id and calls `mlflow.search_traces(locations=[id], **kwargs)`.
- **Auth** — `_connect_workspace(profile) -> (WorkspaceClient, Config)` / `_require_sdk(profile)`. MLflow auth is environment-based (`MLFLOW_TRACKING_URI=databricks`), separate from `--profile`.
- **Deploy-time tags** — `set_uc_tags_for_agent(...)` / `_build_uc_tag_payload(...)` in `_watchdog.py` write `apx.agent.*` UC model tags. **The MLflow experiment id is NOT recorded today** — it is reconstructed at deploy time by naming convention `/Users/<email>/<bundle>-<target>`.
- **CLI** — Click, `@main.group(cls=_ApxGroup)`, profile via `@click.pass_context` → `ctx.obj["profile"]`. Existing sibling groups: `agents`, `traces`, `eval`, `uc`, `fleet`, `canary`, `watchdog`, `memory`, `examples`.

## Goals

1. Let a user run the full judge-alignment loop against a deployed apx agent with two commands.
2. Make label-schema/judge **name and type coherence automatic** so the #1 documented failure mode is impossible by construction.
3. Reuse existing apx conventions (selector, eval predict_fn, trace search, auth) rather than reinventing them.
4. Make the `start → align` handoff deterministic and collision-free across multiple labeling rounds.

## Non-goals

- Authoring/registering the judge (bring-your-own; user calls `make_judge().register()`).
- Re-evaluating the agent with the aligned judge (that is the existing `apx-agent eval` group — Pattern 6).
- Prompt optimization (`optimize_prompts` / GEPA) — a separate concern.
- A CI capability gate for the live loop in v1 (reality tests are opt-in/manual; see Testing).

## Architecture

New module `python/src/apx_agent/_labeling.py` holds pure, independently-testable
functions for each step. CLI wiring lives in `cli.py` as a `label` group (Approach A,
chosen over folding into `eval`) that reads like the existing `fleet`/`memory` groups.

```
apx-agent label start   ──>  _labeling.start_session(...)
apx-agent label align   ──>  _labeling.align_judge(...)
```

`_labeling.py` units (each with one clear purpose, testable in isolation):

| Unit | Purpose | Depends on |
|------|---------|------------|
| `derive_label_schema(judge, scale, options)` | Build the `create_label_schema` kwargs from a loaded judge: `name`=judge.name, `instruction`=judge.instructions, input family from `feedback_value_type`. Pure; no network. | `label_schemas` types |
| `make_run_id(judge_name, now)` | Deterministic `<judge>-<UTC-timestamp>` run id; `now` injected for testability. | — |
| `resolve_experiment(ws, agent, explicit)` | Resolution order: `--experiment` → `apx.mlflow.experiment_id` tag → naming convention. Returns id or raises a clear error. | selector result, ws |
| `select_traces(exp, filter, since, limit, judge_name)` | Pull pre-scored traces; assert the judge feedback is present; fail fast if none. | `search_traces_for_experiment` |
| `score_traces(endpoint_url, token, judge, inputs)` | `--evaluate` path: run `mlflow.genai.evaluate` to produce fresh scored traces. | `_eval.app_predict_fn`, `mlflow.genai.evaluate` |
| `start_session(...)` | Orchestrate: derive schema → create schema → select/score → tag → dataset → review app → session. Returns a result object (URL, run-id, counts). | the above + mlflow.genai |
| `align_judge(...)` | Orchestrate: pull labeled traces → MemAlign → register aligned judge. Returns guidelines + registered name. | mlflow.genai (+ dspy) |

### Input-family mapping (`derive_label_schema`)

| `feedback_value_type` | Label schema input | Extra CLI input |
|-----------------------|--------------------|-----------------|
| `float` / `int` | `InputNumeric(min_value, max_value)` | `--scale MIN-MAX` (required; e.g. `1-5`) |
| `bool` | `InputBoolean()` (or boolean categorical per mlflow API) | none |
| `str` | `InputCategorical(options=[...])` | `--options a,b,c` (required) |

Schema is created with `enable_comment=True` (free-text rationale feeds MemAlign) and
`overwrite=True`. The name is always taken verbatim from the judge — never accepted as
a flag — so it cannot drift.

## Data flow

### `label start`

1. Resolve agent via `_fleet_resolve` — `--uc-name` OR (`--name`/`--catalog`/`--schema`). Must resolve to exactly one agent.
2. Resolve experiment id (`resolve_experiment`).
3. Load judge: `get_scorer(name=--judge, experiment_id=exp)`.
4. `derive_label_schema(judge, --scale, --options)` → `label_schemas.create_label_schema(...)`.
5. Select traces:
   - **default (pre-scored):** `select_traces(...)` with `--filter`/`--since`/`--limit`; fail fast if none carry the judge's feedback (message suggests `--evaluate`).
   - **`--evaluate <inputs.jsonl>` (opt-in):** `score_traces(...)` against the deployed endpoint first, then select the freshly-scored traces.
6. Tag selected traces: `apx.label.run = <run-id>` (run-id from `make_run_id`).
7. Dataset: `create_dataset`/`get_dataset(name=--dataset-name [default `<agent>_label_<run-id>`])` + `merge_records(selected)`.
8. Review App (default on if endpoint resolvable; `--no-review-agent` to skip): `get_review_app(exp).add_agent(agent_name, model_serving_endpoint=<endpoint>, overwrite=True)`. Endpoint from `--endpoint` or `apx.agent.resources`.
9. Session: `create_labeling_session(name=<run-id>_sme, assigned_users=--assignees [default: current user], label_schemas=[judge.name])` then `.add_dataset(dataset_name)`.
10. Output: SME URL, run-id, assignees, trace count. `--format text|json`.

### `label align` *(requires `apx-agent[align]`)*

1. Resolve experiment + judge.
2. `--run <run-id>` (or `--judge`, resolving its most-recent run) → `search_traces(locations=[exp], filter_string="tag.apx.label.run = '<run-id>'", return_type="list")`.
3. Build `MemAlignOptimizer(reflection_lm=--reflection-model, retrieval_k=--retrieval-k [default 5], embedding_model=--embedding-model [default `databricks:/databricks-gte-large-en`])`.
4. `get_scorer(judge, exp).align(traces=..., optimizer=...)`.
5. Register: default **update in-place** (`aligned.update(experiment_id=exp, ...)`); `--new-version <name>` registers a new named judge, preserving the original.
6. Output: distilled guidelines (`aligned._semantic_memory`) + registered judge name. `--format text|json`.

### State handoff

The run-id is the single key tying `start` to `align`: it scopes the trace tag
(`apx.label.run`), the dataset name, and the labeling-session name. Re-running `start`
mints a new run-id, so a second labeling round never cross-contaminates what `align`
pulls. The label-schema name (= judge name) is the orthogonal key that lets `align()`
pair SME labels with judge scores.

## Targeted improvement (additive, in scope)

Record `apx.mlflow.experiment_id` as a UC model tag at deploy time (extend
`_build_uc_tag_payload` / `set_uc_tags_for_agent`), so the selector can resolve traces
without `--experiment`. Backward-compatible: `--experiment` always overrides; agents
deployed before this change fall back to naming-convention resolution. The fragile
convention path is best-effort only and never load-bearing.

## Dependencies

- `start`: runs on the already-pinned `mlflow==3.12.0`. No new deps.
- `align`: needs `dspy` (MemAlign backend). Add an optional extra `apx-agent[align]`.
  A missing `dspy` must surface as a clear `pip install 'apx-agent[align]'` message, not a raw `ImportError`/`MlflowException`.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Selector resolves ≠ 1 agent | Error listing the matches; ask user to narrow. |
| Experiment unresolved | Error instructing the user to pass `--experiment <id\|path>`. |
| Judge not registered / not found | Error pointing to `make_judge(...).register(experiment_id=...)`. |
| Numeric judge, no `--scale` | Error: numeric judges require `--scale MIN-MAX`. |
| Categorical judge, no `--options` | Error: categorical judges require `--options a,b,c`. |
| Default path, no scored traces | Fail fast; suggest `--evaluate <inputs>` to score a cold-start sample. |
| `dspy` missing (`align`) | `pip install 'apx-agent[align]'` hint. |
| MLflow auth / tracking URI missing | Surface clearly; ensure `MLFLOW_TRACKING_URI` is set. |

## Testing

- **Unit (no network):**
  - `derive_label_schema` — name == judge name; instruction == judge instructions; input family per type (float/int→numeric, bool→boolean, str→categorical); `--scale` parsing; `--options` parsing; missing-scale / missing-options errors.
  - `make_run_id` — deterministic given an injected timestamp; format `<judge>-<UTC-ts>`.
  - `resolve_experiment` — resolution order (flag > tag > convention) with a mocked selector result.
- **CLI:** argument wiring for `label start` / `label align`; `--format json` output shape; error-message assertions (ctk claim-vs-reality where it fits).
- **Orchestration:** mock `mlflow.genai` calls to assert the call sequence and arguments — in particular that the label schema is created with `name == judge.name` and that `align` pulls traces by the run-scoped tag.
- **Reality (opt-in, needs a workspace):** `start` returns a real session URL; `align` registers a judge. Deferred to manual like the existing waived capabilities — not a CI gate in v1. A `caps` capability may be added later.

## Open questions

None blocking. Possible future follow-ups (out of scope here): a `label status` command
to poll SME completion %, multi-agent fan-out (label a whole fleet selector), and
promoting the live loop to a `caps` capability.

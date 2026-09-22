# PRD: "Improve instructions" button (optimize_prompts) in the dev-UI Edit tab

**Slug:** optimize-prompts-button
**Status:** ready for implementation
**Scope:** MVP, dev-UI only

## Problem

An apx SA/developer iterating on an agent in the local dev UI hand-edits the
root agent's instructions in the Edit tab and Saves — `POST /_apx/edit`
(`python/src/apx_agent/_dev.py:2341`) writes the full source; the deeper
write-back is `_persist_instructions` (`_dev.py:1353` →
`_ui_edit._set_agent_instructions`). There is no way to have MLflow's
`mlflow.genai.optimize_prompts()` (GEPA) propose better instructions scored
against the agent's existing eval dataset + judge. Every piece needed already
exists (judge_name flow, eval-dataset handling, the write-back); this assembles
them plus a **transient** prompt-registry bridge.

## Solution (the 4 locked decisions)

1. **Reuse existing inputs, fail clear if missing.** Source the eval dataset the
   same way `GET /_apx/eval/data` (`_dev.py:4115`) does — MLflow cache with a
   fallback to `evals.json` via `_find_evals_path()` (`_ui_edit.py:116`, imported
   into `_dev.py:108`). Reuse the existing `judge_name` request-body convention
   (label-start `_dev.py:4306`, label-align `_dev.py:4338`) and load the judge
   with `mlflow.genai.scorers.get_scorer(name=judge_name, experiment_id=...)`
   (as `_labeling.py:42,257,387`). If no dataset or no judge is available at
   runtime, return a clear error and **do not** call `optimize_prompts`.
2. **Hide the prompt registry.** Transiently `mlflow.genai.register_prompt(...)`
   the current instructions, pass its URI as the single `prompt_uris` entry to
   `optimize_prompts`, extract the winning text from the result, then delete the
   transient prompt — in a `finally` so cleanup runs even if optimize raises.
   The registry is never surfaced to the user and never becomes a source of truth.
3. **Land via preview + explicit apply, never auto-write.** The optimize route
   returns only the candidate text + before/after judge scores. The Edit-tab JS
   loads the candidate into the existing instructions editor (client-side diff
   view) for review; the human applies it through the **existing** Save path
   (`POST /_apx/edit` → `_persist_instructions`). The optimize route itself
   writes nothing to `agent.py`.
4. **MVP scope only** (see Non-goals).

### New backend route

`POST /_apx/edit/optimize-instructions` on the same guarded dev router
(`build_dev_ui_router()`), modeled on `eval_label_start` (`_dev.py:4306`):
plain dict on success, `JSONResponse({ok:false,error:...}, status_code=...)` on
failure. Body: `{judge_name: str}`. Success:
`{ok: true, candidate: str, scores: {before: float, after: float}}`.

Flow (all synchronous MLflow work off the event loop via `asyncio.to_thread`):
- validate `judge_name` (422 if empty, matching label-start `_dev.py:4315-4317`);
- load eval rows (evals.json fallback); empty → clear error, no optimize call;
- `get_scorer(name=judge_name, ...)`; failure → clear error, no optimize call;
- build `predict_fn` from the compiled root agent (reuse the `_eval.py` predict
  pattern, `_eval.py:42`);
- `register_prompt` current instructions → `prompt_uris=[uri]`;
- `optimize_prompts(predict_fn=..., train_data=rows, prompt_uris=[uri],
  optimizer=GepaPromptOptimizer(reflection_model=...), scorers=[judge])`;
- extract winning prompt text + scores from `PromptOptimizationResult`;
- `finally:` delete the transient prompt.

### Frontend

In `_render_edit_ui` (the `GET /_apx/edit` template, `_dev.py:2308`): add an
**Improve instructions** button with idle / running / result / error states.
On success it populates the instructions editor with the candidate for review
(no auto-save); the user Saves via the existing control.

## Non-goals

- No dataset-picker UI (reuse the existing eval dataset).
- The MLflow prompt registry is **not** the canonical instruction store — it is
  transient and cleaned up.
- No auto-apply: the candidate is never written to `agent.py` by this feature;
  landing goes through the existing preview + explicit Save.
- Optimizing anything but the **root** agent's instructions.
- No production (non-dev) surface — dev router only.
- GEPA output quality is not guaranteed or gated (stochastic; see Known ceiling).
- No new dependencies (Ponytail — MLflow 3.14.0 already provides everything).

## Acceptance Criteria

- [ ] **AC-1** — `POST /_apx/edit/optimize-instructions` is registered on the dev
  router and, with `optimize_prompts` mocked, returns
  `{ok: true, candidate, scores: {before, after}}`.
- [ ] **AC-2** — Missing/empty `judge_name` returns `422` with a clear error and
  never calls `optimize_prompts`.
- [ ] **AC-3** — No eval dataset available at runtime (empty evals.json, no
  MLflow) returns a clear error and never calls `optimize_prompts`.
- [ ] **AC-4** — Unknown/unavailable judge (`get_scorer` raises) returns a clear
  error and never calls `optimize_prompts`.
- [ ] **AC-5** — The transient prompt is registered before optimize and deleted
  afterward, including when `optimize_prompts` raises (cleanup in `finally`).
- [ ] **AC-6** — The optimize route never writes `agent.py`: after a mocked
  successful call, the on-disk agent source is byte-identical (ctk read-after-write).
- [ ] **AC-7** — The Edit-tab HTML from `_render_edit_ui` contains the
  "Improve instructions" button and a fetch to
  `/_apx/edit/optimize-instructions`; the candidate lands in the editor for
  review (not auto-saved).

## Convergence

- **stopping_signal:** `cd python && uv run pytest tests/ -k optimize_prompts -q`
- **progress_metric:** failing gate-test count (target 0)
- **editability:** high — one new route in `_dev.py`, a button in `_render_edit_ui`,
  landing via existing `_persist_instructions`.
- **verifiability:** medium, re-represented. GEPA output is stochastic; "the
  instructions are better" is not machine-checkable. Re-represented goal: the
  flow runs end-to-end with `optimize_prompts` **mocked**, transiently registers
  + cleans up a prompt, returns candidate + before/after scores, and fails clear
  when dataset/judge is missing. Judge-score improvement is *reported, not gated*.
- **known_ceiling:** real GEPA quality can't be unit-tested (LLM/network/
  stochastic) — only the plumbing and failure paths are gated; quality is a
  manual/observational check.

## Constraints

- **tech_stack:** Python / FastAPI backend + vanilla JS dev-UI.
- **key_files:**
  - `python/src/apx_agent/_dev.py` — dev router; new route + edit-page button
    (routes: GET `/_apx/edit` @2308, POST `/_apx/edit` @2341, preview @2375,
    `_persist_instructions` @1353, judge flow @4306/@4338, eval data @4115).
  - `python/src/apx_agent/_ui_edit.py` — `_find_agent_router_path` @24,
    `_find_evals_path` @116, `_set_agent_instructions` (safe source write-back).
  - `python/src/apx_agent/_eval.py` — predict_fn pattern (@42) + evalset shaping.
  - `python/src/apx_agent/_labeling.py` — `get_scorer` usage (@42/@257/@387).
  - `python/tests/test_optimize_prompts_route.py` — new gate tests.
  - `python/tests/test_optimize_prompts_reality_ctk.py` — new reality test.
- **patterns:** reuse `_persist_instructions` / `_ui_edit` and the existing
  judge/eval helpers; keep the route on the guarded dev router; **no new deps**
  (Ponytail); ctk read-after-write on the no-write claim (Ctk).
- **lint:** no `.get(k, "")`, no `x or ""`, no invented env defaults, no `object`
  annotations, no `tuple[...]` returns, no skipped tests.
- MLflow pinned `3.14.0` (`mlflow[databricks]>=3.14,<3.15`).

## Agent Handoff

```json
{
  "goal": "Add an 'Improve instructions' button to the dev-UI Edit tab that runs mlflow.genai.optimize_prompts (GEPA) against the agent's existing eval dataset + judge, transiently bridges instructions through the MLflow prompt registry, and lands the winning candidate via the existing preview/apply flow — never auto-writing agent.py.",
  "tech_stack": "Python/FastAPI backend + vanilla JS dev-UI",
  "constraints": {
    "key_files": [
      "python/src/apx_agent/_dev.py",
      "python/src/apx_agent/_ui_edit.py",
      "python/src/apx_agent/_eval.py",
      "python/src/apx_agent/_labeling.py",
      "python/tests/test_optimize_prompts_route.py",
      "python/tests/test_optimize_prompts_reality_ctk.py"
    ],
    "no_new_deps": true,
    "dev_only": true,
    "mlflow_version": "3.14.0"
  },
  "patterns": [
    "reuse _persist_instructions / _ui_edit for write-back (landing only, via existing Save)",
    "keep the new route on the guarded dev router (build_dev_ui_router)",
    "reuse judge_name request-body convention + get_scorer, and evals.json fallback via _find_evals_path",
    "transient register_prompt -> optimize_prompts -> extract -> delete in finally (hide the registry)",
    "no new dependencies (Ponytail)",
    "ctk read-after-write to prove the optimize route writes no source (Ctk)"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest tests/ -k optimize_prompts -q",
    "progress_metric": "failing gate-test count",
    "known_ceiling": "GEPA quality is not unit-testable (LLM/network/stochastic); only plumbing and failure paths are gated"
  },
  "escalate_on": [
    "mlflow.genai.optimize_prompts signature differs from assumed (*, predict_fn, train_data, prompt_uris: list[str], optimizer: BasePromptOptimizer, scorers=None, aggregation=None, enable_tracking=True)",
    "no eval dataset or no judge available at runtime and no clear-error path fits the existing route conventions",
    "the transient prompt cannot be deleted / registry cleanup is not possible",
    "the write-back would corrupt agent.py (source splice fails)"
  ],
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "description": "POST /_apx/edit/optimize-instructions is registered and returns {ok:true, candidate, scores:{before,after}} with optimize_prompts mocked.",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_optimize_route_returns_candidate_and_scores",
      "mock": "mlflow.genai.optimize_prompts, register_prompt, get_scorer all mocked"
    },
    {
      "id": "AC-2",
      "description": "Empty/missing judge_name returns 422 and optimize_prompts is never called.",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_missing_judge_name_returns_422",
      "mock": "optimize_prompts mocked; assert not called"
    },
    {
      "id": "AC-3",
      "description": "No eval dataset (empty evals.json, no MLFLOW_EXPERIMENT_ID) returns a clear error and optimize_prompts is never called.",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_missing_eval_dataset_fails_clear",
      "mock": "monkeypatch _find_evals_path to empty; optimize_prompts mocked; assert not called"
    },
    {
      "id": "AC-4",
      "description": "Unknown judge (get_scorer raises) returns a clear error and optimize_prompts is never called.",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_missing_judge_fails_clear",
      "mock": "get_scorer raises; optimize_prompts mocked; assert not called"
    },
    {
      "id": "AC-5",
      "description": "The transient prompt is registered then deleted, including when optimize_prompts raises (finally cleanup).",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_transient_prompt_registered_and_cleaned_up",
      "mock": "register_prompt + delete_prompt spies; optimize_prompts side_effect=raise; assert delete called"
    },
    {
      "id": "AC-6",
      "description": "The optimize route writes no source: the on-disk agent_router.py is byte-identical after a mocked successful call (ctk read-after-write).",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_reality_ctk.py",
      "gate_test": "test_optimize_route_does_not_write_source",
      "mock": "optimize_prompts mocked; assert source bytes unchanged before/after"
    },
    {
      "id": "AC-7",
      "description": "The Edit-tab HTML from _render_edit_ui contains the 'Improve instructions' button and a fetch to /_apx/edit/optimize-instructions.",
      "test_type": "pytest",
      "gate_file": "python/tests/test_optimize_prompts_route.py",
      "gate_test": "test_edit_ui_has_improve_button",
      "mock": "none — assert rendered HTML string contains button id + route path"
    }
  ]
}
```

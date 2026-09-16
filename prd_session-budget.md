# PRD: session_budget — real per-session (cross-turn) token cap on served paths (#768)

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-15

## Summary

Re-introduce `LlmAgent(session_budget={"tokens": N})` and enforce it as a
**cumulative per-session** cap that survives across served turns, raising
`SessionBudgetExceeded(spent, cap)` when the running total crosses `N`. The
cumulative counter is persisted in the compiled graph's keyed `state` channel
(the checkpointer's per-`thread_id` state), so a long session of individually
small turns eventually trips the cap. Enforced on every served entrypoint
(chat `predict`/`predict_stream`, responses `invoke`/`stream`) at the turn
boundary — the first attempt (#766) only summed a single turn on the served
path and stored the accumulator in the unused executor, so the cap was inert.

## Background

`session_budget` and `SessionBudgetExceeded` were removed in the #766 descope:
enforcement lived only in `LangGraphExecutor` (the `run_once` path), while
production serves via `compile_to_langgraph` + `graph.stream`/`graph.invoke`
in `_chat_agent.py` / `_responses_agent.py`, which never touched it; the served
helper summed only the current turn, so the "session" cap never accumulated.

`state_schema()` (`python/src/apx_agent/_compile.py:882`) is
`ApxState(MessagesState)` with a keyed `state: Annotated[dict, _merge_state]`
channel. The checkpointer persists this per `thread_id` across turns, and a turn
that returns `{"state": {...}}` merges into it. This is the durable home for a
cumulative token counter — no new schema field required.

Per-turn token usage is available from the `usage_metadata`
(`input_tokens`/`output_tokens`) on the `AIMessage`s produced during the turn.

## Goals
- G1: `session_budget={"tokens": N}` enforced as a **cumulative** total across
  all turns of a session (same `thread_id`), on every served path.
- G2: The counter persists across turns via the checkpointer `state` channel.
- G3: Verified through the served path (`_chat_agent`/`_responses_agent` with a
  checkpointer + a stable thread_id across turns), not the executor/`run_once`.

## Non-Goals
- No per-turn cap (cumulative-per-session only; decided).
- No mid-turn interrupt — LangGraph runs the tool loop in one call, so
  enforcement is at the turn boundary (documented ceiling); no callback/recursion
  hook in this PRD.
- No USD/DBU cost cap (tokens only).
- No enforcement on the direct `.run()`/`run_once` path (served paths only).
- No deferred tool loading or ParallelAgent work (separate issues #767/#769).

## Requirements

### Functional
- FR-1: Re-add `session_budget: dict[str, int] | None = None` to `LlmAgent.__init__`
  (only the `"tokens"` key supported; validate and raise `ValueError` otherwise).
- FR-2: Re-add `SessionBudgetExceeded(spent: int, cap: int)` to `_errors.py`,
  re-exported from `apx_agent`; docstring states **per-session cumulative,
  turn-boundary** semantics (no "same iteration" claim).
- FR-3: On each served turn (chat `predict`/`predict_stream`, responses
  `invoke`/`stream`), when the agent declares `session_budget`:
  1. read prior cumulative from the graph `state` channel (0 if absent),
  2. before running, if prior cumulative already ≥ cap → raise
     `SessionBudgetExceeded(spent=prior, cap)` without running the turn,
  3. after the turn, add this turn's usage (sum `usage_metadata` input+output
     across produced `AIMessage`s), persist the new total to the `state`
     channel, and raise `SessionBudgetExceeded(spent=total, cap)` if it crossed.
- FR-4: The cumulative total is keyed to the session so different `thread_id`s
  have independent counters (no cross-session leakage).
- FR-5: A shared helper computes per-turn usage from messages (reuse one
  implementation across all four served entrypoints — no duplicated summing).

### Non-functional
- NFR-1: No new `state_schema()` field — store under the existing keyed `state`
  channel (e.g. `state["session_tokens"]`), so backward-compat is preserved.
- NFR-2: `make check` stays green (minus the pre-existing unbuilt-TS env set).

## Design

### Architecture
- `LlmAgent.__init__` (`_agents.py`) stores `self._session_budget`.
- `SessionBudgetExceeded` in `_errors.py`; re-export in `__init__.py`.
- A helper (e.g. `_session_budget_usage(messages) -> int` + an enforcement
  function) in `_langgraph_executor.py` or a small `_budget.py`, called by both
  `_chat_agent.py` and `_responses_agent.py` at the served-turn boundary.
- Persist cumulative in the graph `state` channel; on the served path read it via
  `graph.get_state(lg_config)` (already used for interrupts, `_chat_agent.py:462`)
  and write the updated total back through the turn's `{"state": {...}}` merge or
  a direct `graph.update_state`.

### Interface changes
```python
LlmAgent(..., session_budget: dict[str, int] | None = None)  # {"tokens": N}

class SessionBudgetExceeded(Exception):
    spent: int
    cap: int
```
No change to `state_schema()` shape.

## Acceptance Criteria

- [ ] AC-1: Given `LlmAgent(session_budget={"tokens": 100})` served via
  `chat_agent_for` with an InMemory checkpointer, when two `predict` turns run on
  the same `thread_id` each reporting `usage_metadata` summing to 60, then the
  **second** turn raises `SessionBudgetExceeded(spent>=120, cap==100)` (cumulative
  crossed), even though neither turn alone exceeds 100.
- [ ] AC-2: Given the same setup, when a single turn's usage stays under the cap
  across turns whose cumulative stays < 100, then no exception is raised.
- [ ] AC-3: AC-1 behavior holds for `predict_stream` (raises after the streaming
  turn that crosses; documented turn-boundary semantics).
- [ ] AC-4: AC-1 behavior holds for the responses `invoke` and `stream`
  entrypoints.
- [ ] AC-5: Given prior cumulative already ≥ cap (from earlier turns persisted in
  state), when a new turn starts, then it raises **before running** the graph
  (spent==prior).
- [ ] AC-6: Given two different `thread_id`s under the same agent, when each runs
  turns, then their cumulative counters are independent (no cross-session leak).
- [ ] AC-7: `SessionBudgetExceeded` is importable from `apx_agent`, has int
  `spent`/`cap`, and its docstring says per-session cumulative / turn-boundary
  (no "same iteration"); `session_budget` with a non-`tokens` key raises
  `ValueError`.
- [ ] AC-8: All ACs drive the served path (`_chat_agent`/`_responses_agent` +
  checkpointer + thread_id), not `LangGraphExecutor`/`run_once`.
- [ ] AC-9: Full pytest suite shows no new failures beyond the pre-existing
  unbuilt-internal-TS-runtime set.

## Risks
- **Reading/writing the persisted counter**: `graph.get_state`/`update_state`
  semantics + the `_merge_state` reducer must round-trip the counter. Mitigation:
  AC-1/AC-5 drive real turns through a checkpointer and assert cross-turn behavior.
- **No checkpointer (stateless serving)**: without a checkpointer there's no
  cross-turn persistence — the cap degrades to per-turn. Mitigation: document;
  cumulative enforcement requires a configured checkpointer (the durable path).
- **usage_metadata absent** on some model responses: sum defensively (missing →
  0) but do not silently disable the cap.

## Open Questions
- [ ] Where exactly to write the updated total: fold into the turn's normal
  `{"state": {...}}` return vs an explicit `graph.update_state` after the run —
  implementation to pick whichever round-trips cleanly through `_merge_state`.

---

## Agent Handoff

```json
{
  "prd_version": "1.0",
  "goal": "Enforce session_budget as a cumulative per-session token cap persisted in the checkpointer state channel, on every served entrypoint, verified through the served path with a checkpointer — all gate tests passing.",
  "success_criteria": [
    "AC-1: cumulative across 2 same-thread predict turns raises when total crosses cap",
    "AC-2: under cumulative cap → no raise",
    "AC-3: predict_stream cumulative enforcement (turn boundary)",
    "AC-4: responses invoke + stream cumulative enforcement",
    "AC-5: already-over at turn start → raise before running",
    "AC-6: independent counters per thread_id (no cross-session leak)",
    "AC-7: SessionBudgetExceeded shape/import/docstring + tokens-only validation",
    "AC-8: verified via served path + checkpointer, not run_once",
    "AC-9: no new full-suite failures beyond the pre-existing TS-runtime set"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest tests/test_session_budget.py -q",
    "progress_metric": "failing test count (target: 0)",
    "known_ceiling": "Without a configured checkpointer there is no cross-turn state, so cumulative enforcement is impossible and degrades to per-turn — documented, not faked. Mid-turn interrupt is out of scope (LangGraph runs the loop in one call)."
  },
  "acceptance_criteria": [
    {"id": "AC-1", "description": "cumulative across 2 same-thread predict turns raises when total crosses cap", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac1_cumulative_predict_crosses_cap"},
    {"id": "AC-2", "description": "under cumulative cap → no raise", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac2_under_cap_no_raise"},
    {"id": "AC-3", "description": "predict_stream cumulative enforcement at turn boundary", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac3_predict_stream_cumulative"},
    {"id": "AC-4", "description": "responses invoke + stream cumulative enforcement", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac4_responses_cumulative"},
    {"id": "AC-5", "description": "already-over at turn start raises before running", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac5_over_at_turn_start_refuses"},
    {"id": "AC-6", "description": "independent counters per thread_id", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac6_no_cross_session_leak"},
    {"id": "AC-7", "description": "SessionBudgetExceeded shape/import/docstring + tokens-only validation", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac7_exception_shape_and_validation"},
    {"id": "AC-8", "description": "verified via served path + checkpointer, not run_once", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac8_served_path_marker"},
    {"id": "AC-9", "description": "no new full-suite failures beyond pre-existing TS-runtime set", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_session_budget.py", "gate_test": "test_ac9_suite_regression_marker"}
  ],
  "must_have": [
    "FR-1: session_budget param on LlmAgent (tokens-only, validated)",
    "FR-2: SessionBudgetExceeded exception, re-exported, per-session/turn-boundary docstring",
    "FR-3: read-prior / before-check / add-usage / persist / raise on served turn",
    "FR-4: per-thread_id independent counters",
    "FR-5: single shared usage-summing helper across all four served entrypoints"
  ],
  "out_of_scope": [
    "per-turn cap",
    "mid-turn interrupt / callback hook",
    "USD or DBU cost cap",
    "run_once/.run() path enforcement",
    "deferred tool loading (#767), ParallelAgent isolation (#769)"
  ],
  "constraints": {
    "tech_stack": "Python 3.11+, uv, LangGraph (StateGraph/checkpointer), MLflow",
    "key_files": [
      "python/src/apx_agent/_agents.py",
      "python/src/apx_agent/_errors.py",
      "python/src/apx_agent/__init__.py",
      "python/src/apx_agent/_chat_agent.py",
      "python/src/apx_agent/_responses_agent.py",
      "python/src/apx_agent/_compile.py",
      "python/tests/test_session_budget.py"
    ],
    "patterns": "Persist cumulative under the existing keyed `state` channel (state_schema() ApxState, _merge_state reducer) — no new schema field; read via graph.get_state(lg_config) as _chat_agent.py already does for interrupts; sum usage_metadata (input+output) across AIMessages; one shared helper used by all four served entrypoints; tests drive chat_agent_for/compile_to_responses_agent with an InMemory checkpointer + a stable thread_id, never run_once"
  },
  "escalate_on": [
    "graph.update_state / _merge_state cannot round-trip the counter across turns",
    "a served entrypoint has no access to a stable thread_id / checkpointer config",
    "usage_metadata is unavailable in a way that would silently disable the cap",
    "enforcing before-run requires re-architecting the served predict flow"
  ],
  "loop_guards": {
    "max_iterations": 8,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```

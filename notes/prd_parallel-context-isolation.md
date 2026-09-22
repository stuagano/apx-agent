# PRD: ParallelAgent context isolation in the compiled StateGraph (#769)

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-15

## Summary

A served `ParallelAgent` fans out through the compiled LangGraph
(`_compile_parallel_agent`), where every branch node reads the full shared
`MessagesState`. Independent branches therefore each receive the entire
conversation — O(branches) context bloat and cross-branch leakage. This change
trims each branch's input **inside the compiled StateGraph** to only the
triggering user message plus a single merged system message, so isolation
actually takes effect on the served `predict`/`predict_stream` path (the earlier
attempt in `ParallelAgent.run` never ran when served — see #766).

## Background

`python/src/apx_agent/_compile.py::_compile_parallel_agent` builds a
`StateGraph(state_schema())`, adds each sub-agent as a node via
`graph.add_node(name, _compile_any(sub, ctx))`, and wires `START → node → END`.
Branches merge outputs via `MessagesState`'s `add_messages` reducer. Each node is
the sub-agent's own compiled graph and reads `state["messages"]` verbatim — the
full accumulated history.

`ParallelAgent.run` (the direct, non-served path) was previously changed to trim
context, but production serves via the compiled graph and never calls `.run()`,
so the fix was inert (Isaac Review, #766). That change has been reverted;
`ParallelAgent.run` is back to `origin/main`.

Prior art for per-branch input shaping: `_build_subagent_input_messages` already
constructs a clean message tail for handoff sub-agents in the same module — the
established pattern for injecting a trimmed message list into a sub-agent node.

## Goals

- G1: On the served/compiled path, each `ParallelAgent` branch receives only the
  triggering user message + one merged system message — never the full history.
- G2: Behavior verified by driving the **compiled graph**, not `ParallelAgent.run`.
- G3: The review's edge cases are handled (no double system message; instruction-
  seeded no-user fan-out still runs the branches).

## Non-Goals

- No `context_mode="shared"` opt-out knob — this is a hard breaking change
  (decided). Add a knob only if field feedback later demands it.
- No change to `SequentialAgent`/`RouterAgent`/`LoopAgent` fan-in or handoff
  shaping.
- No change to `ParallelAgent.run` semantics beyond what already landed
  (`origin/main`); the served/compiled path is the target.
- No change to the `add_messages` fan-in / output merge behavior.

## Requirements

### Functional
- FR-1: In `_compile_parallel_agent`, wrap each branch node so the sub-agent is
  invoked with an **isolated** message list derived from the incoming state:
  `[merged_system?] + [last_user]`, where `last_user` is the last `user`/Human
  message in state and `merged_system` is defined by FR-3. Prior assistant/user
  turns are dropped.
- FR-2: Applies to **all** branch sub-agent types (LlmAgent, nested composites,
  remote/A2A) — the wrapper trims the input state uniformly before delegating to
  `_compile_any(sub, ctx)`.
- FR-3: When the `ParallelAgent` has its own `instructions` **and** the incoming
  state carries a system message, the branch receives a **single** system message
  merging the two (agent instructions first, then the incoming system text),
  never two consecutive system messages. If only one source is present, that one
  is used; if neither, no system message is injected.
- FR-4: If there is no user message in the incoming state, branches still run
  with `[merged_system]` when a system/instructions source exists (instruction-
  seeded fan-out); they are not skipped and the graph does not short-circuit to
  empty output.

### Non-functional
- NFR-1: Reuse existing helpers (the `_build_subagent_input_messages` message-
  shaping pattern; `state_schema()`); do not duplicate message-role logic.
- NFR-2: `make check` stays green (full suite, minus the pre-existing unbuilt-TS
  env failures) — no regression to fan-out output merging.

## Design

### Architecture
Change `_compile_parallel_agent` (`python/src/apx_agent/_compile.py`). Instead of
`graph.add_node(name, _compile_any(sub, ctx))`, add a wrapper node that:
1. reads `state["messages"]`,
2. computes the isolated input list (`_isolate_parallel_branch_input(state, agent)`),
3. invokes the compiled sub-agent graph with `{"messages": isolated}`,
4. returns the sub-agent's produced messages for the `add_messages` reducer.

Add a small pure helper `_isolate_parallel_branch_input(messages, agent)` that
returns `[merged_system?] + [last_user?]` per FR-1/FR-3/FR-4, so the trimming
logic is independently unit-testable.

### Interface changes
None public. `ParallelAgent`'s constructor and `.run()` are unchanged. This is a
behavior change to served fan-out only (breaking — documented in the PR).

## Acceptance Criteria

- [ ] AC-1: Given a `ParallelAgent` of 2 recording branch sub-agents compiled via
  `compile_to_langgraph`, when the compiled graph is invoked with `[system "S",
  user "first", assistant "a", user "last"]`, then each branch is invoked with
  exactly `[system "S", user "last"]` (prior turns dropped, incoming system kept).
- [ ] AC-2: Given `ParallelAgent(branches, instructions="X")` and an incoming
  system message "S", when the compiled graph is invoked, then each branch
  receives a **single** system message whose content merges "X" then "S" (not two
  system messages) followed by the last user message.
- [ ] AC-3: Given `ParallelAgent(branches, instructions="X")` with no incoming
  system and `[user "q"]`, when compiled+invoked, then each branch receives
  `[system "X", user "q"]`.
- [ ] AC-4: Given `ParallelAgent(branches, instructions="X")` invoked with a state
  that has a system/instructions source but **no user message**, when
  compiled+invoked, then each branch still runs and receives `[system "X"]` (no
  short-circuit to empty).
- [ ] AC-5: Given a `ParallelAgent` with no instructions and `[user "q"]` (no
  system), when compiled+invoked, then each branch receives `[user "q"]` with no
  injected empty system message.
- [ ] AC-6: The isolation is exercised through the compiled StateGraph path (the
  test compiles the agent and drives the graph), not via `ParallelAgent.run`.
- [ ] AC-7: `_isolate_parallel_branch_input` unit-tests cover the message-merge
  and last-user selection in isolation.
- [ ] AC-8: Full pytest suite shows no new failures beyond the pre-existing
  unbuilt-internal-TS-runtime set.

## Risks
- **Wrapper breaks fan-in merge**: the wrapper must still return the sub-agent's
  messages so `add_messages` accumulates outputs. Mitigation: AC-1..AC-5 invoke
  the full compiled graph and assert branch inputs via spy sub-agents; NFR-2 full
  suite guards the merge.
- **A2A/remote branches**: trimming input may interact with remote-leaf binding.
  Mitigation: FR-2 trims only the input message list; escalate if a remote branch
  test breaks.
- **Multiple system messages in incoming state**: define "the incoming system" as
  the concatenation of all incoming system messages in order (documented), merged
  after the agent's instructions.

## Open Questions
- [ ] If a branch sub-agent is itself an `LlmAgent` with its own `instructions`,
  its compiled graph already prepends them; confirm the merged system from FR-3
  does not double with the sub-agent's own instruction handling.

---

## Agent Handoff

```json
{
  "prd_version": "1.0",
  "goal": "Trim each ParallelAgent branch's input inside the compiled StateGraph to [merged system + last user], verified through the compiled/served path — all gate tests passing.",
  "success_criteria": [
    "AC-1: compiled branch receives only [incoming system, last user]",
    "AC-2: instructions + incoming system merge into ONE system message",
    "AC-3: instructions only + user → [system X, user q]",
    "AC-4: instruction-seeded no-user fan-out still runs branches with [system]",
    "AC-5: no instructions/system → branch gets [user] with no injected system",
    "AC-6: verified by driving the compiled graph, not ParallelAgent.run",
    "AC-7: _isolate_parallel_branch_input unit-tested",
    "AC-8: no new full-suite failures beyond the pre-existing TS-runtime set"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest tests/test_parallel_context_isolation.py -q",
    "progress_metric": "failing test count (target: 0)",
    "known_ceiling": "LangGraph node cannot receive a per-branch input distinct from shared MessagesState without a wrapper node — the wrapper node IS the mechanism; if add_messages fan-in cannot be preserved alongside trimmed input, escalate.",
    "re_represented": false
  },
  "acceptance_criteria": [
    {"id": "AC-1", "description": "compiled branch receives only [incoming system, last user], prior turns dropped", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac1_branch_gets_system_and_last_user_only"},
    {"id": "AC-2", "description": "agent instructions + incoming system merge into one system message", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac2_instructions_and_system_merge_single"},
    {"id": "AC-3", "description": "instructions only + user → [system X, user q]", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac3_instructions_only"},
    {"id": "AC-4", "description": "instruction-seeded no-user fan-out still runs branches with [system]", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac4_instruction_seeded_no_user"},
    {"id": "AC-5", "description": "no instructions/system → branch gets [user] with no injected system", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac5_no_system_no_injection"},
    {"id": "AC-6", "description": "isolation exercised through the compiled StateGraph, not ParallelAgent.run", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac6_via_compiled_graph"},
    {"id": "AC-7", "description": "_isolate_parallel_branch_input unit-tested for merge + last-user selection", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac7_isolate_helper_unit"},
    {"id": "AC-8", "description": "no new full-suite failures beyond pre-existing TS-runtime set", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_parallel_context_isolation.py", "gate_test": "test_ac8_suite_regression_marker"}
  ],
  "must_have": [
    "FR-1: wrapper node trims branch input to [merged system + last user] in _compile_parallel_agent",
    "FR-2: applies to all branch sub-agent types",
    "FR-3: merge instructions + incoming system into one system message",
    "FR-4: instruction-seeded no-user fan-out still runs branches"
  ],
  "out_of_scope": [
    "context_mode=\"shared\" opt-out knob",
    "SequentialAgent/RouterAgent/LoopAgent changes",
    "ParallelAgent.run semantics beyond origin/main",
    "add_messages fan-in / output-merge changes"
  ],
  "constraints": {
    "tech_stack": "Python 3.11+, uv, LangGraph (create_agent/StateGraph), MLflow",
    "key_files": [
      "python/src/apx_agent/_compile.py",
      "python/src/apx_agent/_agents.py",
      "python/tests/test_parallel_context_isolation.py"
    ],
    "patterns": "Follow _build_subagent_input_messages for per-branch message shaping; reuse state_schema(); pure helper _isolate_parallel_branch_input for the trim logic; tests drive compile_to_langgraph / the compiled StateGraph, never ParallelAgent.run"
  },
  "escalate_on": [
    "trimming a branch's input state breaks the add_messages fan-in / output merge",
    "a remote/A2A branch test breaks under input trimming",
    "the compiled sub-agent graph cannot be invoked with a distinct per-branch message list without a new dependency",
    "a branch LlmAgent's own instructions double with the merged system message"
  ],
  "loop_guards": {
    "max_iterations": 7,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```

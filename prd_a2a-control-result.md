# PRD: A2A Control-Result Protocol

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-14

## Summary
Enable remote `LoopAgent` bodies and `HandoffAgent` peers — running as separate Databricks
Apps over A2A — to carry structured control signals (`finish_loop` / continue,
`transfer_to:<target>` + context) back to the orchestrator, so remote control-flow routing
is identical to the in-process sentinel-tool path. Closes the exclusion recorded on the
`codex/declarative-pipeline-agent` branch and lifts the two guards at `_wiring.py:492-497`.
Success metric: remote loop + handoff pass the same routing assertions as local, proven by
a two-App CTK reality test, with zero new failures in the frozen suite.

## Background
On the declarative-A2A branch, declared agent-graph leaves can bind to remote Databricks
Apps for **sequential** work: the peer's final answer is extracted (`_extract_remote_text`,
`_remote.py:184-208`) and returned. Control-flow peers cannot, because control is signaled
in-process by the LLM calling a synthetic sentinel tool (`finish_loop`, `transfer_to_<name>`,
built by `_build_synthetic_tool`, `_compile.py:493`) and the router reads the last
`AIMessage.tool_calls` from shared graph state (`_last_ai_tool_call_name`, `_compile.py:517`).
Over A2A the wire is text-only (`_a2a_models.py` models only `TextPart`; "data parts not yet
modelled"), so the tool_call is lost. Rather than parse control out of final text (explicitly
forbidden by the design), `_wiring.py:492` and `:497` reject remote loop/handoff bindings.
A partial control shape already exists: `task_continue_request` (`_remote.py:250`).

## Goals
- G1: A remote LoopAgent body can end the loop (`finish_loop`) or request another iteration,
  routed identically to a local loop body.
- G2: A remote HandoffAgent peer can transfer to an orchestrator-known target
  (`transfer_to:<target>`) with optional context, routed identically to a local handoff.
- G3: Both guards at `_wiring.py:492-497` removed; remote loop/handoff bindings resolve.
- G4: No control signal is ever emulated via final-answer text; the wire carries a typed,
  structured control payload.

## Non-Goals
- Remote-declared transfer targets: a peer may only transfer to a target that exists in the
  **local** orchestrator graph. An unknown target is a protocol error, not a remote lookup.
- Control protocol for `ParallelAgent` / `RouterAgent` or any peer beyond loop + handoff.
- Live Databricks workspace deploy validation (tests use local ASGI + mocked SDK, matching
  branch convention).
- Changing public API surface: `RemoteDatabricksAgent.run`/`.stream` signatures unchanged;
  remote transport stays private.

## Requirements

### Functional
- FR-1: Extend the A2A response model (`_a2a_models.py`) with an **optional structured
  control field** — a serialized tool_call (`name`, `args`, `id`) carried on the reply
  (on `TaskStatus`/`Message` metadata or a new typed part), never inside a `TextPart`.
  Absence of the field ⇒ ordinary (non-control) reply; back-compatible with existing peers.
- FR-2: The A2A server side, when a served agent's turn ends on a control sentinel tool_call,
  serializes that tool_call into the FR-1 field on the outbound response.
- FR-3: The orchestrator (`_remote.py`) gains a control-extractor (sibling to
  `_extract_remote_text`) that reads the FR-1 field and reconstructs an `AIMessage` whose
  `tool_calls` mirror the peer's — so the existing `_last_ai_tool_call_name` router consumes
  it with no routing-logic change.
- FR-4: For handoff, the reconstructed `transfer_to:<target>` is validated against the local
  graph's node names before routing; an unknown target raises a protocol error naming the
  logical binding and target (no transport/URL leakage), with the original context preserved.
- FR-5: Remove the guards at `_wiring.py:492` (loop) and `_wiring.py:497` (handoff); remote
  loop/handoff `_RemoteLeafBinding`s resolve like sequential ones.
- FR-6: A control reply and its accompanying content coexist — a peer that both answers and
  signals control returns both; content is still available to state, control drives routing.

### Non-functional
- NFR-1: Back-compatible — a peer that returns no control field behaves exactly as today
  (sequential remote leaf). Existing `test_declared_a2a_binding_reality_ctk.py` stays green.
- NFR-2: No secret/URL/transport-diagnostic leakage in any error or user-visible output
  (upholds the prior final-review ruling).
- NFR-3: MLflow trace parentage across the A2A hop is preserved for control replies (the
  branch's cross-app tracing must still hold).

## Design

### Architecture
- **Wire (`_a2a_models.py`)**: add a Pydantic `ControlSignal` (fields: `name: str`,
  `args: dict`, `id: str | None`) and attach it as an optional field on the reply carrier
  (prefer `TaskStatus.message` metadata or a dedicated optional field on `Task`; follow the
  existing `task_continue_request` shape at `_remote.py:250` for naming/consistency).
  Do NOT overload `TextPart`.
- **Server emit**: at the served-agent egress that today produces the reply, detect a
  trailing control sentinel tool_call (reuse the local detection used by
  `_last_ai_tool_call_name`) and populate `ControlSignal`. Reuse `_langchain_to_output_item`
  for lossless tool_call ↔ wire conversion (already proven on this branch).
- **Orchestrator ingest (`_remote.py`)**: `_extract_remote_control(response) -> ControlSignal | None`;
  when present, the bound-leaf runnable returns an `AIMessage(content=<text>, tool_calls=[...])`
  so `_compile.py` routing (`_last_ai_tool_call_name`) sees the sentinel exactly as local.
- **Guard removal + validation (`_wiring.py`)**: delete the two `raise`s; for handoff, resolve
  `transfer_to:<target>` against local graph node names; reject unknown targets.

### Interface changes
- New: `ControlSignal` model in `_a2a_models.py`; optional field on the A2A reply carrier.
- New private: `_extract_remote_control` in `_remote.py`.
- Changed: `_wiring.py` remote-binding resolver (guards removed, handoff target validation).
- Unchanged public: `RemoteDatabricksAgent.run` / `.stream`.

### Data model
`ControlSignal { name: str; args: dict[str, Any]; id: str | None }` — serialized form of a
LangChain tool_call. Carried optionally on the A2A reply; `None`/absent ⇒ non-control reply.

## Acceptance Criteria

- [x] AC-1: Given a remote LoopAgent body bound over A2A whose served agent calls
  `finish_loop`, when the orchestrator runs the loop, then the loop terminates on that
  iteration — same terminal state as an equivalent local loop body (asserted equal).
- [x] AC-2: Given a remote LoopAgent body that does NOT call `finish_loop`, when the
  orchestrator runs the loop, then it performs another iteration (up to the loop's max),
  matching local continue behavior.
- [x] AC-3: Given a remote HandoffAgent peer that calls `transfer_to:<known_target>` with
  context, when the orchestrator routes, then control transfers to that local target and the
  context is delivered — same routing as a local handoff (asserted).
- [x] AC-4: Given a remote peer that calls `transfer_to:<unknown_target>`, when the
  orchestrator ingests it, then a protocol error naming the binding + target is raised and no
  URL/transport detail leaks; no successful answer is fabricated.
- [x] AC-5: Given a remote leaf that returns NO control field (existing sequential peer),
  when the orchestrator runs, then behavior is byte-for-byte the prior behavior (existing
  CTK reality suite green).
- [x] AC-6: Given the two guards at `_wiring.py:492-497`, when the branch is built, then they
  are removed and remote loop/handoff bindings resolve without raising.
- [x] AC-7: Given a control reply that also carries content, when ingested, then routing uses
  the control signal AND the content remains available in state (no loss).
- [x] AC-8: Given a remote control round-trip, when traced, then MLflow parentage across the
  A2A hop is intact (cross-app trace assertion, as in the existing reality test).

## Risks
- R1 (LangChain-internals coupling over the wire): mirroring the tool_call ties the wire to
  LangChain tool_call shape. Mitigation: isolate serialization in one `ControlSignal`
  converter reusing `_langchain_to_output_item`; a single seam to update if the internal
  shape changes.
- R2 (control-vs-content ambiguity): a reply with both a sentinel and a real answer. Mitigation:
  FR-6 + AC-7 make coexistence explicit and tested.
- R3 (security — unknown/hostile transfer target): a remote peer names a target it shouldn't.
  Mitigation: FR-4 local-graph allowlist; unknown ⇒ protocol error (AC-4).
- R4 (full-gate not green): 7 pre-existing unrelated failures on the branch. Mitigation:
  gate on the scoped suite + declared-A2A suite, compare against the recorded 7-failure baseline.

## Open Questions
- [x] Exact carrier for `ControlSignal` — `TaskStatus.message` metadata vs. a dedicated
  optional `Task` field. Resolve during Phase A by matching the `task_continue_request`
  precedent; both satisfy FR-1. (Escalate only if neither fits cleanly.)

---

## Agent Handoff

```json
{
  "prd_version": "1.0",
  "goal": "Remote LoopAgent bodies and HandoffAgent peers over A2A route control flow (finish_loop / continue / transfer_to:<known target>) identically to local, via a typed control field on the A2A reply — not text — with both _wiring.py:492-497 guards removed and the frozen pytest suite green against the 7-failure baseline.",
  "success_criteria": [
    "remote finish_loop terminates loop == local",
    "remote no-signal continues loop == local",
    "remote transfer_to:<known> routes + delivers context == local",
    "transfer_to:<unknown> is a protocol error, no leakage",
    "no-control replies unchanged (existing CTK green)",
    "both guards removed",
    "control + content coexist",
    "cross-app MLflow parentage intact"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run --frozen pytest tests/test_remote.py tests/test_remote_leaf_bindings.py tests/test_wiring.py tests/test_compile.py tests/test_a2a.py tests/test_declared_a2a_binding_reality_ctk.py tests/gates/ -q",
    "progress_metric": "failing acceptance-gate test count",
    "known_ceiling": "A2A message model cannot carry structured non-text data (it can — extend _a2a_models.py) or LangChain tool_call round-trip is lossy (mitigated by reusing _langchain_to_output_item)",
    "re_represented": false
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "Remote finish_loop terminates the loop identically to a local loop body", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac1.py", "gate_test": "test_remote_finish_loop_terminates" },
    { "id": "AC-2", "description": "Remote loop body without finish_loop continues iterating like local", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac2.py", "gate_test": "test_remote_loop_continues" },
    { "id": "AC-3", "description": "Remote transfer_to:<known target> routes and delivers context like local handoff", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac3.py", "gate_test": "test_remote_handoff_routes_known_target" },
    { "id": "AC-4", "description": "transfer_to:<unknown target> raises a protocol error naming binding+target, no URL/transport leak, no fabricated answer", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac4.py", "gate_test": "test_unknown_transfer_target_rejected" },
    { "id": "AC-5", "description": "No-control remote leaf behaves exactly as before (back-compat)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac5.py", "gate_test": "test_no_control_reply_unchanged" },
    { "id": "AC-6", "description": "Both _wiring.py:492-497 guards removed; remote loop/handoff bindings resolve", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac6.py", "gate_test": "test_remote_loop_handoff_bindings_resolve" },
    { "id": "AC-7", "description": "Control signal + content coexist; routing uses control, content stays in state", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac7.py", "gate_test": "test_control_and_content_coexist" },
    { "id": "AC-8", "description": "MLflow trace parentage across A2A hop intact for control replies", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_a2a_control_result_ac8.py", "gate_test": "test_cross_app_trace_parentage_control" }
  ],
  "must_have": [
    "FR-1 typed ControlSignal on A2A reply (not TextPart)",
    "FR-2 server serializes trailing sentinel tool_call",
    "FR-3 orchestrator reconstructs AIMessage tool_calls for existing router",
    "FR-4 local-graph target validation for transfer",
    "FR-5 remove both _wiring.py guards",
    "FR-6 control+content coexistence"
  ],
  "out_of_scope": [
    "remote-declared transfer targets (local-graph allowlist only)",
    "Parallel/Router remote control",
    "live workspace deploy validation",
    "public API signature changes"
  ],
  "constraints": {
    "tech_stack": "Python, LangChain/LangGraph, Pydantic A2A models, MLflow",
    "key_files": [
      "python/src/apx_agent/_a2a_models.py",
      "python/src/apx_agent/_a2a.py",
      "python/src/apx_agent/_remote.py",
      "python/src/apx_agent/_wiring.py",
      "python/src/apx_agent/_compile.py",
      "python/tests/test_declared_a2a_binding_reality_ctk.py",
      "python/tests/test_remote_leaf_bindings.py"
    ],
    "patterns": "Reuse _langchain_to_output_item for tool_call<->wire conversion; extend the task_continue_request shape (_remote.py:250); keep remote transport private; sanitize named-boundary errors (no URL/credential/transport leakage); build on branch codex/declarative-pipeline-agent; tests use deterministic local models + local ASGI + mocked SDK + local MLflow."
  },
  "preferred_skills": [],
  "escalate_on": [
    "neither TaskStatus.message metadata nor a Task field cleanly carries ControlSignal without weakening types",
    "the local loop/handoff router cannot consume a reconstructed AIMessage without changing routing logic",
    "back-compat with existing sequential remote leaves cannot be preserved",
    "a control round-trip breaks cross-app MLflow parentage and no non-invasive fix exists",
    "removing a guard requires touching public API or a second governance path"
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

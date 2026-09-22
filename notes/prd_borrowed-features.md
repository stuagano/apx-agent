# PRD: Borrowed Features (OpenAI Agents API parity)

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-12

## Summary

Add four capabilities to apx-agent inspired by the OpenAI Agents API: deferred/on-demand
tool loading, a hard session token spend cap, isolated context per branch in
`ParallelAgent`, and versioned loop behavior surfaced as an MLflow trace attribute and in
`apx-agent status`. Together these reduce token cost for large-tool-inventory agents,
prevent runaway loop spend, fix context leakage between parallel branches, and give
operators a stable handle on which loop semantics a deployed agent is running.

## Background

apx-agent agents with many UC functions, Genie spaces, and vector search indexes send
every tool schema on every LLM call — a O(tools) cost per turn with no payoff when only
1-2 tools are actually used. The `max_iterations` cap is the only guard against runaway
loops; there is no token budget. `ParallelAgent` passes the full shared conversation to
every branch (line 759 of `_agents.py`), causing context bloat when branches are
independent. The package version is readable via `importlib.metadata` but never stamped
on traces or surfaced in `apx-agent status`.

## Goals

- G1: `tool_loading="deferred"` on `LlmAgent` reduces first-turn token count by
  deferring all tool schemas; the model fetches schemas on demand via tool_search
- G2: `session_budget={"tokens": N}` raises `SessionBudgetExceeded` when cumulative
  input+output tokens across loop iterations exceed N
- G3: `ParallelAgent` branches each receive only the triggering user message (isolated
  context) — **breaking change from shared context**
- G4: `apx.harness.version` appears on every MLflow trace span and `apx-agent status`
  prints it

## Non-Goals

- USD or DBU spend cap (requires async `system.billing.usage` query — separate feature)
- Per-tool `lazy=True` flag (agent-level opt-in only, per user decision)
- Harness version pinning in `pyproject.toml` (observe only)
- `SequentialAgent` context isolation (shared context is correct for pipelines; opt-in
  `context_mode="isolated"` is out of scope for this PRD)
- New tool implementations (computer use, web search, etc.)

## Requirements

### Functional

- FR-1: `LlmAgent.__init__` accepts `tool_loading: str = "eager"`. When `"deferred"`,
  tool schemas are not sent in the first LLM call; instead a `tool_search_tool_bm25`
  or `tool_search_tool_regex` tool is injected so the model can fetch schemas on demand.
  When the model retrieves a schema, the real tool is added to subsequent calls.
- FR-2: `LlmAgent.__init__` accepts `session_budget: dict[str, int] | None = None`.
  The only supported key is `"tokens"`. After each LLM call the executor accumulates
  `input_tokens + output_tokens` from the response usage dict. When the running total
  exceeds the budget, `SessionBudgetExceeded` is raised with the accumulated count and
  the cap.
- FR-3: `SessionBudgetExceeded` is a new public exception in `apx_agent._errors` (and
  re-exported from `apx_agent`). It carries `spent: int` and `cap: int` attributes.
- FR-4: `ParallelAgent.run` and `ParallelAgent.stream` pass only the last user message
  (extracted from `messages`) to each branch, not the full conversation. The
  `instructions` prepend still applies.
- FR-5: At agent startup (in `_audit.py` or equivalent), set span attribute
  `apx.harness.version` to `importlib.metadata.version("apx-agent")` on the active
  span. This must appear on every top-level predict/predict_stream trace.
- FR-6: `apx-agent status` (human-readable output path in `cli.py:status`) prints
  `harness: <version>` after the existing `profile`/`project`/`target` lines. The
  `--json` path includes `"harness_version"` in the payload dict.

### Non-functional

- NFR-1: Deferred tool loading must not change observable agent behavior when the model
  is given enough context — same tools available, only schema transmission is deferred.
- NFR-2: `SessionBudgetExceeded` must be raised within the same executor iteration that
  crosses the threshold — not deferred to the next turn.
- NFR-3: `ParallelAgent` isolation must not break `get_tool_routers`, `collect_tools`,
  or `fetch_remote_tools` (these aggregate across branches, unaffected by context).
- NFR-4: All four changes must leave `make check` green (2600+ existing tests passing).

## Design

### Architecture

**FR-1 — Deferred tool loading:**
- In `_agents.py` `LlmAgent.__init__`, store `self._tool_loading = tool_loading`.
- In `_compile.py` or `_executor.py` where tools are assembled for the LLM call,
  check `_tool_loading`. When `"deferred"`, register all tool *names* in a lookup
  table but inject only a `tool_search_tool_bm25_20251119` tool into the Anthropic
  `tools` list. On `tool_search` result, look up the real schema and add it for
  subsequent calls. When `"eager"` (default), behavior is unchanged.
- The Anthropic SDK already supports `tool_search_tool_bm25_20251119` and
  `tool_search_tool_regex_20251119` — no new dependency.
- Graceful degradation: if the model/endpoint does not support tool_search (non-200
  response or unsupported tool type), log a warning and fall back to eager loading.

**FR-2/FR-3 — Session token budget:**
- Add `SessionBudgetExceeded(spent, cap)` to `python/src/apx_agent/_errors.py`.
- Re-export from `python/src/apx_agent/__init__.py`.
- In `LlmAgent.__init__`, store `self._session_budget = session_budget`.
- In the executor loop (where `ModelTurnResult.usage` is available — `_executor.py`
  line ~145 area, or wherever usage is read from the LLM response), accumulate a
  `_session_tokens: int` counter on the executor run context. After each LLM call,
  add `usage["input_tokens"] + usage["output_tokens"]`. If the running total exceeds
  `session_budget["tokens"]`, raise `SessionBudgetExceeded(spent=total, cap=cap)`.

**FR-4 — ParallelAgent isolation:**
- In `_agents.py` `ParallelAgent.run` and `ParallelAgent.stream`, extract only the
  last user message from `messages` before passing to branches:
  ```python
  last_user = next((m for m in reversed(messages) if m.role == "user"), messages[-1])
  branch_input = self._prepend_instructions([last_user])
  results = await asyncio.gather(*[sub.run(branch_input, request) for sub in self._agents])
  ```
- `_prepend_instructions` already exists and handles the system prompt prepend.
- `stream` already delegates to `run`; update both methods.

**FR-5 — Harness version trace attribute:**
- In `_audit.py`, add `HARNESS_VERSION = "apx.harness.version"` to `AuditAttrs`.
- In the function that stamps initial span attributes on predict/predict_stream (search
  for where `AuditAttrs.AGENT_NAME` is set), also set `AuditAttrs.HARNESS_VERSION`
  using `importlib.metadata.version("apx-agent")`. Cache the version string at module
  import time to avoid repeated metadata lookups.

**FR-6 — `apx-agent status` harness version:**
- In `cli.py:status`, call `_resolve_version()` (already defined at line 773) and
  print `f"harness: {v}"` after the `target` line.
- In the `--json` branch, add `"harness_version": _resolve_version()` to the
  `payload` dict before `click.echo`.

### Interface changes

```python
# LlmAgent — two new optional constructor params
LlmAgent(
    ...,
    tool_loading: str = "eager",          # "eager" | "deferred"
    session_budget: dict[str, int] | None = None,  # {"tokens": N}
)

# New public exception
class SessionBudgetExceeded(Exception):
    spent: int
    cap: int

# apx-agent status output (human)
profile: fe-stable
project: payroll-coworker
target:  apps
harness: 0.9.4          # NEW

# apx-agent status --json
{
  "profile": "...",
  "harness_version": "0.9.4",   # NEW
  ...
}
```

`ParallelAgent.__init__` signature is unchanged — isolation is a behavior change, not
an API change.

### Data model

No schema changes. `SessionBudgetExceeded` is a plain Python exception with two int
attributes; no persistence.

## Acceptance Criteria

- [ ] AC-1: Given an `LlmAgent` with `tool_loading="deferred"` and 3 registered tools,
  when `run_once` is called, then the first LLM request contains exactly one tool
  (the tool_search tool) and zero of the registered tool schemas.

- [ ] AC-2: Given an `LlmAgent` with `session_budget={"tokens": 100}` and a mock
  executor that returns `usage={"input_tokens": 60, "output_tokens": 60}` on the first
  call, when `run_once` is called, then `SessionBudgetExceeded` is raised with
  `spent >= 100` and `cap == 100`.

- [ ] AC-3: Given an `LlmAgent` with `session_budget={"tokens": 1000}` and a mock that
  returns usage summing to 50 tokens per call, when `run_once` is called, then no
  `SessionBudgetExceeded` is raised and the agent completes normally.

- [ ] AC-4: Given a `ParallelAgent` with 2 branches and a conversation containing 3
  messages (system, user, assistant, user), when `run` is called, then each branch
  receives exactly 1 user message (the last one), not the full 4-message history.

- [ ] AC-5: Given a deployed agent, when any predict call is traced via MLflow, then
  the trace span contains the attribute `apx.harness.version` equal to the installed
  package version string.

- [ ] AC-6: Given `apx-agent status` is run in an apx project directory, then the
  output contains a line `harness: <version>` where `<version>` matches
  `importlib.metadata.version("apx-agent")`.

- [ ] AC-7: Given `apx-agent status --json` is run, then the JSON payload contains key
  `"harness_version"` with the correct version string.

- [ ] AC-8: `SessionBudgetExceeded` is importable from `apx_agent` directly and has
  `spent` and `cap` integer attributes.

## Risks

- **ParallelAgent is a breaking change**: any caller relying on shared history across
  branches will see different behavior. Mitigation: document in CHANGELOG under
  "Breaking Changes"; add a `context_mode="shared"` opt-out for backward compat if
  field feedback demands it (out of scope for this PRD).
- **tool_search availability**: not all Databricks model serving endpoints support
  Anthropic tool_search tool types. Mitigation: graceful degradation to eager loading
  with a logged warning (FR-1 design).
- **Token counting across executor paths**: `_claude_sdk_executor.py` and potentially
  other executor backends track usage differently. Mitigation: AC-2/AC-3 gate this;
  research executor paths during implementation.

## Open Questions

- [ ] Does the LangGraph executor path (`_langgraph_executor.py`) also need the token
  accumulation? Check whether `session_budget` applies to LangGraph-compiled agents or
  only the default executor.

---

## Agent Handoff

```json
{
  "prd_version": "1.0",
  "goal": "Add four OpenAI-API-borrowed features to apx-agent: deferred tool loading, session token budget, ParallelAgent context isolation, and harness version tracing — all tests passing under make check.",
  "success_criteria": [
    "AC-1: LlmAgent tool_loading=deferred sends only tool_search tool on first call",
    "AC-2: session_budget tokens cap raises SessionBudgetExceeded when exceeded",
    "AC-3: session_budget does not raise when under cap",
    "AC-4: ParallelAgent branches each receive only the last user message",
    "AC-5: apx.harness.version span attribute present on every predict trace",
    "AC-6: apx-agent status prints harness: <version>",
    "AC-7: apx-agent status --json includes harness_version key",
    "AC-8: SessionBudgetExceeded importable from apx_agent with spent and cap attrs"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest tests/test_borrowed_features.py -q",
    "progress_metric": "failing test count (target: 0)",
    "known_ceiling": "tool_search not supported on target model endpoint — deferred loading falls back to eager with warning, AC-1 still passes via mock",
    "re_represented": false
  },
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "description": "LlmAgent with tool_loading=deferred sends only tool_search tool schema on first LLM call",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac1_deferred_tool_loading_first_call"
    },
    {
      "id": "AC-2",
      "description": "session_budget tokens cap raises SessionBudgetExceeded when usage exceeds cap",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac2_session_budget_exceeded"
    },
    {
      "id": "AC-3",
      "description": "session_budget does not raise when cumulative tokens stay under cap",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac3_session_budget_not_exceeded"
    },
    {
      "id": "AC-4",
      "description": "ParallelAgent passes only last user message to each branch",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac4_parallel_agent_context_isolation"
    },
    {
      "id": "AC-5",
      "description": "apx.harness.version span attribute present on predict trace",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac5_harness_version_span_attribute"
    },
    {
      "id": "AC-6",
      "description": "apx-agent status human output includes harness: <version> line",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac6_status_harness_version_human"
    },
    {
      "id": "AC-7",
      "description": "apx-agent status --json includes harness_version key",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac7_status_harness_version_json"
    },
    {
      "id": "AC-8",
      "description": "SessionBudgetExceeded importable from apx_agent with spent and cap int attrs",
      "verifiable": true,
      "test_type": "pytest",
      "gate_file": "python/tests/test_borrowed_features.py",
      "gate_test": "test_ac8_session_budget_exceeded_exception_shape"
    }
  ],
  "must_have": [
    "FR-1: LlmAgent tool_loading param with deferred/eager modes",
    "FR-2: LlmAgent session_budget param with token accumulation and raise",
    "FR-3: SessionBudgetExceeded exception with spent and cap attrs",
    "FR-4: ParallelAgent branch isolation to last user message",
    "FR-5: apx.harness.version on every predict trace span",
    "FR-6: apx-agent status and status --json surface harness version"
  ],
  "out_of_scope": [
    "USD or DBU spend cap",
    "Per-tool lazy=True flag",
    "Harness version pinning in pyproject.toml",
    "SequentialAgent context isolation",
    "Computer use or web search tools"
  ],
  "constraints": {
    "tech_stack": "Python 3.11+, uv, Anthropic SDK, MLflow, Click",
    "key_files": [
      "python/src/apx_agent/_agents.py",
      "python/src/apx_agent/_errors.py",
      "python/src/apx_agent/_audit.py",
      "python/src/apx_agent/_executor.py",
      "python/src/apx_agent/_claude_sdk_executor.py",
      "python/src/apx_agent/cli.py",
      "python/src/apx_agent/__init__.py",
      "python/tests/test_borrowed_features.py"
    ],
    "patterns": "Existing params added as optional kwargs to __init__ with safe defaults; exceptions in _errors.py re-exported from __init__.py; span attrs via AuditAttrs constants in _audit.py; CLI version via _resolve_version() already defined at cli.py:773"
  },
  "escalate_on": [
    "tool_search tool type not accepted by the Anthropic SDK version pinned in pyproject.toml",
    "ParallelAgent callers in examples/ that rely on shared context and break under isolation",
    "Token usage dict keys differ between executor backends (claude_sdk vs langgraph paths)",
    "AuditAttrs span-write function not called on every predict path (e.g. langgraph executor skips it)",
    "make check fails on pre-existing unrelated test for reasons unrelated to this change"
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

# Design: served-path guardrails + fail-closed identity

**Status:** proposed · **Date:** 2026-06-20 · **Source:** ADK functional-gap audit (G1, G2)

Two security-relevant gaps where a developer configures protection that an
agent silently does not enforce once deployed. Both stem from the same root
cause: the **served execution path bypasses the `LlmAgent.run()` wrapper** where
this logic lives.

---

## Background — two execution paths

`LlmAgent` is invoked two different ways, and only one of them runs the
guard/callback wrappers:

| Path | Entry | How it runs the agent |
|---|---|---|
| **Legacy / in-process** | `LlmAgent.run()` / `.stream()` (`_agents.py`) | wraps the turn with callbacks + guardrails (`_agents.py:188-219`) |
| **Served (production)** | `ApxChatAgent.predict` (`/invocations`), `compile_to_responses_agent` (`/responses`) | calls `compile_to_langgraph(agent, ...)` and runs the **compiled graph** |

The compiled graph is built from the agent's tools, instructions, and model —
it does **not** call `LlmAgent.run()`, and `_compile.py` contains zero
references to `input_guardrails` / `output_guardrails` /
`before_agent_callback` / `after_agent_callback`. So everything wired in
`_agents.py:188-219` is dead on the production endpoints.

---

## G1 — guardrails & agent callbacks never fire on the served path

### Problem

```python
agent = LlmAgent(
    instructions=...,
    input_guardrails=[prompt_injection_heuristic()],   # configured…
    before_agent_callback=audit_entry,
)
```

The guardrails and `before/after_agent` callbacks run on `agent.run(...)` but are
**silent no-ops** under `/invocations` and `/responses` — the two endpoints you
actually deploy. A prompt-injection input guard or an entry audit hook a
developer configures *looks* active and never executes in production. This is a
false sense of protection, which is worse than no protection.

### Evidence

- Sole invocation sites: `_agents.py:188-196` (`run`), `:202-219` (`stream`).
- Served path compiles instead: `_chat_agent.py:611` (`compile_to_langgraph`),
  `_responses_agent.py:62` (imports `compile_to_langgraph`), and runs the
  executor (`_responses_agent.py:764` `_run_executor_sync`).
- `grep input_guardrails|before_agent_callback _compile.py _chat_agent.py
  _responses_agent.py _executor.py` → **no matches**.

### Proposed design

Move enforcement into the **single compile chokepoint** so both endpoints (and
sub-agent composites) inherit it, rather than duplicating it in each `predict`.

`compile_to_langgraph` already builds a `StateGraph`. Add two optional nodes
around the agent body when the agent declares guards/callbacks:

```
 (entry) → [before_agent + input_guardrails] → agent/tool loop → [output_guardrails + after_agent] → (end)
                      │ reject → short-circuit with the rejection message
```

- **Input guard / `before_agent`**: an entry node that runs the configured
  `input_guardrails`; a non-`None` return short-circuits the graph to a terminal
  message (same contract as `_agents._apply_input_guardrails`). Reuse the exact
  helpers from `_agents.py` so behavior is identical across paths.
- **Output guard / `after_agent`**: an exit node. For non-streaming this can
  replace the final text. For **streaming**, match the legacy path's existing
  behavior (it applies output guardrails on the assembled `full_text`,
  `_agents.py:217`) — i.e. buffer-then-screen at end-of-turn, accepting that a
  streamed token can't be retracted mid-flight. Document that ceiling explicitly.

**Sub-agents:** because composites compile recursively, defining the nodes in
`compile_to_langgraph` means a guard on a sub-agent fires when that sub-agent
runs — which is the intuitive contract. (A future ADK-style *plugin* layer (G5)
would add tree-global hooks; out of scope here.)

### Interim mitigation (cheap, ship first)

At serve time, if an agent has any of `input_guardrails` / `output_guardrails` /
`before_agent_callback` / `after_agent_callback` set, emit a **loud `WARNING`**
in `compile_to_chat_agent` / `compile_to_responses_agent`:

```
WARNING: agent declares input_guardrails but the served (ChatAgent/Responses)
path does not yet enforce them — they run only via LlmAgent.run(). See
docs/design/served-path-guards-and-identity.md (G1).
```

This converts a silent failure into a visible one in one small diff, before the
full wiring lands.

### Alternatives considered

- **Wrap inside each `predict`/responses function** — rejected: duplicates the
  logic in two places, misses sub-agents, and diverges from the legacy path.
- **Make the served path call `LlmAgent.run()`** — rejected: `run()` is the
  in-process loop; the served path deliberately compiles to a graph for
  streaming/executor features. Re-routing it is a larger structural change.

---

## G2 — identity fail-open: missing OBO header → app SP + shared memory principal

> **Status: implemented (fail-closed).** In the Databricks Apps runtime a request
> that resolves no OBO user token is now **rejected** (`ApxIdentityError`) at the
> auth chokepoint of both served paths, instead of silently running as the app
> service principal. Operators that genuinely run as a service principal opt in
> with `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` (downgrades the reject to a
> one-time warning). Local dev and Model Serving (SP-by-default) are unaffected.
> See `_obo.resolve_no_obo_or_raise`.

### Problem

Two compounding failures, neither surfaced as an error:

1. **Tool identity** — when no per-request user token is present, the served
   path builds a **default** `WorkspaceClient` (the app service principal). Tools
   then run with the app's grants, not the caller's — privilege escalation
   relative to the intended OBO scope.
2. **Memory identity** — the memory tools fall back to a single
   `default_principal_id` resolved **once at startup**. Two unauthenticated
   callers therefore read and write memories under the *same* key — cross-user
   memory bleed. With the default in-process memory backend they also share
   process state.

### Evidence

- `_responses_agent.py:205-211`:
  ```python
  if obo.get("user_token"):
      ws = _make_workspace_client(token=obo["user_token"], ...)
  else:
      ws = _make_workspace_client()          # ← app service principal
  ```
- `_memory_tools.py:132, 161, 233`: `principal = principal or default_principal_id`
- `_memory_wiring.py:307`: `default_principal_id=default_principal` (resolved once).

### Proposed design

A **fail-closed identity gate**, scoped to deployed multi-user contexts so local
single-user dev keeps its convenience.

1. **Detect deployment context.** Treat the agent as multi-user when running as
   a Databricks App / Model Serving endpoint (e.g. `DATABRICKS_APP_NAME` set, or
   an explicit `apx_agent` serving flag). Local `apx-agent agents run` stays
   single-user.

2. **In a multi-user context, when neither `custom_inputs.user_token` nor
   `X-Forwarded-Access-Token` is present:**
   - **Reject** the request (`401`/error item), **or**
   - run in an explicit `NO_PRINCIPAL` mode with **tools disabled** and **no
     memory access** — never silently as the app SP.

3. **Gate the service-principal fallback** behind an explicit opt-in
   (`allow_service_principal_fallback=False` by default). Some batch/system
   agents legitimately run as an SP; that must be a deliberate choice, not the
   default for an unauthenticated request.

4. **Never let `default_principal_id` back-fill a deployed multi-user agent.**
   Restrict the default-principal fallback to local/dev single-user mode. In a
   served multi-user context, an absent principal is an error, not a default.

### Risk / compatibility

- **Local dev** relies on the default `WorkspaceClient` + default principal —
  preserved by the context check (gate only fires when deployed multi-user).
- **Existing SP-based agents** — preserved via the explicit opt-in flag; surface
  a one-time warning so owners convert intentionally.
- Roll out behind the flag defaulting to *warn* for one release, then *enforce*,
  to avoid breaking deployments that currently (unknowingly) rely on the
  fallback.

---

## Rollout order

1. **G1 interim warning** + **G2 warn-mode** — small diffs, immediate visibility.
2. **G1 compile-path enforcement** (input guard first — the security-relevant
   one — then output guard + callbacks).
3. **G2 fail-closed enforcement** flipped on after the warn period.

Related larger gaps tracked separately: keyed shared state (G3), ToolContext
(G4), plugin layer (G5).

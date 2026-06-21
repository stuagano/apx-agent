# Design: keyed shared state (G3)

**Status:** proposed · **Date:** 2026-06-21 · **Source:** ADK functional-gap audit (G3)

The highest-leverage workflow-reliability gap. apx-agent has no ADK-style keyed
state: no `output_key`, no `session.state`, no scoping, no `{key}` instruction
templating. Steps hand data to each other as **conversation text**, so threading
a typed value (an id, a parsed object, a per-branch result) between steps is
LLM-parse-dependent and lossy. This doc proposes a typed, named state channel
and the surface around it, in phases.

---

## Background — how state flows today

Every compiled graph uses a single LangGraph `MessagesState` (one `messages`
channel, merged by `add_messages`):

- **SequentialAgent** (`_compile.py:_compile_sequential_agent`) — a linear
  `START → s_0 → … → s_n → END` graph. Each step is a node sharing the *same*
  `messages` channel. Step N+1 sees step N's output only as **appended
  assistant messages** — it must re-read and re-parse the prior text.
- **ParallelAgent** — branches fan out from `START` and their outputs merge via
  `add_messages` (effectively message/string accumulation). No keyed join.
- **HandoffAgent** — routing emits a synthetic
  `"[Routed from X to Y] Please help with the request above."` HumanMessage
  (`_compile.py` `_build_handoff_context`). The downstream agent re-derives
  intent from text.
- **`Conversation.session_state`** (`_conversation.py:321`) is a real
  `dict[str, Any]` field that *loads* from storage, but `set_session_state` was
  **stripped** from the `ConversationStore` interface (see the module docstring's
  "Agent-plane methods stripped" list). So it is **read-only / inert** — there
  is no governed mutator to persist a change.

Net: there is exactly one place to put data (the message stream), and it is
untyped text.

## ADK reference (the target surface)

| ADK capability | What it gives |
|---|---|
| `LlmAgent(output_key="x")` | the agent's final text is written to `state["x"]` |
| `ctx.session.state` / `tool_context.state` | read/write a keyed dict mid-run |
| `{state_key}` in instructions | template the prompt from state before the LLM call |
| `EventActions.state_delta` | a node returns a state patch, merged into the session |
| `State.USER_PREFIX` / `APP_PREFIX` / `TEMP_PREFIX` | scope a key to the user / app / this-invocation, vs the default session scope |

---

## Proposed design

### 1. A typed state channel (foundation)

Replace the bare `MessagesState` with a `TypedDict` that **extends** it:

```python
class ApxState(MessagesState):       # inherits `messages`
    state: Annotated[dict[str, Any], _merge_state]   # named keys, dict-merge reducer
```

- `_merge_state(old, new)` = shallow dict merge, last-write-wins per key (the
  `state_delta` analogue). A node returns `{"state": {"k": v}}` to patch one key
  without touching others.
- **Backward compatible:** `graph.invoke({"messages": [...]})` still works
  (`state` defaults to `{}`), and `result["messages"]` is unchanged. The served
  paths (`graph.invoke`/`graph.stream` with `{"messages": …}` and the
  `result["messages"][input_count:]` slice) need no change. Every existing
  `StateGraph(MessagesState)` call site switches to `StateGraph(ApxState)` — a
  mechanical, behavior-preserving edit.

### 2. `output_key` on `LlmAgent`

Add `output_key: str | None = None`. When set, the agent's guard-node wrapper
(the `_wrap_served_hooks` seam already added for G1 — extend it to *all*
LlmAgents, not just guarded ones) writes the agent's final text to
`state[output_key]`:

```python
return {"messages": new_msgs, "state": {agent._output_key: final_text}}
```

This is the single highest-value piece: `SequentialAgent([extract, summarize])`
where `extract` has `output_key="facts"` makes `facts` available to `summarize`
as data, not prose.

### 3. `{key}` instruction templating

Before an agent's LLM call, interpolate `{key}` placeholders in its instructions
from `state` (missing keys → left as-is or empty, TBD — see open questions).
Implemented in the agent node, which already has access to `state`.

### 4. Scope prefixes (later phase — needs persistence)

Model ADK's four scopes by **key prefix**, dispatched on read/write:

| Prefix | Scope | Backing store |
|---|---|---|
| *(none)* | session (this conversation) | `Conversation.session_state` |
| `user:` | this principal, across sessions | a principal-keyed store (Delta/Lakebase) |
| `app:` | all users of this agent | an app-scoped store |
| `temp:` | this invocation only | the in-graph `state` channel; never persisted |

`temp:` works with phase 1 alone (in-graph only). The other three require
persistence, which depends on §5.

### 5. The `session_state` mutator (later phase — governed write)

To persist session-scoped state, restore a **governed** `set_session_state` on
`ConversationStore` (it was deliberately stripped). This is a write to a
governed surface, so — consistent with the OKF/UC-write principle in this repo —
it should be an explicit, audited store method, not an implicit side effect.
`user:`/`app:` scopes need their own stores; scope them as a follow-up.

---

## Phasing

1. **Phase 1 — in-graph keyed state (no persistence).** `ApxState` channel,
   `output_key`, `{key}` templating, `temp:` scope. Delivers deterministic
   `SequentialAgent`/`ParallelAgent` value-threading — the 80% — with **zero new
   storage** and full backward compatibility. Testable end-to-end without a
   backend.
2. **Phase 2 — session persistence.** Governed `set_session_state` mutator +
   default (session) scope reads/writes through `Conversation.session_state`.
3. **Phase 3 — user/app scopes.** Principal- and app-scoped stores; full prefix
   dispatch.

Recommend shipping **Phase 1 alone first** and validating the ergonomics before
committing to the persistence surface.

## Alternatives considered

- **Keep text-passing, add a parsing convention** (e.g. agents emit JSON the
  next step parses) — rejected: still LLM-parse-dependent and lossy, the exact
  problem.
- **A separate side-channel object threaded through `CompileContext`** —
  rejected: doesn't survive LangGraph's state model; the reducer/`state_delta`
  semantics are what make concurrent (Parallel) writes well-defined.
- **Expose raw LangGraph state to users** — rejected: leaks the runtime; the
  `output_key`/`{key}`/scope surface is the stable contract.

## Risks / compatibility

- **Broad but mechanical:** every `StateGraph(MessagesState)` → `StateGraph(ApxState)`.
  Behavior-preserving because `messages` semantics are unchanged and `state`
  defaults empty.
- **ParallelAgent concurrent writes:** two branches writing the same key — the
  reducer must define a winner (last-write-wins is simplest but order is
  non-deterministic under concurrency). Document it; recommend distinct
  `output_key`s per branch, or a list-append reducer for fan-in keys.
- **Serialization:** `state` values must be JSON-serializable to persist
  (Phase 2+) and to flow through the SQL-Statements/Lakebase stores. Phase 1
  (in-graph only) has no such constraint.

## Open questions (for sign-off before Phase 1)

1. **Missing-key templating:** `{key}` with no value in state → leave the
   literal `{key}`, substitute empty, or raise? (Lean: leave literal, log once.)
2. **`output_key` value:** the final assistant **text**, or a structured value
   when the agent has structured output (which apx doesn't have yet — G-parity
   "output_schema")? (Lean: text for now; revisit with output_schema.)
3. **Reducer for collisions:** last-write-wins vs an explicit list/append mode
   for ParallelAgent fan-in. (Lean: last-write-wins + documented guidance.)
4. **Public surface:** is `output_key` + `{key}` enough for Phase 1, or do we
   also expose a `Dependencies.State` so **tools** can read/write keys
   mid-run (the ADK `tool_context.state` analogue, overlaps G4/ToolContext)?

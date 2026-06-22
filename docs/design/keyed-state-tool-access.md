# Design: tool-level keyed-state access (G3 increment 2)

**Status:** approved · **Date:** 2026-06-21 · **Builds on:** [keyed-shared-state.md](keyed-shared-state.md) (resolves open question #4)

Increment 1 (#240) shipped the `ApxState.state` channel, agent-level `output_key`,
and `{key}` instruction templating — all in-graph, no persistence. This increment
adds the missing half of the ADK `tool_context.state` analogue: a **tool** can read
and write keyed state mid-run. Still in-graph only (phase 1) — no persistence.

---

## The gap

`output_key` writes state *after an agent finishes*; `{key}` reads it *before the
LLM call*. Neither lets a **tool** participate. A tool that looks something up, or
accumulates a running value across calls, has nowhere to put it except its return
string — which the next step must re-parse. ADK closes this with
`tool_context.state` (a mutable keyed dict). apx has no equivalent.

## Why the existing `Dependencies.*` mechanism doesn't reach

Today's dependencies (`UserClient`, `Sql`, `Principal`, …) are FastAPI
`Annotated[T, Depends(...)]` aliases. In the compiled path they resolve **once at
compile time** — `_resolve_deps_for_fn` builds a static `resolved_deps` dict that
`_make_langchain_tool` captures in a closure and splices in via `**resolved_deps`.

State is different: it must be resolved **per tool call**, against the live graph
`state` channel, and writes must flow back into it. That is LangGraph `ToolNode`
territory (`InjectedState` / `InjectedToolCallId` / `Command`), not FastAPI's
request-cycle injection. So `Dependencies.State` needs a separate resolution path.

## Public surface

```python
class Dependencies:
    State: TypeAlias = ...   # a dict-like StateProxy; hidden from the LLM schema
```

Authored like a dict — no LangGraph types in sight:

```python
def resolve_account(name: str, state: Dependencies.State) -> str:
    """Resolve a customer name to an account id and stash it for later steps."""
    acct = crm.lookup(name)
    state["account_id"] = acct          # write — harvested into a state delta
    return f"resolved {name} -> {acct}"
```

The LLM sees only `name`. `state` is excluded from the tool's input schema, exactly
like the other `Dependencies.*` params. A later `{account_id}`-templated agent or a
downstream tool reads the value as data, not prose.

The example writes a **scalar** deliberately — see the concurrency caveat below for
why read-modify-write accumulation (`state["x"] = [*state["x"], y]`) is unsafe when
the LLM emits several tool calls in one turn.

## How it works (hidden from the author)

`_make_langchain_tool` detects a parameter typed `Dependencies.State` and, for that
tool only, takes the injected path instead of the static closure:

1. Add two hidden, `ToolNode`-injected params to the langchain tool's signature:
   - `Annotated[dict, InjectedState("state")]` — the live keyed dict (read).
   - `Annotated[str, InjectedToolCallId]` — needed to build the write-back message.
2. Wrap the user fn:
   - Construct a `StateProxy` over the injected dict. Reads are live; writes
     (`__setitem__`, `update`, `pop`, `setdefault`) are **tracked** into a delta.
   - Call the user fn with the proxy bound to its `state` param (plus any
     compile-time deps it also declares — the two paths compose).
   - After it returns its plain value:
     - **no writes** → return the plain value; `ToolNode` wraps it as a normal
       `ToolMessage` (unchanged behavior).
     - **writes** → return
       `Command(update={"state": delta, "messages": [ToolMessage(plain_return, tool_call_id=...)]})`.
       The `delta` merges via increment 1's shallow last-write-wins reducer.

This keeps the apx tool contract intact (a tool returns a string/value and treats
`state` as a dict) while staying inside `create_agent`'s `ToolNode` — apx does not
need to own tool execution.

## Decisions

- **Delta = tracked writes only**, not a full-dict diff. The proxy records the keys
  the tool actually set; only those are emitted. No spurious keys, clean merge.
- **Nested in-place mutation is not tracked.** `state["facts"].append(x)` mutates a
  value the proxy handed out and bypasses `__setitem__`, so it won't be persisted.
  Reassign instead (`state["facts"] = [...]`). Documented in the `Dependencies.State`
  docstring; matches ADK's same caveat.
- **Missing key behaves like a dict.** `state["x"]` raises `KeyError`;
  `state.get("x")` returns `None`. Consistent with the `{key}` templating choice to
  not invent values.
- **Sync and async tool paths both supported** — both `_sync_wrapper` and the
  `_async_wrapper`/`_sync_bridge` variants get the proxy + `Command` treatment.
- **In-graph only.** Writes land in the `state` channel for the rest of the
  invocation (the `temp:`/in-graph scope). Persistence across turns/sessions is
  phases 2–3 of the parent design, unchanged by this increment.
- **`create_agent` must be given `state_schema=ApxState`** for an injected
  `state` field to exist inside its inner subgraph. `_compile_llm_agent` does not
  pass one today; this increment adds it (scoped to agents that have a
  State-using tool, to keep the blast radius minimal). *Verified by the
  feasibility probe.*

## Concurrency caveat (verified by probe)

When the LLM emits **several tool calls in one turn**, ToolNode runs them in the
same superstep. Each call's injected `state` reflects the value at superstep
*start*, and the increment-1 reducer is shallow **last-write-wins per key**. So:

- **Scalar / independent-key writes are fine** — distinct keys merge cleanly; a
  last write to one key is the intended value.
- **Read-modify-write on a shared key loses data.** Two same-turn calls that each
  do `state["facts"] = [*state.get("facts", []), x]` both read the empty list and
  the second delta overwrites the first — only one `x` survives. (Observed: asking
  for two facts in one turn kept one.)

This is the same hazard the parent design notes for ParallelAgent fan-in. For this
increment we **document it and keep last-write-wins** (consistent with #240). Safe
accumulation across concurrent calls — a list-append reducer for designated keys —
is deferred (YAGNI until a real use case needs it). Guidance: use a distinct key
per writer, or accumulate at the agent level via `output_key`.

## Feasibility gate — PASSED (probe, 2026-06-21, live Claude)

The approach rested on one assumption: `create_agent`'s `ToolNode` honors
`InjectedState` / `InjectedToolCallId` on a `StructuredTool` and applies a `Command`
the tool returns (state update **and** the `ToolMessage`). A minimal live probe
confirmed it — `state["facts"]` was updated and the `ToolMessage` emitted — with one
discovered requirement: **`create_agent` must be passed `state_schema=ApxState`** or
the injected `state` field doesn't exist. The fallback (apx owning a thin tool node)
is **not** needed. The probe also surfaced the concurrency caveat above.

Verified API shape (locks the plan's code):

```python
from langgraph.prebuilt import InjectedState
from langchain_core.tools import InjectedToolCallId
from langgraph.types import Command

def _wrapper(name: str,
             state: Annotated[dict, InjectedState("state")],
             tool_call_id: Annotated[str, InjectedToolCallId]):
    ...  # returns Command(update={"state": delta, "messages": [ToolMessage(ret, tool_call_id=tool_call_id)]})
```

## Testing

- **Feasibility probe** (gate): DONE — committed as a regression test so the
  injected-state + `Command` mechanism is guarded against langgraph upgrades.
- **Unit:** `StateProxy` read/write/delta tracking; missing-key semantics;
  concurrency caveat (two deltas on one key → last-write-wins);
  no-write path returns the plain value (no `Command`); detection of the `State`
  param in `_make_langchain_tool`; schema excludes `state`.
- **Integration:** a tool reads a key written by an earlier `output_key`, writes a
  new key, and a later `{key}`-templated agent reads it — the full
  agent↔tool↔agent thread over one graph.

## Out of scope

- Persistence / scope prefixes (`user:`/`app:`/session) — phases 2–3.
- Structured `output_key` values (depends on output_schema, not yet in apx).
- A general read/write `ToolContext` object beyond `state` (overlaps G4).

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
def remember(fact: str, state: Dependencies.State) -> str:
    """Append a fact to running memory and report the count."""
    facts = state.get("facts", [])
    state["facts"] = [*facts, fact]      # write — harvested into a state delta
    return f"noted ({len(facts) + 1} total)"
```

The LLM sees only `fact`. `state` is excluded from the tool's input schema, exactly
like the other `Dependencies.*` params.

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

## Feasibility gate (verify before building the proxy)

The whole approach rests on one assumption: `create_agent`'s `ToolNode` honors
`InjectedState` / `InjectedToolCallId` on a `StructuredTool` and applies a `Command`
the tool returns (state update **and** the `ToolMessage`). Prove this first with a
minimal tool (fake or live) before implementing `StateProxy`. If it does not hold,
the fallback is apx owning a thin tool node — a larger scope change that would send
us back to design.

## Testing

- **Feasibility probe** (gate): a trivial state-writing tool through a real
  `create_agent` runtime; assert the state delta lands and the `ToolMessage` is
  emitted.
- **Unit:** `StateProxy` read/write/delta tracking; missing-key semantics;
  no-write path returns the plain value (no `Command`); detection of the `State`
  param in `_make_langchain_tool`; schema excludes `state`.
- **Integration:** a tool reads a key written by an earlier `output_key`, writes a
  new key, and a later `{key}`-templated agent reads it — the full
  agent↔tool↔agent thread over one graph.

## Out of scope

- Persistence / scope prefixes (`user:`/`app:`/session) — phases 2–3.
- Structured `output_key` values (depends on output_schema, not yet in apx).
- A general read/write `ToolContext` object beyond `state` (overlaps G4).

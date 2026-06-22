# Design: session-scoped state persistence (G3 phase 2)

**Status:** approved · **Date:** 2026-06-21 · **Builds on:** [keyed-shared-state.md](keyed-shared-state.md) (phase 2), [keyed-state-tool-access.md](keyed-state-tool-access.md) (increment 2)

Phases 1 + increment 2 gave us an in-graph keyed `state` channel (`output_key`,
`{key}` templating, tool read/write) — but it lives only for one invocation. This
phase makes session-scoped state **survive across turns** of a conversation, so a
value a tool or `output_key` wrote in turn 1 is readable in turn 2.

In-graph state ↔ `Conversation.session_state`, through a **governed** store
method. `user:`/`app:` scopes remain phase 3.

---

## Background — the gap

- The `state` channel (`ApxState`, #240) is seeded only with `messages`; nothing
  connects it to persisted state. `LangGraphExecutor.run_turn` only sees messages.
- `Conversation.session_state` is a real `dict[str, Any]` field that backends
  (`_conversation_delta.py`, `_conversation_lakebase.py`) **load** from a JSON
  column on read — but there is no write-back: `set_session_state` was stripped
  when `_conversation.py` was mirrored as a subset of upstream `omniagents`.
- The turn orchestration that has BOTH the `Conversation` and the graph
  invocation is the MLflow AgentServer adapters: `_chat_agent.py` and
  `_responses_agent.py` (`_load_or_create_conversation(...)` → `graph.invoke(...)`
  → append items). That is the seam this phase wires.

## Design

### 1. The governed write surface — `set_session_state` on `ConversationStore`

Re-add the abstract method (decision: restore it on the mirrored ABC, per the
parent design's stated intent — accepting that it will need reconciling when the
upstream `omniagents` import lands; note it in the module docstring's stripped
list):

```python
@abstractmethod
def set_session_state(self, conversation_id: str, session_state: dict[str, Any]) -> None:
    """Overwrite the conversation's persisted session_state (full replacement)."""
```

Implement in all three backends:
- **In-memory** (`_conversation.py`) — replace the stored `Conversation`'s
  `session_state` (via `dataclasses.replace` or an in-place dict swap, matching
  how other mutators update the in-memory row).
- **Delta** (`_conversation_delta.py`) — `UPDATE <table> SET session_state =
  :json WHERE id = :cid`, `json.dumps(session_state)`.
- **Lakebase** (`_conversation_lakebase.py`) — the same UPDATE via SQLAlchemy.

**Governed = explicit + audited.** It is a real store method (not an implicit
side effect), and each call logs one line (`conversation_id`, key count) — the
same audit posture the repo applies to other governed-surface writes. No new
audit table (YAGNI).

### 2. Seed on the way in

In both adapters, the turn already loads the conversation then invokes the graph
with messages only. Seed the state channel from persisted state:

```python
graph.invoke({"messages": lc_input, "state": conv.session_state})
```

So `{key}` templating and stateful-tool reads observe prior-turn values. The
sessionless / degraded path (no conversation) seeds `{}` — unchanged behavior.

### 3. Persist on the way out

After the invocation:

```python
final = result.get("state") or {}
persisted = {k: v for k, v in final.items() if not k.startswith("temp:")}
try:
    store.set_session_state(conv_id, persisted)
except Exception:
    logger.warning("session_state persist degraded for %s", conv_id, exc_info=True)
```

**Full overwrite is correct.** Because we seeded `conv.session_state` into the
channel and #240's shallow-merge reducer applied this turn's deltas on top,
`final` already equals *prior session_state + this turn's changes*. Keys the turn
didn't touch survive; `temp:` scratch never persists. No separate read-merge.

**Never fatal.** The response has already streamed by persist time; a persist
failure (including a non-JSON-serializable value, see §5) logs and degrades —
it does not crash the turn. Mirrors the existing "degraded to sessionless"
handling in `_load_or_create_conversation`.

Persist only when there is a conversation (skip the sessionless path).

### 4. The `temp:` scope

The only scope prefix this phase introduces. `temp:foo` is readable and writable
in-graph for the duration of the turn (it lives in the `state` channel like any
key) but is stripped before persistence — an author's "scratch, don't store this"
escape hatch. `user:` / `app:` scopes (cross-session, cross-user) stay phase 3.

### 5. Constraints / edges

- **JSON-serializable.** `session_state` persists to a JSON column. A
  `json.dumps` failure inside `set_session_state` logs and skips the write
  (degraded, not fatal) rather than crashing the turn. Document that persisted
  state values must be JSON-serializable.
- **Backwards compatible.** Graphs invoked without a seeded `state` still work
  (`state` defaults `{}`); turns with no conversation skip persistence; existing
  message semantics and `result["messages"]` are unchanged.
- **Both adapters.** `_chat_agent.py` and `_responses_agent.py` both get the
  seed + persist. (`_ui_probe.py` reads conversations but does not run turns — out
  of scope; confirm during implementation.)

## Testing

- **Store unit:** `set_session_state` write→read-back on the in-memory store;
  the Delta/Lakebase UPDATE construction.
- **`temp:`-strip unit:** the persisted dict excludes `temp:`-prefixed keys and
  keeps the rest.
- **Round-trip integration** (no LLM): drive `_chat_agent` with a fake graph that
  returns a `state` delta; assert (a) the graph was invoked with `state` seeded
  from `conv.session_state`, and (b) `set_session_state` was called with the
  `temp:`-stripped final state.
- **Degradation:** a `set_session_state` that raises does not fail the turn.
- **Live smoke:** two turns on a real model where turn 2 reads a key a tool wrote
  in turn 1.

## Out of scope

- `user:` / `app:` scope prefixes and their stores — phase 3.
- Per-key persistence policy beyond `temp:` (e.g. TTLs, size caps).
- Seeding/persisting on the bare `LangGraphExecutor` path (it has no conversation
  handle; the served adapters are the persistence surface).

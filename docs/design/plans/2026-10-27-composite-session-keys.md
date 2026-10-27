# Composite session keys + halt statuses — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A durable composite (`SequentialAgent` root) can persist allow-listed state keys across turns of a session in the managed Session Store, stop early when a step reports a halt status, and still accept typed `apx_data_parts` at its first typed step.

**Architecture:** There's no graph checkpointer. `execute` loads a per-session snapshot of the `session_keys` from the managed Session Store (SDK `SessionStoreClient`, under a thread id derived from `session_thread_config`), seeds them into the graph's `state` channel, and appends a new snapshot when a turn completes. The sequence compiler adds a conditional edge after each step: it routes to `END` when the step's `output_status_path` value is one of `halt_statuses`. When a typed data part arrives, a conditional START edge jumps to the first typed step.

**Tech Stack:** Python 3.11+, LangGraph `StateGraph`, `databricks_agentkit` (`SessionStoreClient`, `DurableAgentServer`), pytest.

**Spec:** `docs/design/composite-session-keys.md`

## Global Constraints

- `session_keys` are unprefixed state keys; `user:` and `app:` are rejected (same rule as `persistable_state`).
- `session_keys` on a root `LlmAgent` is an error (it has the full checkpointer).
- A composite with `session_keys` requires the App's managed store (`AGENT_SESSION_STORE`) and fails at compile without it.
- Save only on a turn whose status is `completed`, whether or not it halted. Failed or interrupted turns save nothing. Keys not on the list are never written.
- Snapshot item shape: `{"type": "apx_session_state", "state": {...}}`. Compact after 20 items (`clear_items`, then append the latest).
- State session id: the SDK's `_session_id(thread_id, "apx-state")`, where `thread_id = session_thread_config(agent_name, session_id, user_id)["configurable"]["thread_id"]`.
- Composites still refuse a full checkpointer, recovery, and approvals/interrupts.
- A halt is a normal completed turn, not an escalation. The halting step's assistant message is the answer.
- Repo lint: no `.get(k, "")`, no `x or ""`, no invented env defaults, no `object` annotations, no `tuple[...]` returns, no skipped tests.

## Review Focus

- **A typed caller to an intake-first sequence:** `apx_data_parts` must reach the first typed step and skip the untyped intake. A regression breaks every API and evidence caller. Pinned in Task 5.
- **Stale keys:** a non-listed `output_key` from turn 1 (e.g. `market_signal`) must be absent at the start of turn 2. Pinned in Task 4.
- **Cross-user state:** the same `session_id` from two different users must not share state. Pinned in Task 4.
- **A failed turn:** an exception mid-sequence must not save a half-updated snapshot. Pinned in Task 4.
- **A non-halt status:** `status == "ready"` must continue to the next step. Pinned in Task 2.

---

## File Structure

- Modify `python/src/apx_agent/_agents.py`: `SequentialAgent.__init__` takes `session_keys` and `halt_statuses` and validates them.
- Modify `python/src/apx_agent/_compile.py`: `_compile_sequential_agent` adds the halt edge and the typed-entry START route.
- Create `python/src/apx_agent/_composite_session.py`: `load_session_state` and `save_session_state` over a `SessionStoreClient` (one responsibility: snapshot I/O).
- Modify `python/src/apx_agent/_graph_turn.py`: `GraphTurn.state` (the final `state` channel).
- Modify `python/src/apx_agent/_runtime_targets.py`: keep the managed store for a composite that declares `session_keys`; still refuse a checkpointer.
- Modify `python/src/apx_agent/_durable_agent.py`: build the client, load in `execute`, save on completion, and route typed parts to the first typed step.
- Tests:
  - `python/tests/test_composite_session_keys.py`: new; unit and integration tests for all of the above.
  - `python/tests/test_runtime_targets.py`: update the composite-store expectations.
- Docs:
  - `docs/running/runtime-targets.md` (durable composites section);
  - `docs/design/managed-state-contract-map.md` (new contract row);
  - the spec's status line, set to implemented;
  - `CHANGELOG.md`.

---

### Task 1: `SequentialAgent(session_keys, halt_statuses)` declaration

**Files:**
- Modify: `python/src/apx_agent/_agents.py` (`SequentialAgent.__init__`, ~line 937)
- Test: `python/tests/test_composite_session_keys.py` (create)

**Interfaces:**
- Produces: `SequentialAgent._session_keys: tuple[str, ...]` (default `()`) and `SequentialAgent._halt_statuses: frozenset[str]` (default empty).

- [ ] **Step 1: Write the failing tests**

```python
"""Session state and halt statuses for durable composites (docs/design/composite-session-keys.md)."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from apx_agent import Agent, SequentialAgent, compile_to_langgraph
from apx_agent import _compile


def test_sequence_declares_session_keys_and_halt_statuses():
    seq = SequentialAgent([Agent(name="a")], session_keys=("intake",), halt_statuses=("needs_input",))
    assert seq._session_keys == ("intake",)
    assert seq._halt_statuses == frozenset({"needs_input"})
    assert SequentialAgent([Agent()])._session_keys == () and SequentialAgent([Agent()])._halt_statuses == frozenset()


@pytest.mark.parametrize("keys", [("user:pref",), ("app:flag",), ("",), ("a", "a")])
def test_session_keys_reject_scoped_blank_or_duplicate(keys):
    with pytest.raises(ValueError, match="session_keys"):
        SequentialAgent([Agent()], session_keys=keys)


def test_halt_statuses_reject_blank():
    with pytest.raises(ValueError, match="halt_statuses"):
        SequentialAgent([Agent()], halt_statuses=("",))
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'session_keys'`

- [ ] **Step 3: Implement**

In `SequentialAgent.__init__`, add the parameters after `on_failure`:

```python
        session_keys: tuple[str, ...] = (),
        halt_statuses: tuple[str, ...] = (),
```

and after `self._on_failure = on_failure`:

```python
        # Allow-listed state persisted across turns of a durable session (composite-session-keys.md).
        if any(not k or ":" in k for k in session_keys) or len(set(session_keys)) != len(session_keys):
            raise ValueError("session_keys must be distinct unprefixed state keys (no user:/app: scope)")
        self._session_keys = tuple(session_keys)
        if any(not s for s in halt_statuses):
            raise ValueError("halt_statuses must be non-empty status values")
        self._halt_statuses = frozenset(halt_statuses)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agents.py python/tests/test_composite_session_keys.py
git commit --quiet --message "feat: SequentialAgent declares session_keys and halt_statuses"
```

---

### Task 2: Halt gate in the compiled sequence

**Files:**
- Modify: `python/src/apx_agent/_compile.py` (`_compile_sequential_agent`, the edge loop at ~line 632)
- Test: `python/tests/test_composite_session_keys.py`

**Interfaces:**
- Consumes: `SequentialAgent._halt_statuses`, and the leaf's `_output_key` / `_output_status_path`.
- Produces: the compiled sequence routes `src → END` when `state["state"][src_output_key]` at `src_output_status_path` is in `_halt_statuses`.

- [ ] **Step 1: Write the failing tests**

```python
class Intake(BaseModel):
    status: str
    question: str | None = None


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch, replies):
    model = _Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)


def _intake_sequence(seen):
    return SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="downstream", before_agent_callback=seen.append),
    ], name="pipeline", halt_statuses=("needs_input",))


def test_halt_status_ends_the_sequence_with_the_steps_message(monkeypatch):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    graph = compile_to_langgraph(_intake_sequence(seen), ws=MagicMock(), model="fake")
    result = graph.invoke({"messages": [HumanMessage(content="price the savings product")]})
    assert seen == []  # the downstream step never ran
    assert result["state"]["intake"] == {"status": "needs_input", "question": "Which window?"}
    assert "Which window?" in result["messages"][-1].content


def test_non_halt_status_continues(monkeypatch):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}'), AIMessage(content="done")])
    graph = compile_to_langgraph(_intake_sequence(seen), ws=MagicMock(), model="fake")
    result = graph.invoke({"messages": [HumanMessage(content="price the savings product")]})
    assert len(seen) == 1 and result["messages"][-1].content == "done"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider -k halt`
Expected: FAIL in `test_halt_status_ends...`: `seen == []` fails because the downstream step ran.

- [ ] **Step 3: Implement**

In `_compile_sequential_agent`, before the edge loop, build a status reader:

```python
    def _halted(sub: BaseAgent) -> Callable[[dict[str, Any]], bool] | None:
        key, path = getattr(sub, "_output_key", None), getattr(sub, "_output_status_path", None)
        if not agent._halt_statuses or not key or not path:
            return None

        def check(state: dict[str, Any]) -> bool:
            value: Any = state.get("state", {}).get(key)
            for part in path:
                value = value.get(part) if isinstance(value, dict) else None
            return isinstance(value, str) and value in agent._halt_statuses
        return check
```

Then replace the per-pair edge from `src` with:

```python
        halted = _halted(agent._agents[i])
        if protected or halted is not None:
            def route(state: dict[str, Any], halted: Any = halted) -> str:
                if protected and state.get("_apx_escalation") is not None:
                    return "stop"
                return "stop" if halted is not None and halted(state) else "next"
            graph.add_conditional_edges(src, route, {"stop": END, "next": cont})
        else:
            graph.add_edge(src, cont)
```

(`i` is the index of `src` in `node_names`, so the loop must enumerate the step objects too; `zip(node_names, node_names[1:])` already yields `i`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py tests/test_sequential_escalation_reality_ctk.py -q -p no:cacheprovider`
Expected: PASS (escalation behavior unchanged)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_compile.py python/tests/test_composite_session_keys.py
git commit --quiet --message "feat: a sequence halts after a step whose status is in halt_statuses"
```

---

### Task 3: Session state snapshot I/O

**Files:**
- Create: `python/src/apx_agent/_composite_session.py`
- Test: `python/tests/test_composite_session_keys.py`

**Interfaces:**
- Produces:
  - `load_session_state(client: SessionStoreClient, thread_id: str, keys: tuple[str, ...]) -> dict[str, Any]`
  - `save_session_state(client: SessionStoreClient, thread_id: str, keys: tuple[str, ...], state: dict[str, Any]) -> None`
  - `MAX_SNAPSHOTS = 20`

- [ ] **Step 1: Write the failing tests**

```python
class _FakeSessions:
    """Stands in for the SDK SessionStoreClient: sessions keyed by id, items appended in order."""

    def __init__(self):
        self.items: dict[str, list[Any]] = {}

    def get_session(self, *, session_id):
        from databricks.sdk.errors import NotFound
        if session_id not in self.items:
            raise NotFound("no session")
        return MagicMock(session_id=session_id)

    def create_session(self, *, actor_id, session_id=None, metadata=None):
        self.items[session_id] = []
        return MagicMock(session_id=session_id)

    def append_items(self, session, *, items):
        self.items[session.session_id].extend(items)

    def list_items(self, session, *, order_by=None):
        return iter(MagicMock(data=d) for d in self.items[session.session_id])

    def clear_items(self, session):
        self.items[session.session_id] = []


def test_snapshot_round_trip_keeps_only_listed_keys():
    from apx_agent._composite_session import load_session_state, save_session_state
    store = _FakeSessions()
    assert load_session_state(store, "t1", ("intake",)) == {}
    save_session_state(store, "t1", ("intake",), {"intake": {"slots": {"bank": "acme"}}, "market": {"rows": 3}})
    assert load_session_state(store, "t1", ("intake",)) == {"intake": {"slots": {"bank": "acme"}}}
    [item] = next(iter(store.items.values()))
    assert item == {"type": "apx_session_state", "state": {"intake": {"slots": {"bank": "acme"}}}}


def test_snapshots_compact_after_the_cap():
    from apx_agent._composite_session import MAX_SNAPSHOTS, load_session_state, save_session_state
    store = _FakeSessions()
    for n in range(MAX_SNAPSHOTS + 3):
        save_session_state(store, "t1", ("intake",), {"intake": {"n": n}})
    assert len(next(iter(store.items.values()))) <= MAX_SNAPSHOTS
    assert load_session_state(store, "t1", ("intake",)) == {"intake": {"n": MAX_SNAPSHOTS + 2}}


def test_threads_are_isolated():
    from apx_agent._composite_session import load_session_state, save_session_state
    store = _FakeSessions()
    save_session_state(store, "user-a", ("intake",), {"intake": {"who": "a"}})
    assert load_session_state(store, "user-b", ("intake",)) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider -k "snapshot or threads"`
Expected: FAIL with `ModuleNotFoundError: No module named 'apx_agent._composite_session'`

- [ ] **Step 3: Implement** `python/src/apx_agent/_composite_session.py`

```python
"""Allow-listed session state for durable composites (docs/design/composite-session-keys.md).

One managed Session per agent/user/session thread (suffix "apx-state"); each completed turn
appends a snapshot of the allow-listed keys. No graph checkpointer is involved."""

from __future__ import annotations

from typing import Any

MAX_SNAPSHOTS = 20
_ITEM_TYPE = "apx_session_state"


def _session(client: Any, thread_id: str) -> Any:
    from databricks.sdk.errors import NotFound
    from databricks_agentkit.langgraph.session_store import _session_id

    session_id = _session_id(thread_id, "apx-state")
    try:
        return client.get_session(session_id=session_id)
    except NotFound:
        return client.create_session(actor_id=thread_id, session_id=session_id,
                                     metadata={"apx_state_thread_id": thread_id})


def _snapshots(client: Any, session: Any) -> list[dict[str, Any]]:
    return [i.data["state"] for i in client.list_items(session)
            if isinstance(i.data, dict) and i.data.get("type") == _ITEM_TYPE and isinstance(i.data.get("state"), dict)]


def load_session_state(client: Any, thread_id: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """The latest snapshot's allow-listed keys, or {} for a new session."""
    snapshots = _snapshots(client, _session(client, thread_id))
    return {k: v for k, v in snapshots[-1].items() if k in keys} if snapshots else {}


def save_session_state(client: Any, thread_id: str, keys: tuple[str, ...], state: dict[str, Any]) -> None:
    """Append a snapshot of the allow-listed keys; compact past MAX_SNAPSHOTS."""
    session = _session(client, thread_id)
    item = {"type": _ITEM_TYPE, "state": {k: state[k] for k in keys if k in state}}
    if len(_snapshots(client, session)) >= MAX_SNAPSHOTS:
        client.clear_items(session)  # ponytail: last-write-wins; the SDK serializes per session
    client.append_items(session, items=[item])
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_composite_session.py python/tests/test_composite_session_keys.py
git commit --quiet --message "feat: snapshot I/O for allow-listed composite session state"
```

---

### Task 4: Durable host loads and saves session keys

**Files:**
- Modify:
  - `python/src/apx_agent/_graph_turn.py` (`GraphTurn` gains `state: dict[str, Any]`; `run_graph_turn` returns `result.get("state", {})`; the paused branch returns `{}`)
  - `python/src/apx_agent/_runtime_targets.py` (`inspect_target` ~line 60-90)
  - `python/src/apx_agent/_durable_agent.py` (`compile_durable_handlers` ~line 150-170, `execute` ~line 190-270, and the `session_store` wiring ~line 360)
- Test: `python/tests/test_composite_session_keys.py`, plus `python/tests/test_runtime_targets.py:281-305`

**Interfaces:**
- Consumes: `load_session_state`, `save_session_state` (Task 3); `SequentialAgent._session_keys` (Task 1).
- Produces: a composite root with `session_keys` plus `session_store=<name>` compiles on `durable_agent_server`. Each completed turn persists the keys under `session_thread_config(...)` for that agent/user/session.

- [ ] **Step 1: Write the failing tests**

```python
def _store(monkeypatch):
    store = _FakeSessions()
    import apx_agent._durable_agent as durable
    monkeypatch.setattr(durable, "_session_state_client", lambda name, ws: store)
    return store


def _turn(app_client, body):
    from tests.test_native_invocation_contract import headers
    r = app_client.post("/api/invocations", json=body, headers=headers())
    assert r.status_code in (200, 202), r.text
    return app_client.get(f"/api/invocations/{body['id']}", headers=headers()).json()


def test_session_keys_persist_across_turns_and_nothing_else(execution, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent
    from tests.test_runtime_targets import _write_native_pyproject
    import uuid

    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    store = _store(monkeypatch)
    seen_state = []
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",),
              before_agent_callback=lambda ctx: seen_state.append(dict(ctx.state))),
        Agent(name="market", output_key="market"),
    ], name="pipeline", session_keys=("intake",), halt_statuses=("needs_input",))
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}'), AIMessage(content="m1"),
                         AIMessage(content='{"status": "needs_input", "question": "q"}')])
    app = compile_agent(root, target="durable_agent_server", model="fake", service_ws=execution.ws, session_store="s")
    sid = str(uuid.uuid4())
    with TestClient(app) as client:
        for text in ("turn one", "turn two"):
            _turn(client, {"id": str(uuid.uuid4()), "session_id": sid, "input": {"messages": [{"role": "user", "content": text}]}})
    assert "intake" not in seen_state[0]                           # turn 1 starts empty
    assert seen_state[1]["intake"] == {"status": "ready", "question": None}  # turn 2 sees turn 1's intake
    assert "market" not in seen_state[1]                           # non-listed key never persists
    assert all(set(i["state"]) <= {"intake"} for items in store.items.values() for i in items)


def test_failed_turn_saves_nothing(execution, monkeypatch, tmp_path):
    """A turn that raises mid-sequence leaves the stored snapshot untouched."""
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent
    from tests.test_runtime_targets import _write_native_pyproject
    import uuid

    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    store = _store(monkeypatch)

    def boom(ctx):
        raise RuntimeError("downstream failed")
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="market", before_agent_callback=boom),
    ], name="pipeline", session_keys=("intake",))
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}')])
    app = compile_agent(root, target="durable_agent_server", model="fake", service_ws=execution.ws, session_store="s")
    with TestClient(app) as client:
        state = _turn(client, {"id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
                               "input": {"messages": [{"role": "user", "content": "x"}]}})
    assert state["status"] == "failed"
    assert all(items == [] for items in store.items.values())


def test_users_do_not_share_session_state():
    from apx_agent._durable_agent import session_thread_config
    a = session_thread_config("pipeline", "same", "user-a")["configurable"]["thread_id"]
    b = session_thread_config("pipeline", "same", "user-b")["configurable"]["thread_id"]
    assert a != b


def test_composite_session_keys_require_a_store(execution):
    from apx_agent import compile_agent
    root = SequentialAgent([Agent(name="a")], session_keys=("intake",))
    with pytest.raises(ValueError, match="session_keys.*session store"):
        compile_agent(root, target="durable_agent_server", model="fake", service_ws=execution.ws)


def test_llm_root_rejects_session_keys_and_composite_still_rejects_checkpointer(execution):
    from langgraph.checkpoint.memory import InMemorySaver
    from apx_agent import compile_agent
    with pytest.raises(NotImplementedError):
        compile_agent(SequentialAgent([Agent(name="a")], session_keys=("k",)), target="durable_agent_server",
                      model="fake", service_ws=execution.ws, checkpointer=InMemorySaver())
```

(`execution` comes from `tests.test_runtime_targets`; import it as other test modules do: `from tests.test_runtime_targets import execution  # noqa: F401`. `before_agent_callback` receives a context with `.state`; if its signature differs, adapt the lambda to whatever `test_sequential_escalation_reality_ctk.py` passes, and record a ledger ruling.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider -k "persist or failed_turn or require_a_store or rejects_session"`
Expected: FAIL. `compile_agent` currently drops the store for composites, or raises `session_store currently requires LlmAgent`.

- [ ] **Step 3: Implement**

`_graph_turn.py`: add `state: dict[str, Any]` to `GraphTurn`. Return `GraphTurn(messages=new_messages, approval_required=None, resumed=..., state=dict(result.get("state", {})))`, and `state={}` in the paused branch.

`_runtime_targets.py` `inspect_target`: before `effective_session = session if isinstance(agent, LlmAgent) else None`, add

```python
    if getattr(agent, "_session_keys", ()) and session_store is None:
        raise ValueError("session_keys on a durable composite require a session store (AGENT_SESSION_STORE)")
```

and change the `session_store currently requires LlmAgent` check so it doesn't raise when `getattr(agent, "_session_keys", ())` is set.

`_durable_agent.py`:

```python
def _session_state_client(store_name: str, service_ws: Any) -> Any:
    """The SDK client bound to the App's managed store (patched in tests)."""
    from databricks_agentkit.runtime.session_store_client import SessionStoreClient
    return SessionStoreClient(service_ws).set_session_store(store_name)
```

In `compile_durable_handlers`, when `getattr(agent, "_session_keys", ())` is set and a `session_store` name is given, bind `state_client = _session_state_client(session_store, service_ws)` and do **not** build a `DatabricksSessionStoreSaver` (`checkpointer` stays `None`).

In `execute`, after `domain_input` is built and before `compile_to_langgraph`:

```python
        keys = getattr(agent, "_session_keys", ())
        state_thread = None
        if keys:
            session = context.session_id
            if not isinstance(session, str) or not session:
                raise ValueError("session_keys require a non-empty session_id")
            state_thread = session_thread_config(getattr(agent, "_name", None), session,
                                                 headers.user_id if headers is not None else None)["configurable"]["thread_id"]
            domain_input.update(load_session_state(state_client, state_thread, keys))
```

After `turn = run_graph_turn(...)`, and only on the success path (no approval, no exception):

```python
        if state_thread is not None and turn.approval_required is None:
            save_session_state(state_client, state_thread, keys, turn.state)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py tests/test_runtime_targets.py -q -p no:cacheprovider`
Expected: PASS. Update `test_runtime_targets.py:281-305` only where it asserted that a composite *with* `session_keys` drops the store. Composites without `session_keys` still drop it and still error on an explicit store.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_graph_turn.py python/src/apx_agent/_runtime_targets.py python/src/apx_agent/_durable_agent.py python/tests/
git commit --quiet --message "feat: durable composites persist session_keys in the managed Session Store"
```

---

### Task 5: Typed callers enter at the first typed step

**Files:**
- Modify:
  - `python/src/apx_agent/_durable_agent.py` (`execute`: choose the typed leaf for `apx_data_parts`)
  - `python/src/apx_agent/_compile.py` (`_compile_sequential_agent`: a conditional START edge on `state["_apx_typed_entry"]`)
- Test: `python/tests/test_composite_session_keys.py`

**Interfaces:**
- Produces:
  - `typed_entry_step(agent: SequentialAgent) -> tuple[int, BaseAgent] | None`: the first top-level step whose input leaf declares `_input_schema`, when that step isn't index 0.
  - `domain_input["_apx_typed_entry"] = <step name>`, which the root sequence uses to route START to that step.

- [ ] **Step 1: Write the failing test**

```python
class Request(BaseModel):
    bank: str


def test_typed_part_skips_an_untyped_intake(execution, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent
    from apx_agent._native_input import native_data_part
    from tests.test_runtime_targets import _write_native_pyproject
    import uuid

    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    _store(monkeypatch)
    seen = []
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", before_agent_callback=seen.append),
        Agent(name="intelligence", input_schema=Request, input_key="request"),
    ], name="pipeline", session_keys=("intake",))
    _model(monkeypatch, [AIMessage(content="ok")])
    app = compile_agent(root, target="durable_agent_server", model="fake", service_ws=execution.ws, session_store="s")
    part = native_data_part(Request, {"bank": "acme"})
    with TestClient(app) as client:
        state = _turn(client, {"id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
                               "input": {"messages": [], "apx_data_parts": [part]}})
    assert state["status"] == "completed"
    assert seen == []  # the untyped intake was skipped
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py -q -p no:cacheprovider -k typed_part`
Expected: FAIL. `execute` raises "Native typed input requires its declared schema" (the input leaf is the untyped intake).

- [ ] **Step 3: Implement**

`_durable_agent.py`, where `leaf = input_leaf(agent)` is computed:

```python
        leaf = input_leaf(agent)
        entry = None
        if value.get("apx_data_parts") is not None and getattr(leaf, "_input_schema", None) is None:
            from ._agents import SequentialAgent
            if isinstance(agent, SequentialAgent):
                for i, step in enumerate(agent._agents[1:], start=1):
                    typed = input_leaf(step)
                    if getattr(typed, "_input_schema", None) is not None:
                        leaf, entry = typed, getattr(step, "_name", None) or f"step_{i}"
                        break
        schema = getattr(leaf, "_input_schema", None)
```

and after `domain_input` is built: `if entry is not None: domain_input["_apx_typed_entry"] = entry`.

`_compile.py` `_compile_sequential_agent`: when the root (no `ctx.step_path`) has any later step with a typed input leaf, replace `graph.add_edge(START, node_names[0])` (and the `init_name → node_names[0]` edge when protected) with

```python
    typed_names = {n for n, s in zip(node_names, agent._agents) if getattr(input_leaf(s), "_input_schema", None) is not None}
    if typed_names and not ctx.step_path:
        first = init_name if protected and not ctx.step_path else START
        graph.add_conditional_edges(
            first, lambda state: state.get("state", {}).get("_apx_typed_entry") or node_names[0],
            {n: n for n in node_names})
```

(import `input_leaf` from `._native_input`; keep the existing unconditional edges when `typed_names` is empty).

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_composite_session_keys.py tests/test_native_invocation_contract.py tests/test_runtime_targets.py -q -p no:cacheprovider`
Expected: PASS. A typed-root sequence (first leaf typed) behaves exactly as before.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_durable_agent.py python/src/apx_agent/_compile.py python/tests/test_composite_session_keys.py
git commit --quiet --message "feat: typed data parts enter a sequence at its first typed step"
```

---

### Task 6: Docs, contract map, changelog, gate

**Files:**
- Modify:
  - `docs/running/runtime-targets.md` (durable composites section, ~line 647);
  - `docs/design/managed-state-contract-map.md` (add the `apx_session_state` row next to `set_session_state` ~line 257);
  - `docs/design/composite-session-keys.md` (status set to implemented);
  - `CHANGELOG.md` (`[Unreleased]` → `### Added`).

- [ ] **Step 1:** Write the docs. Show the `SequentialAgent(..., session_keys=..., halt_statuses=...)` example. State which keys persist and when. Explain the typed-entry rule. Say that a checkpointer, recovery and approvals are still refused for composites.
- [ ] **Step 2:** Run the gate:
  - `make check`;
  - `pre-commit run --files $(git diff --name-only origin/main)`;
  - `cd python && uv run pyright`;
  - the credential-free run: `env $(env | grep -o '^DATABRICKS_[A-Z_]*' | sed 's/^/-u /') HOME=/tmp/nohome PATH="$NP" .venv/bin/python -m pytest --numprocesses auto`, where `NP` is `PATH` without homebrew and `/usr/local/bin`.

  Expected: all green, except the 2 known node-only `test_optimize_prompts_route` failures.
- [ ] **Step 3:** Commit: `git commit --quiet --message "docs: session_keys and halt_statuses on durable composites"`

---

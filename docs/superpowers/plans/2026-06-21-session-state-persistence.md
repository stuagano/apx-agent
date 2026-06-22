# Session-scoped state persistence (G3 phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The in-graph keyed `state` channel survives across turns of a conversation — seeded from `Conversation.session_state` on the way in, persisted back via a governed `set_session_state` on the way out, with `temp:`-prefixed keys kept in-turn only.

**Architecture:** Re-add `set_session_state` to the `ConversationStore` ABC + all three backends (in-memory, Delta, Lakebase). A small `_session_state.py` helper strips `temp:` keys and does the guarded write. The two MLflow AgentServer adapters (`_chat_agent.py`, `_responses_agent.py`) seed the `state` channel from the conversation's `session_state` and persist the final state after each turn — across all four invoke sites (each adapter's non-streaming + streaming handler).

**Tech Stack:** Python 3.12, langgraph (`ApxState`/`state_schema()` from #240), the existing `ConversationStore` backends. Tests via `pytest` (`uv run pytest` from `python/`).

## Global Constraints

- Run tests from `python/`: `cd python && uv run pytest …`. The repo root `.venv` shadows `src/`.
- Full-package pyright must stay clean: `cd python && uv run pyright`.
- Pre-commit guards REJECT: `.get(key, "")` (empty-STRING default only — `.get(key)` with no default is fine), `x or ""`, `getattr(obj, "literal", default)`, `str = ""` defaults, `: object` annotations, `tuple[X, Y, ...]` returns. Do not introduce these.
- Do NOT touch `python/uv.lock`; if `git status` shows it modified after `uv run`, `git checkout python/uv.lock` before committing.
- Commit messages end with the trailer block:
  `Co-authored-by: Isaac` then `Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3`
- In-graph only this phase. The only scope prefix introduced is `temp:` (never persisted). `user:`/`app:` scopes are out of scope (phase 3).
- Persisted `session_state` must be JSON-serializable; a serialization failure logs and degrades (never crashes the turn).
- Full-overwrite persist semantics: the value passed to `set_session_state` is the final `state` (already prior-session-state + this-turn deltas, merged by #240's reducer) minus `temp:` keys. No read-merge.
- Branch: `feat/session-state-persistence` (already created off `main`).

---

### Task 1: `set_session_state` on ABC + in-memory store, and the `_session_state` helper

**Files:**
- Modify: `python/src/apx_agent/_conversation.py` (ABC abstract method + `InMemoryConversationStore` impl + docstring stripped-list note)
- Create: `python/src/apx_agent/_session_state.py`
- Test: `python/tests/test_session_state_store.py`

**Interfaces:**
- Produces:
  - `ConversationStore.set_session_state(self, conversation_id: str, session_state: dict[str, Any]) -> None` (abstract) — overwrites the row's persisted session_state (full replacement). No-op if the conversation does not exist (matches a SQL `UPDATE` of zero rows).
  - `persistable_state(state: dict[str, Any] | None) -> dict[str, Any]` in `_session_state.py` — returns `state` with `temp:`-prefixed keys removed.
  - `persist_session_state(store: Any, conversation_id: str | None, final_state: dict[str, Any] | None) -> None` — guarded, never-fatal write-back: no-ops on missing store/conv_id, strips `temp:`, calls `set_session_state`, logs+swallows on any exception.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_session_state_store.py
import pytest

from apx_agent._conversation import InMemoryConversationStore
from apx_agent._session_state import persistable_state, persist_session_state


def _store_with_conv(cid: str = "c1") -> InMemoryConversationStore:
    store = InMemoryConversationStore("memory://")
    store.create_conversation(id=cid)
    return store


def test_set_and_read_back_session_state():
    store = _store_with_conv()
    store.set_session_state("c1", {"account_id": "ACME-42", "n": 3})
    conv = store.get_conversation("c1")
    assert conv is not None
    assert conv.session_state == {"account_id": "ACME-42", "n": 3}


def test_set_session_state_full_overwrite():
    store = _store_with_conv()
    store.set_session_state("c1", {"a": 1})
    store.set_session_state("c1", {"b": 2})
    assert store.get_conversation("c1").session_state == {"b": 2}


def test_set_session_state_missing_conv_is_noop():
    store = InMemoryConversationStore("memory://")
    store.set_session_state("nope", {"a": 1})  # must not raise


def test_persistable_state_strips_temp_keys():
    out = persistable_state({"keep": 1, "temp:scratch": 2, "also": 3})
    assert out == {"keep": 1, "also": 3}


def test_persistable_state_handles_none():
    assert persistable_state(None) == {}


def test_persist_session_state_strips_temp_and_writes():
    store = _store_with_conv()
    persist_session_state(store, "c1", {"x": 1, "temp:y": 2})
    assert store.get_conversation("c1").session_state == {"x": 1}


def test_persist_session_state_noops_without_conv_id():
    store = _store_with_conv()
    persist_session_state(store, None, {"x": 1})  # must not raise
    assert store.get_conversation("c1").session_state == {}


def test_persist_session_state_swallows_backend_error():
    class _Boom:
        def set_session_state(self, conversation_id, session_state):
            raise RuntimeError("backend down")

    # Must not raise — degraded, not fatal.
    persist_session_state(_Boom(), "c1", {"x": 1})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_session_state_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'persistable_state'` / `InMemoryConversationStore` has no `set_session_state`.

- [ ] **Step 3a: Add the abstract method to `ConversationStore`**

In `python/src/apx_agent/_conversation.py`, find the `ConversationStore(ABC)` class (search `class ConversationStore`) and add this abstract method next to `update_conversation`:

```python
    @abstractmethod
    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        """Overwrite the conversation's persisted session_state (full replacement).

        ``session_state`` must be JSON-serializable. No-op when the conversation
        does not exist (parity with a SQL ``UPDATE`` matching zero rows).
        """
        ...
```

Also update the module docstring: in the "Agent-plane methods stripped from ConversationStore" list (search `set_session_state,` near the top), remove `set_session_state` from that list and add a one-line note, e.g. append after the list:
`(set_session_state was restored for G3 session-state persistence — see docs/design/session-state-persistence.md.)`

- [ ] **Step 3b: Implement on `InMemoryConversationStore`**

The in-memory store holds `self._conversations: dict[str, Conversation]` under `self._lock` and mutates rows with `replace(...)` + `_now_ms()` (see its `update_conversation`). Add:

```python
    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return  # no-op for unknown conversation (SQL-UPDATE parity)
            self._conversations[conversation_id] = replace(
                conv, session_state=dict(session_state), updated_at=_now_ms()
            )
```

(`replace` and `_now_ms` are already imported/defined in this module — confirm by reading the existing `update_conversation`.)

- [ ] **Step 3c: Create the `_session_state.py` helper**

```python
# python/src/apx_agent/_session_state.py
"""Session-state persistence helpers for the G3 phase-2 turn boundary.

The served adapters seed the in-graph ``state`` channel from a conversation's
``session_state`` and, after the turn, persist the final ``state`` back through
``ConversationStore.set_session_state``. ``temp:``-prefixed keys are scratch —
readable in-graph during the turn but never persisted. See
docs/design/session-state-persistence.md.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "temp:"


def persistable_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``state`` with ``temp:``-scoped keys removed (they live only for
    the turn)."""
    if not state:
        return {}
    return {k: v for k, v in state.items() if not str(k).startswith(_TEMP_PREFIX)}


def persist_session_state(
    store: Any, conversation_id: str | None, final_state: dict[str, Any] | None
) -> None:
    """Governed, never-fatal write-back of session-scoped state.

    No-ops without a store or conversation id. Strips ``temp:`` keys, then calls
    the governed ``set_session_state`` mutator. Any failure (including a
    non-JSON-serializable value surfacing in a backend) is logged and swallowed —
    the response has already been produced, so a persist failure must not crash
    the turn.
    """
    if store is None or conversation_id is None:
        return
    persisted = persistable_state(final_state)
    try:
        store.set_session_state(conversation_id, persisted)
    except Exception:
        logger.warning(
            "session_state persist degraded for %s", conversation_id, exc_info=True
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_session_state_store.py -q`
Expected: PASS (8 passed). Then confirm the package still imports and the abstract method didn't break other stores: `cd python && uv run python -c "import apx_agent; print('ok')"`.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git checkout python/uv.lock 2>/dev/null
git add python/src/apx_agent/_conversation.py python/src/apx_agent/_session_state.py python/tests/test_session_state_store.py
git commit -m "feat: set_session_state on ConversationStore ABC + in-memory + session-state helpers

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 2: `set_session_state` on the Delta and Lakebase backends

**Files:**
- Modify: `python/src/apx_agent/_conversation_delta.py`
- Modify: `python/src/apx_agent/_conversation_lakebase.py`
- Test: `python/tests/test_session_state_backends.py`

**Interfaces:**
- Consumes: the `set_session_state` abstract method from Task 1 (both backends subclass `ConversationStore`, so they now MUST implement it or they can't instantiate).
- Produces: `set_session_state` on `DeltaConversationStore` and `LakebaseConversationStore`, mirroring each backend's existing `update_conversation` SQL idiom.

> Why this task exists: adding an `@abstractmethod` in Task 1 makes both backend classes abstract until they implement it. Read each backend's existing `update_conversation` first and copy its exact idiom (table attr name, escaping/parameter helper, connection handling, whether it stamps `updated_at`).

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_session_state_backends.py
"""Construction-level tests for set_session_state on the SQL backends — assert
the UPDATE statement + params are built correctly without a live warehouse."""
import json
from unittest.mock import MagicMock, patch

import pytest


def test_delta_set_session_state_builds_update(monkeypatch):
    from apx_agent import _conversation_delta as mod

    captured = {}

    def fake_run_sql(ws, sql, warehouse_id=None):
        captured["sql"] = sql

    monkeypatch.setattr(mod, "run_sql", fake_run_sql)
    store = mod.DeltaConversationStore.__new__(mod.DeltaConversationStore)
    store.ws = MagicMock()
    store.warehouse_id = "wh1"
    store._conv_table = "cat.sch.conversations"

    store.set_session_state("c1", {"account_id": "ACME-42"})

    sql = captured["sql"]
    assert "UPDATE cat.sch.conversations" in sql
    assert "session_state" in sql
    assert "c1" in sql
    # the JSON payload is embedded (escaped) in the statement
    assert "ACME-42" in sql


def test_lakebase_set_session_state_builds_update(monkeypatch):
    from apx_agent import _conversation_lakebase as mod

    store = mod.LakebaseConversationStore.__new__(mod.LakebaseConversationStore)
    store._conv_table = "conversations"

    conn = MagicMock()
    engine_ctx = MagicMock()
    engine_ctx.__enter__ = MagicMock(return_value=conn)
    engine_ctx.__exit__ = MagicMock(return_value=False)
    store._engine = MagicMock()
    store._engine.begin = MagicMock(return_value=engine_ctx)

    store.set_session_state("c1", {"account_id": "ACME-42"})

    assert conn.execute.called
    args, kwargs = conn.execute.call_args
    # second positional arg is the params dict
    params = args[1]
    assert params["cid"] == "c1"
    assert json.loads(params["ss"]) == {"account_id": "ACME-42"}
```

> The exact attribute names (`store.ws`, `store.warehouse_id`, `store._conv_table`, `store._engine`) and the lakebase param key names (`:cid`, `:ss`) MUST match what you write in Step 3. Read each backend's `__init__` and `update_conversation` first; if the real attribute names differ (e.g. `self._warehouse_id`), fix BOTH the implementation and this test to match — they are the contract for this task.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_session_state_backends.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'set_session_state'`.

- [ ] **Step 3: Implement on both backends**

In `python/src/apx_agent/_conversation_delta.py`, read `update_conversation` (it builds `f"UPDATE {self._conv_table} SET … WHERE conversation_id = {_sql_str(conversation_id)}"` and calls `run_sql(self.ws, sql, warehouse_id=self.warehouse_id)`; `_sql_str` and `run_sql` are imported there). Add, mirroring that idiom exactly (including `updated_at` if `update_conversation` stamps it):

```python
    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        import json

        sql = (
            f"UPDATE {self._conv_table} "
            f"SET session_state = {_sql_str(json.dumps(session_state))} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
```

In `python/src/apx_agent/_conversation_lakebase.py`, read `update_conversation` (it uses `sa.text(f"UPDATE {self._conv_table} SET … WHERE conversation_id = :cid")` then `with self._engine.begin() as conn: conn.execute(sql, params)`). Add, mirroring it:

```python
    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        import json

        sql = sa.text(
            f"UPDATE {self._conv_table} "
            f"SET session_state = :ss "
            f"WHERE conversation_id = :cid"
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"ss": json.dumps(session_state), "cid": conversation_id})
```

> Adjust `Any` import if needed (both modules likely already `from typing import Any`). If `update_conversation` in either backend also sets `updated_at`, add the same `updated_at` assignment here for consistency, and extend the test to expect it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_session_state_backends.py -q`
Expected: PASS (2 passed). Also confirm both backend classes still instantiate (no remaining abstract methods): `cd python && uv run python -c "from apx_agent._conversation_delta import DeltaConversationStore; from apx_agent._conversation_lakebase import LakebaseConversationStore; print('ok')"`.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git checkout python/uv.lock 2>/dev/null
git add python/src/apx_agent/_conversation_delta.py python/src/apx_agent/_conversation_lakebase.py python/tests/test_session_state_backends.py
git commit -m "feat: set_session_state on Delta + Lakebase conversation stores

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 3: seed + persist in `_chat_agent.py` (predict + predict_stream)

**Files:**
- Modify: `python/src/apx_agent/_chat_agent.py`
- Test: `python/tests/test_chat_agent_session_state.py`

**Interfaces:**
- Consumes: `persist_session_state` (Task 1); `set_session_state` (Tasks 1–2).
- `_chat_agent`'s `_load_or_create_conversation` returns a `Conversation` (has `.session_state`); `conv_id = conv.conversation_id if conv is not None else None`; `self._conversation_store` is the store.
- Produces: both `predict` and `predict_stream` seed the graph `state` channel from `conv.session_state` and persist the final state after the turn.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_chat_agent_session_state.py
"""_chat_agent seeds the state channel from session_state and persists the
temp:-stripped final state. Uses a fake graph — no LLM."""
from unittest.mock import MagicMock, patch

import pytest
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage
from mlflow.types.agent import ChatAgentMessage

from apx_agent import LlmAgent
from apx_agent._chat_agent import chat_agent_for
from apx_agent._conversation import InMemoryConversationStore


class _FakeGraph:
    """Records the invoke input; returns the input messages + an AI message and
    a state delta (mirrors a real compiled graph's invoke result shape)."""

    def __init__(self, delta):
        self.delta = delta
        self.seen_input = None

    def invoke(self, payload):
        self.seen_input = payload
        msgs = list(payload["messages"]) + [AIMessage(content="done")]
        return {"messages": msgs, "state": dict(self.delta)}


def test_predict_seeds_and_persists():
    store = InMemoryConversationStore("memory://")
    store.create_conversation(id="c1")
    store.set_session_state("c1", {"prior": "p"})

    wrapped = chat_agent_for(
        LlmAgent(tools=[], instructions="help"),
        model="any-endpoint",
        conversation_store=store,
    )
    fake = _FakeGraph({"account_id": "ACME-42", "temp:scratch": "x"})

    with patch(
        "apx_agent._defaults._make_workspace_client",
        return_value=MagicMock(name="sp_ws"),
    ), patch(
        "apx_agent._chat_agent.compile_to_langgraph", return_value=fake
    ):
        wrapped.predict(
            messages=[ChatAgentMessage(role="user", content="look up acme", id="u1")],
            custom_inputs={"thread_id": "c1"},
        )

    # seeded from prior session_state
    assert fake.seen_input["state"] == {"prior": "p"}
    # persisted final state, temp: stripped
    assert store.get_conversation("c1").session_state == {"account_id": "ACME-42"}
```

> Construction is the factory `chat_agent_for(agent, *, model=, conversation_store=)` (NOT a direct class) — verified against `tests/test_chat_agent.py`. Messages are `ChatAgentMessage` objects; the conversation-id custom-input key is `thread_id`. The double-patch (`_make_workspace_client` + `compile_to_langgraph`) mirrors the existing chat-agent tests. Add an analogous `predict_stream` test: call `list(wrapped.predict_stream(...))` to drain the generator, then assert the same seed (`fake.seen_input["state"]`) and persisted state. For `predict_stream` the fake graph needs a `.stream(payload, stream_mode=...)` method that yields `("updates", {...})` then `("values", {"messages": [...], "state": delta})` — model it on the verified multi-mode shape.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_chat_agent_session_state.py -q`
Expected: FAIL — seeded `state` is absent (invoke called with only `messages`) and/or session_state not persisted.

- [ ] **Step 3: Wire `predict` and `predict_stream`**

Add the import near the other `from ._…` imports in `_chat_agent.py`:
```python
from ._session_state import persist_session_state
```

In `predict` (search `result = graph.invoke({"messages": lc_input})`), seed the channel:
```python
                    seed_state = conv.session_state if conv is not None else {}
                    result = graph.invoke({"messages": lc_input, "state": seed_state})
```
After the existing `self._persist_conv_turn(...)` call in `predict`, persist session state:
```python
                self._persist_conv_turn(
                    conv_id,
                    input_messages=messages,
                    new_messages=new_messages,
                    model=effective_model,
                )
                persist_session_state(
                    self._conversation_store, conv_id, result.get("state")
                )
```

In `predict_stream`, the loop currently is `for chunk in graph.stream({"messages": lc_input}, stream_mode="updates"):`. Change it to multi-mode so the final state can be captured, keeping the existing text-emission logic for the `updates` chunks:
```python
                seed_state = conv.session_state if conv is not None else {}
                last_values: dict | None = None
                for mode, chunk in graph.stream(
                    {"messages": lc_input, "state": seed_state},
                    stream_mode=["updates", "values"],
                ):
                    if mode == "values":
                        last_values = chunk
                        continue
                    # mode == "updates" — unchanged emission logic below
                    if not isinstance(chunk, dict):
                        continue
                    for _node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        for msg in node_output.get("messages", []) or []:
                            delta = _from_langchain_message(msg, emitted)
                            emitted += 1
                            new_messages.append(delta)
                            yield ChatAgentChunk(delta=delta)
```
After the existing `self._persist_conv_turn(...)` in `predict_stream`, persist:
```python
                persist_session_state(
                    self._conversation_store,
                    conv_id,
                    (last_values or {}).get("state"),
                )
```

> Verified: `graph.stream(stream_mode=["updates", "values"])` yields `(mode, chunk)` tuples; the last `values` chunk's `["state"]` is the full prior+turn state. Keep the rest of each handler (spans, attrs, persist-turn) exactly as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_chat_agent_session_state.py -q`
Expected: PASS. Then run the existing chat-agent suite to confirm no regression: `cd python && uv run pytest tests/ -k chat_agent -q`.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git checkout python/uv.lock 2>/dev/null
git add python/src/apx_agent/_chat_agent.py python/tests/test_chat_agent_session_state.py
git commit -m "feat: chat agent seeds + persists session_state (predict + predict_stream)

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 4: seed + persist in `_responses_agent.py` (non_streaming + streaming)

**Files:**
- Modify: `python/src/apx_agent/_responses_agent.py`
- Test: `python/tests/test_responses_agent_session_state.py`

**Interfaces:**
- Consumes: `persist_session_state` (Task 1).
- `_responses_agent`'s `_load_or_create_conversation(store, custom_inputs, agent_id=...)` returns a `_ConvLoad` (a dataclass with `conversation_id`, `items`, `is_new`) — it does NOT currently carry `session_state`. This task adds it.
- Produces: `_ConvLoad` gains a `session_state: dict[str, Any]` field; both `non_streaming` and `streaming` handlers seed from it and persist the final state.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_responses_agent_session_state.py
import pytest
pytest.importorskip("langgraph")

from apx_agent import _responses_agent
from apx_agent._conversation import InMemoryConversationStore


def test_conv_load_carries_session_state():
    store = InMemoryConversationStore("memory://")
    store.create_conversation(id="c1")
    store.set_session_state("c1", {"prior": "p"})

    load = _responses_agent._load_or_create_conversation(
        store, {"thread_id": "c1"}, agent_id="a"
    )
    assert load is not None
    assert load.session_state == {"prior": "p"}


def test_conv_load_session_state_defaults_empty_for_new():
    store = InMemoryConversationStore("memory://")
    load = _responses_agent._load_or_create_conversation(
        store, {"thread_id": "newconv"}, agent_id="a"
    )
    assert load is not None
    assert load.session_state == {}
```

> Add a fuller round-trip test for the `non_streaming` handler mirroring Task 3's `_FakeGraph` approach IF the responses handler is reachable in a unit test the way `predict` is — read the existing responses-agent tests (`grep tests/ -l responses`) and follow their construction. If the handler is only reachable via the MLflow `@invoke()` decorator and hard to unit-drive, the `_ConvLoad.session_state` tests above plus the integration test in Task 5 are sufficient; note that in your report.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_responses_agent_session_state.py -q`
Expected: FAIL — `_ConvLoad` has no `session_state` attribute.

- [ ] **Step 3: Add `session_state` to `_ConvLoad` and wire both handlers**

In `_responses_agent.py`, add the field to the `_ConvLoad` dataclass (search `class _ConvLoad`):
```python
    session_state: dict[str, Any] = field(default_factory=dict)
```
(Ensure `field` is imported from `dataclasses` and `Any` from `typing` — they likely are; add if missing.)

In `_load_or_create_conversation`, populate it from the fetched conversation. The function does `existing = store.get_conversation(conv_id)` then creates if absent and returns `_ConvLoad(conversation_id=conv_id, items=page.data, is_new=is_new)`. Capture the session_state:
```python
        existing = store.get_conversation(conv_id)
        is_new = existing is None
        if is_new:
            store.create_conversation(id=conv_id, agent_id=agent_id)
        page = store.list_items(conv_id, order="asc", limit=10_000)
        session_state = existing.session_state if existing is not None else {}
        return _ConvLoad(
            conversation_id=conv_id,
            items=page.data,
            is_new=is_new,
            session_state=session_state,
        )
```
> Read the actual body and preserve its existing `is_new` computation — only add the `session_state` capture and pass it through. Don't change the degraded/sessionless return (`None`).

Add the import near the other `from ._…` imports:
```python
from ._session_state import persist_session_state
```

In the `non_streaming` handler (search `result = graph.invoke({"messages": graph_input})`), seed and persist:
```python
                    seed_state = conv.session_state if conv is not None else {}
                    result = graph.invoke({"messages": graph_input, "state": seed_state})
```
After the existing `_persist_conv_turn(...)` call inside `if conv_id is not None:`:
```python
                persist_session_state(_conversation_store, conv_id, result.get("state"))
```

In the `streaming` handler, the graph-stream branch is `for chunk in graph.stream({"messages": graph_input}, stream_mode="updates"):`. Convert to multi-mode, keeping the existing emission logic for `updates`:
```python
                seed_state = conv.session_state if conv is not None else {}
                last_values: dict | None = None
                for mode, chunk in graph.stream(
                    {"messages": graph_input, "state": seed_state},
                    stream_mode=["updates", "values"],
                ):
                    if mode == "values":
                        last_values = chunk
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    for _node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        for lc_msg in node_output.get("messages", []) or []:
                            raw = _langchain_to_output_item(lc_msg, output_index)
                            for item in _flatten_output_items([raw]):
                                output_items.append(item)
                                yield ResponsesAgentStreamEvent(
                                    type="response.output_item.done",
                                    item=item,
                                    output_index=output_index,
                                )
                                output_index += 1
```
After the streaming handler's existing persist/terminal logic, persist session state when there is a conversation:
```python
                if conv_id is not None:
                    persist_session_state(
                        _conversation_store, conv_id, (last_values or {}).get("state")
                    )
```
> Place the persist call where `conv_id` and `_conversation_store` are in scope (the streaming handler already references both for `_persist_conv_turn`). Keep all other streaming logic identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_responses_agent_session_state.py -q`
Expected: PASS. Then: `cd python && uv run pytest tests/ -k responses -q` (no regression).

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git checkout python/uv.lock 2>/dev/null
git add python/src/apx_agent/_responses_agent.py python/tests/test_responses_agent_session_state.py
git commit -m "feat: responses agent seeds + persists session_state (non-streaming + streaming)

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 5: full-suite + pyright gate, cross-turn integration, and live smoke

**Files:**
- Test: `python/tests/test_session_state_roundtrip.py` (integration, no LLM)
- Create (scratch, deleted before commit): `python/scratch_live_session.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.

- [ ] **Step 1: Write the cross-turn integration test**

```python
# python/tests/test_session_state_roundtrip.py
"""Two turns through the chat agent with a fake graph: turn 2's seed reflects
what turn 1 persisted (temp: stripped). No LLM."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage
from mlflow.types.agent import ChatAgentMessage

from apx_agent import LlmAgent
from apx_agent._chat_agent import chat_agent_for
from apx_agent._conversation import InMemoryConversationStore


class _WritingGraph:
    """Records the seeded state; returns prior+delta as the new state (mirrors
    how a real graph seeds from input state then merges this turn's writes)."""

    def __init__(self, delta):
        self.delta = delta
        self.seen_state = None

    def invoke(self, payload):
        self.seen_state = dict(payload.get("state") or {})
        msgs = list(payload["messages"]) + [AIMessage(content="ok")]
        return {"messages": msgs, "state": {**self.seen_state, **self.delta}}


@contextmanager
def _patched(graph):
    with patch(
        "apx_agent._defaults._make_workspace_client",
        return_value=MagicMock(name="sp_ws"),
    ), patch(
        "apx_agent._chat_agent.compile_to_langgraph", return_value=graph
    ):
        yield


def _turn(wrapped, graph, text):
    with _patched(graph):
        wrapped.predict(
            messages=[ChatAgentMessage(role="user", content=text, id="u")],
            custom_inputs={"thread_id": "t1"},
        )


def test_state_threads_across_two_turns():
    store = InMemoryConversationStore("memory://")
    wrapped = chat_agent_for(
        LlmAgent(tools=[], instructions="help"),
        model="any-endpoint",
        conversation_store=store,
    )

    # turn 1 writes account_id + a temp: scratch key
    g1 = _WritingGraph({"account_id": "ACME-42", "temp:scratch": "x"})
    _turn(wrapped, g1, "resolve acme")
    assert store.get_conversation("t1").session_state == {"account_id": "ACME-42"}

    # turn 2 sees account_id seeded, not the temp: key
    g2 = _WritingGraph({"note": "second"})
    _turn(wrapped, g2, "and now?")
    assert g2.seen_state == {"account_id": "ACME-42"}
    assert store.get_conversation("t1").session_state == {
        "account_id": "ACME-42",
        "note": "second",
    }
```

- [ ] **Step 2: Run the integration test**

Run: `cd python && uv run pytest tests/test_session_state_roundtrip.py -q`
Expected: PASS.

- [ ] **Step 3: Full suite + pyright**

Run:
```bash
cd python && uv run pytest -q && uv run pyright
```
Expected: full suite passes (if `test_trace_search_reality_ctk.py` fails only in the full run, re-run it alone — known order-dependent flake; note it, don't chase it); pyright `0 errors` (pre-existing `_tool.py` warnings are acceptable).

- [ ] **Step 4: Live smoke (scratch, not committed)**

```python
# python/scratch_live_session.py  — delete after
import uuid
from mlflow.types.agent import ChatAgentMessage
from apx_agent import Agent, Dependencies
from apx_agent._chat_agent import chat_agent_for
from apx_agent._conversation import InMemoryConversationStore

def remember_color(color: str, state: Dependencies.State) -> str:
    """Remember the user's favorite color."""
    state["favorite_color"] = color
    return f"got it: {color}"

store = InMemoryConversationStore("memory://")
wrapped = chat_agent_for(
    Agent(tools=[remember_color], name="c",
          instructions="If the user states a favorite color, call remember_color. "
                       "If asked what their color is, answer from this: {favorite_color}."),
    model="databricks-claude-sonnet-4-6",
    conversation_store=store,
)
tid = f"t-{uuid.uuid4().hex[:8]}"
wrapped.predict(
    messages=[ChatAgentMessage(role="user", content="My favorite color is teal.", id="u1")],
    custom_inputs={"thread_id": tid},
)
print("after turn 1:", store.get_conversation(tid).session_state)
resp = wrapped.predict(
    messages=[ChatAgentMessage(role="user", content="What's my favorite color?", id="u2")],
    custom_inputs={"thread_id": tid},
)
print("turn 2 answer:", resp.messages[-1].content)
fav = store.get_conversation(tid).session_state.get("favorite_color")
assert fav is not None and fav.lower().startswith("teal")
assert "teal" in resp.messages[-1].content.lower()
print("OK: session_state persisted across turns and the second turn read it.")
```
Run: `cd python && DATABRICKS_CONFIG_PROFILE=fe-stable uv run python scratch_live_session.py`
Expected: turn-1 state shows `favorite_color`, turn-2 answer contains "teal", `OK:` printed.

> Construct via `chat_agent_for(...)` exactly as in the unit tests. If the live two-turn read depends on `{key}` templating reading seeded state, that path is already shipped (#240/#242); this smoke just confirms the persistence wiring end to end.

- [ ] **Step 5: Clean up + commit the integration test**

```bash
cd python && rm -f scratch_live_session.py && cd .. && git checkout python/uv.lock 2>/dev/null
git add python/tests/test_session_state_roundtrip.py
git commit -m "test: cross-turn session_state round-trip integration

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
git status --short   # expect clean (no scratch_*, no uv.lock)
```

- [ ] **Step 6: Push + PR + auto-merge**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git push -u origin feat/session-state-persistence
gh pr create --title "feat: session-scoped state persistence (G3 phase 2)" \
  --body "Implements docs/design/session-state-persistence.md. In-graph keyed state survives across turns: seeded from Conversation.session_state, persisted via a governed set_session_state on ConversationStore (in-memory + Delta + Lakebase); temp:-prefixed keys are in-turn only. Wired into both AgentServer adapters (chat predict/predict_stream, responses non-streaming/streaming). Live-verified cross-turn.

This pull request and its description were written by Isaac."
gh pr merge --auto --squash
```

---

## Notes for the implementer

- **Seed is trivial; persist-on-streaming is the subtle part.** The multi-mode `stream(stream_mode=["updates","values"])` change is verified to expose the final `state` as the last `values` chunk. Keep the `updates`-chunk emission logic byte-identical — only wrap it in the `mode` branch.
- **Full-overwrite is intentional.** Don't read-merge in `set_session_state`; the value handed in is already prior+turn merged. A regression toward read-merge would double-apply.
- **Never crash a turn on persist failure.** `persist_session_state` swallows + logs. Don't "improve" it into a raise.
- **Don't widen scope.** Only the `temp:` prefix this phase. No `user:`/`app:` handling.
- After Tasks 1–2, both SQL backends MUST implement `set_session_state` or they become un-instantiable (abstract). The Step-4 import check in each task catches this.

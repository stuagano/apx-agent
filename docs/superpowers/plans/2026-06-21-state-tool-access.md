# Tool-level keyed-state access (G3 increment 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an apx tool read and write the in-graph keyed `state` channel via a dict-like `Dependencies.State`, with writes harvested into a LangGraph `Command` — no LangGraph types in the tool author's code.

**Architecture:** `Dependencies.State` is a sentinel-marked `Annotated` type (not a FastAPI `Depends`). `_inspect_tool_fn` records the State param name and excludes it from the LLM schema. `_make_langchain_tool` routes State-using tools through a wrapper that declares two LangGraph-injected params (`InjectedState("state")`, `InjectedToolCallId`), binds a `StateProxy` to the author's param, and after the fn returns converts tracked writes into `Command(update={"state": delta, "messages": [ToolMessage(...)]})`. `create_agent` is given `state_schema=ApxState` when a tool uses State, and the served-path wrapper node propagates tool-written `state` back out.

**Tech Stack:** Python 3.12, langgraph / langchain prebuilt `create_agent`, langchain_core tools, pydantic. Tests via `pytest` (`uv run pytest`). The feasibility gate (InjectedState + InjectedToolCallId + Command through ToolNode) is **already verified live** (see design doc); Task 1's test guards it without an LLM.

## Global Constraints

- Run all tests from `python/`: `cd python && uv run pytest …`. The repo root `.venv` shadows `src/` with a stale copy.
- Full-package pyright must stay clean: `cd python && uv run pyright`.
- No `.get(key, "")`, no `x or ""`, no `getattr(obj, "literal", default)`, no `str = ""` defaults, no `object` annotations, no `tuple[X, Y, ...]` returns — repo pre-commit guards reject these. (Use `dict.get(key)` returning `None`, explicit defaults, named models.)
- In-graph only — no persistence, no scope prefixes. Reducer stays #240's shallow last-write-wins; do NOT add a list-append reducer (deferred / YAGNI).
- `state` values stay in-process this increment; no JSON-serializability requirement yet.
- Commit messages end with the repo trailer block (`Co-authored-by: Isaac` + `Claude-Session:`).
- Branch: `feat/state-tool-access` (already created off `main`).

---

### Task 1: `StateProxy` — a write-tracking dict view

**Files:**
- Create: `python/src/apx_agent/_state_proxy.py`
- Test: `python/tests/test_state_proxy.py`

**Interfaces:**
- Produces: `class StateProxy(MutableMapping[str, Any])` wrapping a source dict. Reads pass through to the source; writes (`__setitem__`, `update`, `pop`, `setdefault`, `__delitem__`) are recorded. Properties: `dirty: bool` (any write happened), `delta: dict[str, Any]` (current value of every key the proxy wrote — sourced from the proxy's own merged view so a key written twice reports its latest value).

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_state_proxy.py
from apx_agent._state_proxy import StateProxy


def test_reads_pass_through_and_clean_by_default():
    p = StateProxy({"a": 1})
    assert p["a"] == 1
    assert p.get("missing") is None
    assert "a" in p
    assert p.dirty is False
    assert p.delta == {}


def test_setitem_tracked_into_delta():
    p = StateProxy({"a": 1})
    p["b"] = 2
    assert p["b"] == 2          # readable after write
    assert p.dirty is True
    assert p.delta == {"b": 2}


def test_last_write_wins_within_delta():
    p = StateProxy({})
    p["k"] = 1
    p["k"] = 2
    assert p.delta == {"k": 2}


def test_update_and_setdefault_and_pop_tracked():
    p = StateProxy({"x": 0})
    p.update({"y": 9})
    assert p.setdefault("z", 7) == 7
    assert p.setdefault("x", 100) == 0      # existing key unchanged, not tracked
    assert p.delta == {"y": 9, "z": 7}


def test_missing_key_raises_keyerror():
    p = StateProxy({})
    import pytest
    with pytest.raises(KeyError):
        _ = p["nope"]


def test_none_source_behaves_as_empty():
    p = StateProxy(None)
    assert p.get("a") is None
    p["a"] = 1
    assert p.delta == {"a": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_state_proxy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._state_proxy'`

- [ ] **Step 3: Write minimal implementation**

```python
# python/src/apx_agent/_state_proxy.py
"""A dict-like view over the in-graph keyed state that records writes.

The view is handed to tools as ``Dependencies.State``. Reads pass through to
the injected state dict; writes are recorded so the tool wrapper can emit them
as a LangGraph ``Command`` state delta after the tool returns. See
docs/design/keyed-state-tool-access.md (G3 increment 2).
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any


class StateProxy(MutableMapping[str, Any]):
    """Read-through, write-tracking view of the keyed ``state`` dict.

    ``delta`` is the set of keys this proxy wrote, with their latest values —
    that is what becomes the ``Command`` state update. In-place mutation of a
    value read out of the proxy (e.g. ``proxy["xs"].append(1)``) is NOT tracked;
    reassign the key to record a change.
    """

    def __init__(self, source: dict[str, Any] | None) -> None:
        self._source: dict[str, Any] = source if source is not None else {}
        self._writes: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key in self._writes:
            return self._writes[key]
        return self._source[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._writes[key] = value

    def __delitem__(self, key: str) -> None:
        # Deletion is recorded as a write of None (the reducer can't remove a
        # key; this is the closest in-graph semantic). Documented limitation.
        if key not in self._source and key not in self._writes:
            raise KeyError(key)
        self._writes[key] = None

    def __iter__(self) -> Iterator[str]:
        return iter({**self._source, **self._writes})

    def __len__(self) -> int:
        return len({**self._source, **self._writes})

    @property
    def dirty(self) -> bool:
        return bool(self._writes)

    @property
    def delta(self) -> dict[str, Any]:
        return dict(self._writes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_state_proxy.py -q`
Expected: PASS (6 passed)

Note: `MutableMapping` supplies `get`, `update`, `setdefault`, `pop`, `__contains__` on top of the five abstract methods above — that is why the `update`/`setdefault` test passes without writing them. `setdefault` on an existing key calls `__getitem__` only (no write), so it is correctly not tracked.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_state_proxy.py python/tests/test_state_proxy.py
git commit -m "feat: StateProxy — write-tracking dict view for tool state access

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 2: `Dependencies.State` marker + signature detection

**Files:**
- Create: `python/src/apx_agent/_state_marker.py` (sentinel, leaf module)
- Modify: `python/src/apx_agent/_defaults.py` (add `Dependencies.State` alias)
- Modify: `python/src/apx_agent/_inspection.py` (detect + skip State param, add `_state_param_name`)
- Test: `python/tests/test_state_dependency_detection.py`

**Interfaces:**
- Consumes: `StateProxy` (Task 1) only as the documented runtime type; detection is by marker, not type.
- Produces:
  - `_STATE_DEP` sentinel instance and `Dependencies.State` (an `Annotated` alias carrying it) in `_defaults.py`.
  - `_is_state_dependency(annotation) -> bool` and `_state_param_name(fn) -> str | None` in `_inspection.py`.
  - `_inspect_tool_fn` **skips** a State param: it is added to neither `plain_params` (so it's excluded from the LLM schema) nor `dep_param_names` (so `_resolve_deps_for_fn` never tries to resolve it as a FastAPI dep). `ToolSignature`'s shape is UNCHANGED — it stays a 2-field NamedTuple. (Six call sites unpack it positionally as `a, b = _inspect_tool_fn(fn)`; adding a field would break them with `ValueError: too many values to unpack`. The State param name is exposed via the separate `_state_param_name(fn)` helper instead.)

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_state_dependency_detection.py
from apx_agent import Dependencies
from apx_agent._inspection import _inspect_tool_fn, _is_state_dependency, _state_param_name


def test_is_state_dependency_true_for_state_alias():
    assert _is_state_dependency(Dependencies.State) is True


def test_is_state_dependency_false_for_plain_and_depends():
    assert _is_state_dependency(str) is False
    assert _is_state_dependency(Dependencies.UserClient) is False


def test_state_param_excluded_from_schema_and_not_a_dep():
    def tool(name: str, state: Dependencies.State) -> str:
        return name

    sig = _inspect_tool_fn(tool)
    assert _state_param_name(tool) == "state"
    assert "state" not in sig.plain_params       # excluded from LLM schema
    assert "state" not in sig.dep_param_names     # not a FastAPI dep
    assert "name" in sig.plain_params


def test_state_param_name_none_when_absent():
    def tool(name: str) -> str:
        return name

    assert _state_param_name(tool) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_state_dependency_detection.py -q`
Expected: FAIL — `ImportError: cannot import name '_is_state_dependency'` (and `Dependencies` has no `State`).

- [ ] **Step 3: Write minimal implementation**

In `python/src/apx_agent/_defaults.py`, near the other `Dependencies` aliases (after the imports at top, add the marker; inside `class Dependencies`, add the alias):

```python
# module level, with the other TypeAlias/dependency definitions
from collections.abc import MutableMapping
from typing import Annotated

from ._state_marker import _STATE_DEP

# Not a FastAPI Depends — resolved per-call from LangGraph state, not the request
# cycle. See _compile._make_stateful_langchain_tool and
# docs/design/keyed-state-tool-access.md.
StateDependency: TypeAlias = Annotated[MutableMapping[str, Any], _STATE_DEP]
```

```python
# inside class Dependencies, alongside Client/UserClient/Sql/...:
    State: TypeAlias = StateDependency
    """In-graph keyed state, read/written like a dict; excluded from the LLM
    schema. Writes are harvested into the graph state after the tool returns.
    In-place mutation of a nested value is not tracked — reassign the key.
    Recommended usage: ``state: Dependencies.State``"""
```

Create a leaf module to hold the sentinel so there's no import cycle (`_defaults.py` and `_inspection.py` both import it):

```python
# python/src/apx_agent/_state_marker.py
"""Sentinel marking a tool parameter as the in-graph keyed-state view.

Lives in its own leaf module so both _defaults (the Dependencies.State alias)
and _inspection (detection) can import it without a cycle.
"""


class _StateDep:
    """Marker object; identity-compared in _is_state_dependency."""


_STATE_DEP = _StateDep()
```

In `python/src/apx_agent/_inspection.py`, add the detector and the name helper, and make `_inspect_tool_fn` skip the State param. `ToolSignature` stays a 2-field NamedTuple — do NOT add a field:

```python
# near _is_fastapi_dependency
from apx_agent._state_marker import _STATE_DEP


def _is_state_dependency(annotation: Any) -> bool:
    """Return True if the annotation is ``Dependencies.State``."""
    if get_origin(annotation) is not Annotated:
        return False
    return any(arg is _STATE_DEP for arg in get_args(annotation))


def _state_param_name(fn: _ToolFn) -> str | None:
    """Return the name of the fn's Dependencies.State parameter, or None."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    for name in inspect.signature(fn).parameters:
        if _is_state_dependency(hints.get(name, Any)):
            return name
    return None
```

In the `_inspect_tool_fn` loop, skip a State param (excluded from BOTH lists, so it leaves the schema and is never resolved as a dep):

```python
    for name, param in sig.parameters.items():
        annotation = hints.get(name, Any)
        if _is_state_dependency(annotation):
            continue                            # injected per-call, not in schema/deps
        if _is_fastapi_dependency(annotation):
            dep_param_names.append(name)
        else:
            default = param.default if param.default is not inspect.Parameter.empty else ...
            plain_params[name] = (annotation, default)

    return ToolSignature(plain_params=plain_params, dep_param_names=dep_param_names)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_state_dependency_detection.py -q && uv run python -c "import apx_agent; print('import ok')"`
Expected: PASS (3 passed) and `import ok`.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_state_marker.py python/src/apx_agent/_defaults.py python/src/apx_agent/_inspection.py python/tests/test_state_dependency_detection.py
git commit -m "feat: Dependencies.State marker + signature detection

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 3: route State tools through the injected wrapper (the gate regression)

**Files:**
- Modify: `python/src/apx_agent/_compile.py` (`_make_langchain_tool`)
- Test: `python/tests/test_state_tool_injection.py`

**Interfaces:**
- Consumes: `StateProxy` (Task 1); `_state_param_name` (Task 2); existing `_resolve_deps_for_fn`, `_make_input_model`.
- Produces: `_make_langchain_tool` returns, for a State-using tool, a `StructuredTool` whose underlying func has hidden `Annotated[dict, InjectedState("state")]` and `Annotated[str, InjectedToolCallId]` params, binds a `StateProxy` to the author's state param, and returns either the plain value (no writes) or `Command(update={"state": delta, "messages": [ToolMessage(text, tool_call_id=...)]})` (writes). Schema (`args_schema`) excludes both injected params and the state param.
- This test exercises the verified gate (InjectedState + InjectedToolCallId + Command applied by a real `ToolNode`) **without an LLM** — it drives the tool with a synthetic tool call.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_state_tool_injection.py
import pytest
pytest.importorskip("langgraph")

from unittest.mock import MagicMock
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from apx_agent import Dependencies
from apx_agent._compile import CompileContext, _make_langchain_tool


def _ctx() -> CompileContext:
    # CompileContext carries ws/model/headers; tools here use neither.
    return CompileContext(ws=MagicMock(), model="databricks-claude-sonnet-4-6", headers=None)


def _tool_call(name: str, args: dict, call_id: str = "c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.mark.asyncio
async def test_state_write_becomes_command_update():
    def resolve(name: str, state: Dependencies.State) -> str:
        state["account_id"] = f"ACME-{name}"
        return f"resolved {name}"

    tool = _make_langchain_tool(resolve, _ctx())
    node = ToolNode([tool])
    out = await node.ainvoke(
        {"messages": [_tool_call("resolve", {"name": "x"})], "state": {}}
    )
    # state delta applied
    assert out["state"]["account_id"] == "ACME-x"
    # tool message still emitted with the plain return text
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and "resolved x" in tms[0].content


@pytest.mark.asyncio
async def test_state_read_only_returns_plain_value_no_state_update():
    def lookup(q: str, state: Dependencies.State) -> str:
        acct = state.get("account_id")
        return f"{q}:{acct}"

    tool = _make_langchain_tool(lookup, _ctx())
    node = ToolNode([tool])
    out = await node.ainvoke(
        {"messages": [_tool_call("lookup", {"q": "hi"})], "state": {"account_id": "A1"}}
    )
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and tms[0].content == "hi:A1"
    # no writes → no state delta on this node's output
    assert "state" not in out or out.get("state") in ({}, None)


def test_state_param_excluded_from_tool_schema():
    def resolve(name: str, state: Dependencies.State) -> str:
        return name

    tool = _make_langchain_tool(resolve, _ctx())
    assert set(tool.args.keys()) == {"name"}  # state + injected params hidden
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_state_tool_injection.py -q`
Expected: FAIL — the current `_make_langchain_tool` ignores `state_param_name`, so `state` is treated as a plain param (appears in schema / fn called without a proxy → `KeyError` or schema includes `state`).

- [ ] **Step 3: Write minimal implementation**

In `python/src/apx_agent/_compile.py`, add imports at top of `_make_langchain_tool` (local imports, matching the file's style) and branch on `state_param_name`:

The current code starts with `plain_params, _ = _inspect_tool_fn(fn)`. Add the State branch right after the existing setup lines (`plain_params`, `input_model`, `resolved_deps`, `is_async`), before the `if is_async:` block:

```python
def _make_langchain_tool(fn: Any, ctx: CompileContext) -> Any:
    from langchain_core.tools import StructuredTool

    plain_params, _ = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, plain_params)
    resolved_deps = _resolve_deps_for_fn(fn, ctx)
    is_async = inspect.iscoroutinefunction(fn)

    state_param = _state_param_name(fn)
    if state_param is not None:
        return _make_stateful_langchain_tool(
            fn, state_param, resolved_deps, input_model, is_async
        )

    # ... existing non-state path (async/sync wrappers) unchanged ...
```

> Import `_state_param_name` alongside the existing `_inspect_tool_fn` import in `_compile.py` (grep for `from ._inspection import` / `_inspect_tool_fn` and add it there). Leave the existing async/sync wrapper bodies as-is for the non-state path.

Add the stateful builder (place it just above `_make_langchain_tool`):

```python
def _tool_message_text(value: Any) -> str:
    """Render a tool return for a ToolMessage body, matching ToolNode's default
    coercion: strings verbatim, everything else as JSON."""
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _make_stateful_langchain_tool(
    fn: Any,
    state_param: str,
    resolved_deps: dict[str, Any],
    input_model: Any,
    is_async: bool,
) -> Any:
    """Build a StructuredTool for a tool that declares ``Dependencies.State``.

    The wrapper takes LangGraph-injected ``state`` and ``tool_call_id`` params
    (hidden from the LLM), binds a StateProxy to the author's state param, and
    turns tracked writes into a Command state delta after the fn returns.
    """
    from typing import Annotated

    from langchain_core.messages import ToolMessage
    from langchain_core.tools import InjectedToolCallId, StructuredTool
    from langgraph.prebuilt import InjectedState
    from langgraph.types import Command

    from ._state_proxy import StateProxy

    def _finish(proxy: StateProxy, tool_call_id: str, ret: Any) -> Any:
        if not proxy.dirty:
            return ret
        return Command(
            update={
                "state": proxy.delta,
                "messages": [
                    ToolMessage(_tool_message_text(ret), tool_call_id=tool_call_id)
                ],
            }
        )

    if is_async:
        async def _wrapper(
            __apx_state: Annotated[dict, InjectedState("state")],
            __apx_tool_call_id: Annotated[str, InjectedToolCallId],
            **kwargs: Any,
        ) -> Any:
            proxy = StateProxy(__apx_state)
            ret = await fn(**kwargs, **resolved_deps, **{state_param: proxy})
            return _finish(proxy, __apx_tool_call_id, ret)
    else:
        def _wrapper(
            __apx_state: Annotated[dict, InjectedState("state")],
            __apx_tool_call_id: Annotated[str, InjectedToolCallId],
            **kwargs: Any,
        ) -> Any:
            proxy = StateProxy(__apx_state)
            ret = fn(**kwargs, **resolved_deps, **{state_param: proxy})
            return _finish(proxy, __apx_tool_call_id, ret)

    _wrapper.__name__ = fn.__name__
    _wrapper.__doc__ = fn.__doc__
    if is_async:
        return StructuredTool.from_function(
            coroutine=_wrapper,
            name=fn.__name__,
            description=(fn.__doc__ or fn.__name__).strip(),
            args_schema=input_model,
        )
    return StructuredTool.from_function(
        func=_wrapper,
        name=fn.__name__,
        description=(fn.__doc__ or fn.__name__).strip(),
        args_schema=input_model,
    )
```

> If `test_state_param_excluded_from_tool_schema` shows `tool.args` still containing `__apx_state`/`__apx_tool_call_id`, that means `args_schema` isn't suppressing them — confirm `InjectedState`/`InjectedToolCallId` annotations cause langgraph to treat them as injected (they should, per the probe). If `tool.args` includes the injected names, add `from langchain_core.tools.base import get_all_basemodel_annotations` debugging, but first re-run: the probe used the same annotations and they were hidden.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_state_tool_injection.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_compile.py python/tests/test_state_tool_injection.py
git commit -m "feat: route Dependencies.State tools through InjectedState + Command

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 4: give `create_agent` the state schema + propagate tool writes through the wrapper node

**Files:**
- Modify: `python/src/apx_agent/_compile.py` (`_compile_llm_agent`, `_wrap_agent_node`)
- Test: `python/tests/test_state_tool_threading.py`

**Interfaces:**
- Consumes: `state_schema()` (existing, returns `ApxState`); `_make_stateful_langchain_tool` (Task 3); `_state_param_name` (Task 2).
- Produces:
  - `_agent_has_state_tool(agent) -> bool` helper — true if any of the agent's tool fns declares a `Dependencies.State` param.
  - `_compile_llm_agent` passes `state_schema=state_schema()` to `create_agent` when `_agent_has_state_tool(agent)` is true (keeps blast radius minimal — agents without State tools are unchanged).
  - `_wrap_agent_node`'s node merges `result.get("state")` into its returned update, so tool writes made inside a wrapped (guarded / output_key / templated) agent are not dropped.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_state_tool_threading.py
import pytest
pytest.importorskip("langgraph")

from unittest.mock import MagicMock
from langchain_core.messages import AIMessage, HumanMessage

from apx_agent import Dependencies, LlmAgent
from apx_agent._compile import _agent_has_state_tool, _wrap_agent_node


def test_agent_has_state_tool_detects_state_param():
    def with_state(q: str, state: Dependencies.State) -> str:
        return q

    def without(q: str) -> str:
        return q

    assert _agent_has_state_tool(LlmAgent(tools=[with_state], name="a")) is True
    assert _agent_has_state_tool(LlmAgent(tools=[without], name="b")) is False


class _StateWritingRunnable:
    """Fake inner agent that writes to state (as a Command-applying ToolNode
    would have) by returning a state delta alongside its messages."""

    async def ainvoke(self, state: dict) -> dict:
        msgs = list(state["messages"])
        return {
            "messages": msgs + [AIMessage(content="done")],
            "state": {"account_id": "ACME-1"},
        }


@pytest.mark.asyncio
async def test_wrapped_node_propagates_tool_state_writes():
    # An agent wrapped for output_key still must surface tool-written state.
    agent = LlmAgent(tools=[], name="x", instructions="Help.", output_key="answer")
    graph = _wrap_agent_node(agent, _StateWritingRunnable(), templated=False)
    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert out["state"]["account_id"] == "ACME-1"   # tool write survived
    assert out["state"]["answer"] == "done"          # output_key still written
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_state_tool_threading.py -q`
Expected: FAIL — `_agent_has_state_tool` missing; and `_wrap_agent_node` currently returns only `{"messages": ..., "state": {output_key: ...}}`, dropping the inner `result["state"]`, so `account_id` is absent.

- [ ] **Step 3: Write minimal implementation**

Add the helper near `_agent_needs_node_wrap` in `_compile.py`:

```python
def _agent_has_state_tool(agent: LlmAgent) -> bool:
    """True if any tool fn declares a Dependencies.State parameter."""
    return any(_state_param_name(fn) is not None for fn in agent._tool_fns)
```

In `_compile_llm_agent`, pass the state schema only when needed:

```python
    create_kwargs: dict[str, Any] = {
        "model": llm,
        "tools": tools,
        "system_prompt": (agent._instructions or None) if bake_prompt else None,
        "middleware": [_governance_exception_middleware()],
    }
    if _agent_has_state_tool(agent):
        create_kwargs["state_schema"] = state_schema()
    runnable = create_agent(**create_kwargs)
```

In `_wrap_agent_node`'s `_node`, merge the inner result's state into the update. Find the block that builds `update` near the end of `_node`:

```python
        await agent._invoke_callback(agent._after_agent_callback, text)
        update: dict[str, Any] = {"messages": new_msgs}
        inner_state = result.get("state")          # tool writes from the inner agent
        merged_state: dict[str, Any] = dict(inner_state) if inner_state else {}
        if agent._output_key:
            merged_state[agent._output_key] = text
        if merged_state:
            update["state"] = merged_state
        return update
```

> The output-guard replacement path (earlier in `_node`) intentionally does NOT write output_key. Leave it; per design, a guard replacement also discards in-progress tool state — do not propagate `result["state"]` on that branch.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_state_tool_threading.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_compile.py python/tests/test_state_tool_threading.py
git commit -m "feat: thread tool state writes — create_agent state_schema + wrapper propagation

Co-authored-by: Isaac
Claude-Session: https://claude.ai/code/session_01LQDopEif2g6KwEer5xJgD3"
```

---

### Task 5: full-suite + pyright gate, and a live end-to-end smoke check

**Files:**
- Modify: none (verification task) — fix any regressions surfaced.
- Create (optional): `python/scratch_live_state.py` (scratch, deleted before commit).

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: confidence that the increment is regression-free and works against a live LLM end-to-end (the agent↔tool↔agent thread the design promises).

- [ ] **Step 1: Run the full suite + pyright**

Run:
```bash
cd python && uv run pytest -q && uv run pyright
```
Expected: full suite passes (note `test_trace_search_reality_ctk.py` can fail only when run in a certain order — confirm it passes in isolation if it flags: `uv run pytest tests/test_trace_search_reality_ctk.py -q`); pyright `0 errors`.

- [ ] **Step 2: Live end-to-end smoke check (scratch, not committed)**

```python
# python/scratch_live_state.py  — delete after
import asyncio
from databricks.sdk import WorkspaceClient
from langchain_core.messages import HumanMessage
from apx_agent import Agent, Dependencies, SequentialAgent, compile_to_langgraph

def resolve_account(name: str, state: Dependencies.State) -> str:
    """Resolve a customer name to an account id and stash it."""
    state["account_id"] = "ACME-42"
    return f"resolved {name} to ACME-42"

ws = WorkspaceClient(); M = "databricks-claude-sonnet-4-6"
finder = Agent(tools=[resolve_account], name="finder",
    instructions="Call resolve_account with the customer name the user gives. Then say done.")
reporter = Agent(tools=[], name="reporter",
    instructions="The account id is {account_id}. State it back as 'Account: {account_id}'.")
graph = compile_to_langgraph(SequentialAgent(agents=[finder, reporter], name="flow"), ws=ws, model=M)

async def main():
    r = await graph.ainvoke({"messages": [HumanMessage(content="Look up Acme Corp")]})
    print("STATE:", r.get("state"))
    print("FINAL:", r["messages"][-1].content)
    assert r.get("state", {}).get("account_id") == "ACME-42", "tool write missing"
    assert "ACME-42" in r["messages"][-1].content, "downstream agent didn't read it"
    print("OK: tool wrote state, downstream {account_id} agent read it.")

asyncio.run(main())
```

Run: `cd python && DATABRICKS_CONFIG_PROFILE=fe-stable uv run python scratch_live_state.py`
Expected: `STATE: {'account_id': 'ACME-42'}`, final contains `ACME-42`, `OK:` printed.

> This depends on Task 3's `SequentialAgent` continuation fix (PR #241) being on the branch for a 2-agent pipeline to run on Claude. If #241 has merged to `main`, rebase this branch onto `main` first. If not, run the live check with the two steps invoked separately (one agent with the tool, one templated agent seeded with `{"state": {"account_id": "ACME-42"}}`), mirroring the increment-1 live verification.

- [ ] **Step 3: Clean up scratch + revert any uv.lock churn**

```bash
cd python && rm -f scratch_live_state.py && cd .. && git checkout python/uv.lock 2>/dev/null; git status --short
```
Expected: only the intended source/test changes staged or committed; no `scratch_*`, no `uv.lock`.

- [ ] **Step 4: Open PR with auto-merge**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git push -u origin feat/state-tool-access
gh pr create --title "feat: Dependencies.State — tool-level keyed state (G3 increment 2)" \
  --body "Implements docs/design/keyed-state-tool-access.md. Tools read/write the in-graph state channel via a dict-like Dependencies.State; writes harvested into a Command. In-graph only. Live-verified agent->tool->agent threading.

This pull request and its description were written by Isaac."
gh pr merge --auto --squash
```

---

## Notes for the implementer

- The feasibility gate is already proven (design doc, §"Feasibility gate — PASSED"). Task 3's test re-proves it LLM-free; if it ever fails on a langgraph upgrade, that is a real break, not a flaky test.
- Concurrency caveat is by design: two same-turn tool calls writing one key resolve last-write-wins. Do not "fix" it with a list-append reducer in this increment.
- Keep `import apx_agent` working — the Task 2 cycle note matters; verify after every Task 2/3/4 commit with `uv run python -c "import apx_agent"`.

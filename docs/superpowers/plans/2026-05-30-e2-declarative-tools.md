# E2 · Declarative Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent's resource-reference-tier tools be declared as data in `[[tool.apx.tools]]` (pyproject.toml) and auto-attached to the agent on all runtimes — serve, log/deploy, and model-serving-predict.

**Architecture:** A new `_tool_config.py` holds a `type → factory` registry, a dispatcher (`load_config_tools`), and a merge helper (`merge_config_tools`). E1's `apply_config_knobs` is promoted into a unified, idempotent `finalize_agent(agent, config, pyproject_path)` that applies knobs + the persona overlay + the config-tool merge, called from the **three** chokepoints that read the agent: `setup_agent` (serve), `log_agent` (log/deploy — this is the fix that covers direct notebook/Coworker logging AND model-serving predict, since predict captures the agent at log time), and `apx info`. The `apx deploy` CLI's standalone `apply_config_knobs` call is retired (now covered by `log_agent`).

**Tech Stack:** Python 3.11+, Pydantic v2, `tomllib`, pytest, pyright (CI gate — run `uv run pyright src/apx_agent/` before committing; these files are NOT in the type-debt exclude list so they must stay error-clean).

**Spec:** `docs/superpowers/specs/2026-05-29-e2-declarative-tools-design.md`
**Backing analysis:** `docs/engine-scope/02-declarative-tools.md` (factory inventory, governance, full rationale)

**Decisions locked (2026-05-30):**
- Chokepoint: **promote `apply_config_knobs` → `finalize_agent` inside `log_agent`** (+ `setup_agent` + `apx info`).
- Trust model: **trusted default + opt-in `APX_TOOLS_ALLOWED_HOSTS` allow-list** for `openapi`/`mcp_*`.

**Convention:** run everything from `python/` via `uv run …` (repo-root `.venv` is stale and shadows `src/`).

---

## File structure

- **Create** `python/src/apx_agent/_tool_config.py` — `ToolConfigError`, the `type→factory` registry, `load_config_tools(raw_tables)`, env-var resolution, host allow-list, `merge_config_tools(agent, pyproject_path)`. One responsibility: turning declared tool tables into merged callables.
- **Modify** `python/src/apx_agent/_agents.py` — add `LlmAgent._register_tool(fn)` (append to `_tool_fns` AND `_analyzed`).
- **Modify** `python/src/apx_agent/_wiring.py` — add `finalize_agent(...)` (wraps `apply_config_knobs` + `merge_config_tools`); call it from `setup_agent` in place of the bare `apply_config_knobs`.
- **Modify** `python/src/apx_agent/_chat_agent.py` — call `finalize_agent` at the top of `log_agent`.
- **Modify** `python/src/apx_agent/cli.py` — call `finalize_agent` in `apx info`; remove the deploy command's standalone `apply_config_knobs` call (now covered by `log_agent`).
- **Create** `python/tests/test_tool_config.py`; **modify** `python/tests/test_wiring.py`, `python/tests/test_chat_agent.py`.

---

## Task 1: `_tool_config.py` — registry + dispatcher + errors (offline core)

**Files:**
- Create: `python/src/apx_agent/_tool_config.py`
- Test: `python/tests/test_tool_config.py`

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_tool_config.py
import pytest
from apx_agent._tool_config import ToolConfigError, load_config_tools


def test_dispatches_single_tool_by_keyword():
    tools = load_config_tools([{"type": "genie", "space_id": "01ef", "name": "ask_sales"}])
    assert len(tools) == 1
    assert tools[0].__name__ == "ask_sales"


def test_flattens_toolkit_lists():
    # sql + a jobs toolkit (returns 4) → 1 + 4 = 5 callables, all flat
    tools = load_config_tools([
        {"type": "sql", "warehouse_id": "wh1"},
        {"type": "jobs", "warehouse_id": "wh1"},
    ])
    assert all(callable(t) for t in tools)
    assert len(tools) == 5


def test_unknown_type_raises_listing_known():
    with pytest.raises(ToolConfigError, match="unknown type 'nope'"):
        load_config_tools([{"type": "nope"}])


def test_missing_type_raises():
    with pytest.raises(ToolConfigError, match="missing 'type'"):
        load_config_tools([{"space_id": "x"}])


def test_missing_required_arg_wrapped_as_config_error():
    with pytest.raises(ToolConfigError, match="genie"):
        load_config_tools([{"type": "genie"}])  # genie_tool needs space_id


def test_same_name_collision_requires_explicit_name():
    with pytest.raises(ToolConfigError, match="duplicate tool name"):
        load_config_tools([
            {"type": "vector_search", "index_name": "a.b.c"},
            {"type": "vector_search", "index_name": "a.b.d"},  # both default to "vector_search"
        ])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_tool_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._tool_config'`

- [ ] **Step 3: Write minimal implementation**

```python
# python/src/apx_agent/_tool_config.py
"""Declarative resource tools — load [[tool.apx.tools]] into callables.

Each table is ``{type, <factory kwargs>}``. ``type`` selects a factory from the
registry; the remaining keys are splatted as keyword args (every factory takes
its identifier as positional-or-keyword, so all-keyword calls work uniformly).
Toolkit factories return lists, which are flattened. The factories are the
validation surface — a bad/missing kwarg surfaces as a wrapped ToolConfigError.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ToolConfigError(ValueError):
    """A [[tool.apx.tools]] table could not be turned into a tool."""


def _registry() -> dict[str, Callable[..., Any]]:
    # Lazy imports keep this module cheap to import (factories pull in the SDK).
    from .catalog import (
        catalog_tool,
        lineage_tool,
        schema_tool,
        uc_function_tool,
        uc_function_toolkit,
    )
    from .foundation_model import foundation_model_tool
    from .genie import genie_query_tool, genie_tool
    from .http_tools import http_tool, openapi_tool
    from .jobs_tools import (
        jobs_for_table_tool,
        jobs_history_tool,
        jobs_logs_tool,
        jobs_source_paths_tool,
        jobs_tools,
    )
    from .mcp_consume import mcp_tool, mcp_toolkit
    from .sql_tools import sql_tool
    from .vector_search import vector_search_tool

    return {
        "genie": genie_tool,
        "genie_query": genie_query_tool,
        "vector_search": vector_search_tool,
        "uc_function": uc_function_tool,
        "uc_function_toolkit": uc_function_toolkit,
        "catalog": catalog_tool,
        "schema": schema_tool,
        "lineage": lineage_tool,
        "sql": sql_tool,
        "http": http_tool,
        "openapi": openapi_tool,
        "mcp_tool": mcp_tool,
        "mcp_toolkit": mcp_toolkit,
        "foundation_model": foundation_model_tool,
        "jobs": jobs_tools,
        "jobs_for_table": jobs_for_table_tool,
        "jobs_history": jobs_history_tool,
        "jobs_logs": jobs_logs_tool,
        "jobs_source_paths": jobs_source_paths_tool,
    }


def _build_one(index: int, table: dict[str, Any], registry: dict[str, Callable]) -> list[Callable]:
    kwargs = dict(table)
    type_ = kwargs.pop("type", None)
    if type_ is None:
        raise ToolConfigError(f"tool #{index}: missing 'type' key.")
    factory = registry.get(type_)
    if factory is None:
        raise ToolConfigError(
            f"tool #{index}: unknown type {type_!r}; known: {sorted(registry)}."
        )
    try:
        result = factory(**kwargs)
    except ToolConfigError:
        raise
    except TypeError as e:
        raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
    return result if isinstance(result, list) else [result]


def load_config_tools(raw_tables: list[dict[str, Any]]) -> list[Callable]:
    """Build the flat list of tool callables from [[tool.apx.tools]] tables."""
    registry = _registry()
    out: list[Callable] = []
    for i, table in enumerate(raw_tables):
        out.extend(_build_one(i, table, registry))
    # Config-vs-config name collision: two tables yielding the same __name__ is
    # an authoring bug (would break the LLM tool schema) — fail loud.
    seen: set[str] = set()
    for fn in out:
        nm = getattr(fn, "__name__", None)
        if nm in seen:
            raise ToolConfigError(
                f"duplicate tool name {nm!r} from [[tool.apx.tools]]; "
                f"set an explicit 'name' on one of them."
            )
        if nm:
            seen.add(nm)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_tool_config.py -v`
Expected: PASS (6 passed). If `jobs`/`sql` factories require a live `ws` to *construct* (they should not — they defer I/O to call time), and a test errors, mark it and report — do not loosen the assertion.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_tool_config.py python/tests/test_tool_config.py
git commit -m "feat(tools): [[tool.apx.tools]] registry + dispatcher (offline core)"
```

---

## Task 2: env-var resolution, host allow-list, strict mode

**Files:**
- Modify: `python/src/apx_agent/_tool_config.py`
- Test: `python/tests/test_tool_config.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_tool_config.py
import os


def test_env_var_resolved_on_string_values(monkeypatch):
    monkeypatch.setenv("SALES_SPACE", "01ef-from-env")
    tools = load_config_tools([{"type": "genie", "space_id": "$SALES_SPACE", "name": "ask"}])
    # The genie tool closes over the resolved space_id; assert via the factory
    # being called with the resolved value by checking no error + a built tool.
    assert tools[0].__name__ == "ask"


def test_allowlist_blocks_disallowed_host(monkeypatch):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    with pytest.raises(ToolConfigError, match="not in APX_TOOLS_ALLOWED_HOSTS"):
        load_config_tools([{
            "type": "mcp_toolkit",
            "server_url": "https://evil.example.com/mcp",
        }])


def test_allowlist_unset_allows_any_host(monkeypatch):
    monkeypatch.delenv("APX_TOOLS_ALLOWED_HOSTS", raising=False)
    # No allow-list configured → trusted default → host not checked.
    # Monkeypatch mcp_toolkit so we don't hit the network at factory time.
    import apx_agent._tool_config as mod
    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": lambda **kw: [lambda: None]})
    tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://anything/mcp"}])
    assert len(tools) == 1


def test_io_factory_failure_skipped_with_warning(monkeypatch, caplog):
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.delenv("APX_TOOLS_STRICT", raising=False)
    tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])
    assert tools == []  # skipped, not raised


def test_io_factory_failure_raises_in_strict_mode(monkeypatch):
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.setenv("APX_TOOLS_STRICT", "1")
    with pytest.raises(ToolConfigError, match="server down"):
        load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_tool_config.py -k "env or allowlist or strict or io_factory" -v`
Expected: FAIL — allow-list/env/strict not implemented.

- [ ] **Step 3: Write the implementation**

Add to `python/src/apx_agent/_tool_config.py` (imports + helpers + wire into `_build_one`/`load_config_tools`):

```python
import os
from urllib.parse import urlparse

from ._wiring import _resolve_env_var  # $VAR / ${VAR} → env value

# Types whose factory touches the network at construction (host-gated + skippable).
_IO_TYPES = {"openapi", "mcp_tool", "mcp_toolkit"}
# Which kwarg holds the host-bearing URL, per IO type.
_HOST_KEY = {"openapi": "spec", "mcp_tool": "server_url", "mcp_toolkit": "server_url"}


def _resolve_env_deep(value: Any) -> Any:
    if isinstance(value, str):
        return _resolve_env_var(value)
    if isinstance(value, list):
        return [_resolve_env_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_env_deep(v) for k, v in value.items()}
    return value


def _check_allowlist(index: int, type_: str, kwargs: dict[str, Any]) -> None:
    allowed = os.environ.get("APX_TOOLS_ALLOWED_HOSTS", "").strip()
    if not allowed or type_ not in _IO_TYPES:
        return  # unset → trusted default → no restriction
    hosts = {h.strip() for h in allowed.split(",") if h.strip()}
    url = kwargs.get(_HOST_KEY[type_], "")
    host = urlparse(url).hostname or ""
    if host not in hosts:
        raise ToolConfigError(
            f"tool #{index} (type={type_}): host {host!r} is not in "
            f"APX_TOOLS_ALLOWED_HOSTS ({sorted(hosts)})."
        )
```

Rewrite `_build_one` to resolve env vars, enforce the allow-list, and apply the skip-with-warning / strict policy for I/O factories:

```python
def _build_one(index: int, table: dict[str, Any], registry: dict[str, Callable]) -> list[Callable]:
    kwargs = _resolve_env_deep(dict(table))
    type_ = kwargs.pop("type", None)
    if type_ is None:
        raise ToolConfigError(f"tool #{index}: missing 'type' key.")
    factory = registry.get(type_)
    if factory is None:
        raise ToolConfigError(
            f"tool #{index}: unknown type {type_!r}; known: {sorted(registry)}."
        )
    _check_allowlist(index, type_, kwargs)
    try:
        result = factory(**kwargs)
    except ToolConfigError:
        raise
    except TypeError as e:
        # Bad/missing kwarg — always a hard config error.
        raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
    except Exception as e:
        # Factory-time runtime failure (network/live discovery). Only I/O types
        # reach here; pure-data factories don't fail for connectivity reasons.
        if os.environ.get("APX_TOOLS_STRICT", "").strip() in ("1", "true", "True"):
            raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
        logger.warning(
            "Skipping tool #%d (type=%s): factory failed at load time: %s",
            index, type_, e,
        )
        return []
    return result if isinstance(result, list) else [result]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_tool_config.py -v`
Expected: PASS (all). Then `cd python && uv run pyright src/apx_agent/_tool_config.py` → 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_tool_config.py python/tests/test_tool_config.py
git commit -m "feat(tools): env-var resolution, host allow-list, strict mode"
```

---

## Task 3: `LlmAgent._register_tool` — keep `_tool_fns` and `_analyzed` in sync

**Files:**
- Modify: `python/src/apx_agent/_agents.py`
- Test: `python/tests/test_agents_register_tool.py` (new) — or append to an existing agents test file if one exists; check `ls python/tests | grep agents` first.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_agents_register_tool.py
from apx_agent import Agent


def _a_tool(query: str) -> str:
    """Echo tool."""
    return query


def test_register_tool_updates_both_lists_and_collect_tools():
    agent = Agent(tools=[])
    before = len(agent.collect_tools())
    agent._register_tool(_a_tool)
    assert _a_tool in agent._tool_fns
    # _analyzed must grow too, or collect_tools()/build_router() won't see it
    assert len(agent._analyzed) == len(agent._tool_fns)
    names = [t.name for t in agent.collect_tools()]
    assert "_a_tool" in names
    assert len(agent.collect_tools()) == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_agents_register_tool.py -v`
Expected: FAIL — `AttributeError: 'LlmAgent' object has no attribute '_register_tool'`

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_agents.py`, add a method to `LlmAgent` (the `_analyzed` tuple shape and helpers `_inspect_tool_fn` / `_make_input_model` already exist and are used in `__init__` at lines ~125-129):

```python
    def _register_tool(self, fn: _ToolFn) -> None:
        """Append a tool post-construction, keeping _tool_fns and _analyzed in sync.

        Run-time compile reads _tool_fns; collect_tools()/build_router() read
        _analyzed. Both must grow together or a tool added after __init__ is
        invisible to the A2A card / MCP surface / per-tool routes.
        """
        self._tool_fns.append(fn)
        plain_params, dep_names = _inspect_tool_fn(fn)
        input_model = _make_input_model(fn, plain_params)
        self._analyzed.append((fn, plain_params, dep_names, input_model))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_agents_register_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agents.py python/tests/test_agents_register_tool.py
git commit -m "feat(agents): LlmAgent._register_tool keeps _tool_fns + _analyzed in sync"
```

---

## Task 4: `merge_config_tools(agent, pyproject_path=None)`

**Files:**
- Modify: `python/src/apx_agent/_tool_config.py`
- Test: `python/tests/test_tool_config.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_tool_config.py
import textwrap
from apx_agent import Agent
from apx_agent._tool_config import merge_config_tools


def _write_pyproject(tmp_path, body: str):
    p = tmp_path / "pyproject.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_merge_appends_config_tools_to_agent(tmp_path):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)
    assert "ask_sales" in [t.name for t in agent.collect_tools()]


def test_merge_is_idempotent(tmp_path):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)
    merge_config_tools(agent, pyproject_path=pp)  # second call
    assert [t.name for t in agent.collect_tools()].count("ask_sales") == 1


def test_code_wired_tool_wins_on_name_collision(tmp_path, caplog):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    def ask_sales(q: str) -> str:
        """Code-wired."""
        return q
    agent = Agent(tools=[ask_sales])
    merge_config_tools(agent, pyproject_path=pp)
    # only one ask_sales, and it's the code-wired callable
    assert agent._tool_fns.count(ask_sales) == 1
    assert [t.name for t in agent.collect_tools()].count("ask_sales") == 1


def test_no_tools_section_is_noop(tmp_path):
    pp = _write_pyproject(tmp_path, '[tool.apx.agent]\nname = "t"\n')
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)  # no [[tool.apx.tools]]
    assert agent.collect_tools() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_tool_config.py -k merge -v`
Expected: FAIL — `cannot import name 'merge_config_tools'`

- [ ] **Step 3: Write the implementation**

Add to `python/src/apx_agent/_tool_config.py`:

```python
from pathlib import Path


def _read_tools_section(pyproject_path: str | None) -> list[dict[str, Any]]:
    path = Path(pyproject_path) if pyproject_path else Path.cwd() / "pyproject.toml"
    if not path.exists():
        return []
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(path.read_text())
    except Exception:
        return []
    tables = (((data.get("tool") or {}).get("apx") or {}).get("tools")) or []
    return tables if isinstance(tables, list) else []


def merge_config_tools(agent: Any, pyproject_path: str | None = None) -> None:
    """Load [[tool.apx.tools]] and append the callables to the agent.

    Dedup by __name__ (code-wired tools win — config is additive), which also
    makes this idempotent (a second call sees the config tools already present).
    Composition roots without ``_register_tool`` are warned + skipped.
    """
    tables = _read_tools_section(pyproject_path)
    if not tables:
        return
    register = getattr(agent, "_register_tool", None)
    existing = {getattr(fn, "__name__", None) for fn in getattr(agent, "_tool_fns", [])}
    if register is None:
        logger.warning(
            "[[tool.apx.tools]] declared but %s is a composition root with no "
            "_tool_fns to attach them to — skipping. Put tools on a leaf LlmAgent.",
            type(agent).__name__,
        )
        return
    for fn in load_config_tools(tables):
        nm = getattr(fn, "__name__", None)
        if nm in existing:
            logger.warning(
                "[[tool.apx.tools]] declares %r but the agent already wires a "
                "tool with that name — keeping the existing one, ignoring config.",
                nm,
            )
            continue
        register(fn)
        if nm:
            existing.add(nm)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_tool_config.py -v`
Expected: PASS (all). Then `uv run pyright src/apx_agent/_tool_config.py` → 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_tool_config.py python/tests/test_tool_config.py
git commit -m "feat(tools): merge_config_tools (dedup, idempotent, composition-root skip)"
```

---

## Task 5: `finalize_agent` — promote `apply_config_knobs`, wire into `setup_agent`

**Files:**
- Modify: `python/src/apx_agent/_wiring.py`
- Test: `python/tests/test_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import textwrap
from apx_agent import Agent, AgentConfig
from apx_agent._wiring import finalize_agent


def test_finalize_applies_knobs_and_merges_tools(tmp_path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """))
    agent = Agent(tools=[], instructions="GROUNDING")
    cfg = AgentConfig(name="t", temperature=0.3, instructions="PERSONA")
    finalize_agent(agent, config=cfg, pyproject_path=str(pp))
    # knob applied
    assert agent._temperature == 0.3
    # persona overlay applied
    assert "PERSONA" in agent._instructions and "GROUNDING" in agent._instructions
    # config tool merged
    assert "ask_sales" in [t.name for t in agent.collect_tools()]


def test_finalize_is_idempotent(tmp_path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text('[tool.apx.agent]\nname="t"\n[[tool.apx.tools]]\ntype="genie"\nspace_id="01ef"\nname="ask_sales"\n')
    agent = Agent(tools=[])
    cfg = AgentConfig(name="t")
    finalize_agent(agent, config=cfg, pyproject_path=str(pp))
    finalize_agent(agent, config=cfg, pyproject_path=str(pp))
    assert [t.name for t in agent.collect_tools()].count("ask_sales") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py -k finalize -v`
Expected: FAIL — `cannot import name 'finalize_agent'`

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_wiring.py`, add `finalize_agent` (keep `apply_config_knobs` as-is; `finalize_agent` composes it with the tool merge):

```python
def finalize_agent(
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> None:
    """Apply all config→instance steps before the agent is served or logged.

    The single seam every runtime must run: it applies generation knobs + the
    persona instruction overlay (apply_config_knobs) AND merges
    [[tool.apx.tools]] (merge_config_tools). Idempotent — safe to call from
    setup_agent (serve), log_agent (log/deploy), and apx info; a second call is
    a no-op. Future declarative features (memory, guards — E3) extend here.
    """
    if config is None:
        config = _load_agent_config(pyproject_path=pyproject_path)
    if config is not None:
        apply_config_knobs(agent, config)
    from ._tool_config import merge_config_tools

    merge_config_tools(agent, pyproject_path=pyproject_path)
```

Then in `setup_agent`, replace the existing bare `apply_config_knobs(agent, config)` call (currently `_wiring.py:191`, located just before `tools = agent.collect_tools()`) with:

```python
    # Apply knobs + persona overlay + config-tool merge BEFORE the card snapshot
    # (collect_tools below) so declared tools are both callable and advertised.
    finalize_agent(agent, config, pyproject_path=pyproject_path)
```

(Confirm `pyproject_path` is in scope in `setup_agent`; it is a parameter of `setup_agent`. If `_load_agent_config` here doesn't accept a `pyproject_path` kwarg with that exact name, match its real signature — it is `_load_agent_config(section_path=..., pyproject_path=...)` per `_inspection.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py -v`
Expected: PASS (new finalize tests + all pre-existing wiring/knob tests). Then `uv run pyright src/apx_agent/_wiring.py` → 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_wiring.py python/tests/test_wiring.py
git commit -m "feat(wiring): finalize_agent (knobs + persona + config tools); use in setup_agent"
```

---

## Task 6: Wire `finalize_agent` into `log_agent`; retire the deploy CLI's knob call

**Files:**
- Modify: `python/src/apx_agent/_chat_agent.py`
- Modify: `python/src/apx_agent/cli.py`
- Test: `python/tests/test_chat_agent.py`

This is the governance-critical task: `log_agent` reads the agent twice — `mlflow_resources_for(agent, ...)` (resource derivation) then `compile_to_chat_agent(agent, ...)` (captured for model-serving predict). `finalize_agent` must run **before both**.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_chat_agent.py
import textwrap
from apx_agent import Agent
from apx_agent._resources import mlflow_resources_for


def test_log_path_finalizes_before_resource_derivation(tmp_path, monkeypatch):
    # A config-declared genie tool must contribute a genie_space resource at
    # log time — proves finalize runs before mlflow_resources_for. (This is the
    # governance regression the sub_agents precedent lacked.)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef0000000000000000000000000000"
        name = "ask_sales"
    """))
    agent = Agent(tools=[])
    from apx_agent._wiring import finalize_agent
    finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"))
    specs = [s.kind for s in mlflow_resources_for(agent, model="m")]  # or .resource_type
    assert "genie_space" in str(specs).lower()
```

(Adjust the resource-attribute access to the real `ResourceSpec` field — inspect `mlflow_resources_for`'s return and `ResourceSpec` in `_resources.py` first; the assertion intent is "a genie_space resource is present".)

- [ ] **Step 2: Run test to verify it fails (then verify the real gap)**

Run: `cd python && uv run pytest tests/test_chat_agent.py -k log_path_finalizes -v`
This test calls `finalize_agent` directly so it should PASS once Task 5 is done — it's the *spec* of the behavior. Then add the harder assertion: that `log_agent` ITSELF finalizes. Since `log_agent` calls mlflow, assert via a spy:

```python
def test_log_agent_calls_finalize(monkeypatch, tmp_path):
    import apx_agent._chat_agent as ca
    called = {}
    monkeypatch.setattr(ca, "finalize_agent", lambda agent, **kw: called.setdefault("yes", True))
    # stub mlflow bits so we don't actually log
    monkeypatch.setattr(ca, "compile_to_chat_agent", lambda agent, **kw: object())
    import sys, types
    fake_mlflow = types.SimpleNamespace(
        pyfunc=types.SimpleNamespace(log_model=lambda **kw: "ok"),
        set_experiment=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    monkeypatch.setitem(sys.modules, "mlflow.pyfunc", fake_mlflow.pyfunc)
    monkeypatch.setattr(ca, "mlflow_resources_for", lambda agent, **kw: [], raising=False)
    ca.log_agent(Agent(tools=[]), model="m")
    assert called.get("yes")
```

Expected before impl: FAIL (`finalize_agent` not imported/called in `_chat_agent`).

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_chat_agent.py`, inside `log_agent`, immediately after the `mlflow` import block and before `mlflow_resources_for(...)` is called, add:

```python
    from ._wiring import finalize_agent

    # Finalize BEFORE resource derivation and BEFORE compile capture: config
    # tools must appear in the logged resources AND in the agent the served
    # model compiles at predict time. log_agent is public (notebooks/Coworker
    # call it directly), so this is the single site that covers all of them.
    finalize_agent(agent, pyproject_path=None)  # reads cwd pyproject.toml
```

In `python/src/apx_agent/cli.py`, **remove** the deploy command's standalone knob application (currently ~`cli.py:1798-1808`: the `from ._wiring import apply_config_knobs` import, the `AgentConfig` dict-filter, and the `apply_config_knobs(agent, deploy_config)` call). `log_agent` now finalizes. Replace that block with a one-line comment:

```python
    # (Config knobs + persona overlay + declared tools are applied inside
    # log_agent via finalize_agent — no separate call needed here.)
```

- [ ] **Step 4: Run tests**

Run: `cd python && uv run pytest tests/test_chat_agent.py tests/test_cli.py -v`
Expected: PASS. Confirm no pre-existing deploy/cli test regressed from removing the standalone knob call. Then `uv run pyright src/apx_agent/_chat_agent.py` → 0 errors (cli.py is in the type-debt exclude list, so it's not gated — but don't add new errors).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_chat_agent.py python/src/apx_agent/cli.py python/tests/test_chat_agent.py
git commit -m "feat(deploy): finalize_agent inside log_agent; retire CLI-only knob call"
```

---

## Task 7: Wire `finalize_agent` into `apx info`

**Files:**
- Modify: `python/src/apx_agent/cli.py`
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_cli.py
import textwrap
from click.testing import CliRunner
from apx_agent.cli import main


def test_apx_info_lists_config_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(tools=[])\n")
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        module = "agent:agent"
        model = "databricks-claude-sonnet-4-6"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """))
    res = CliRunner().invoke(main, ["info"])
    assert res.exit_code == 0
    assert "ask_sales" in res.output
```

(Confirm the exact `apx info` subcommand name and how it loads the agent — adjust `["info"]` and the pyproject keys to match the real command, e.g. it may need `module`. Inspect `cli.py` `info` command first.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py -k apx_info_lists_config -v`
Expected: FAIL — config tool not listed (info doesn't finalize).

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`'s `info` command, after the agent is loaded (`_load_agent(...)`) and the config is read, and **before** `collect_resource_specs(agent)` / the tool listing, add:

```python
    from ._wiring import finalize_agent

    finalize_agent(agent, config_as_AgentConfig_or_None, pyproject_path=None)
```

(Use the config object the command already has; if it only has the raw dict, pass `config=None` and let `finalize_agent` load it. Match the surrounding variable names.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py -k apx_info_lists_config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_cli.py
git commit -m "feat(cli): apx info finalizes agent so it lists declared tools"
```

---

## Task 8: Integration — serve path card visibility + OBO/resource end-to-end

**Files:**
- Test: `python/tests/test_wiring.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import textwrap
import pytest
from fastapi import FastAPI
from apx_agent import Agent
from apx_agent._wiring import setup_agent


@pytest.mark.asyncio
async def test_setup_agent_serves_config_tools_in_card(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        model = "databricks-claude-sonnet-4-6"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """))
    app = FastAPI()
    agent = Agent(tools=[])
    ctx = await setup_agent(app, agent, pyproject_path=str(tmp_path / "pyproject.toml"))
    # The A2A card snapshot must include the config tool (proves the merge ran
    # before the card was frozen).
    skill_names = [s.name for s in ctx.card.skills]
    assert "ask_sales" in skill_names
```

(Confirm the `AgentCard` field for tools is `.skills` with `.name`; inspect `_models.py` `AgentCard`/`A2ASkill`. Adjust if the attribute differs. Use the existing async-test pattern in `test_wiring.py` — if it uses `anyio`/`asyncio` markers, match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py -k serves_config_tools -v`
Expected: PASS if Task 5 wired `finalize_agent` before the card snapshot; FAIL if the merge runs after. If it fails, the `finalize_agent` call in `setup_agent` is positioned after `collect_tools()` — move it before.

- [ ] **Step 3: Fix positioning if needed**

If the test fails, ensure the `finalize_agent(agent, config, ...)` call in `setup_agent` precedes `tools = agent.collect_tools()` / the `AgentCard(...)` construction. No new code — just ordering.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_wiring.py python/src/apx_agent/_wiring.py
git commit -m "test(wiring): config tools appear in the served A2A card snapshot"
```

---

## Task 9: Public export + full-suite regression

**Files:**
- Modify: `python/src/apx_agent/__init__.py`
- Test: `python/tests/test_tool_config.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_tool_config.py
def test_public_exports():
    import apx_agent
    assert hasattr(apx_agent, "ToolConfigError")
    assert hasattr(apx_agent, "finalize_agent")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_tool_config.py::test_public_exports -v`
Expected: FAIL — names not exported.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/__init__.py`, add near the wiring exports:

```python
from ._tool_config import ToolConfigError, load_config_tools, merge_config_tools
from ._wiring import finalize_agent
```

Add `"ToolConfigError"`, `"finalize_agent"`, `"merge_config_tools"`, `"load_config_tools"` to `__all__` if it exists.

- [ ] **Step 4: Run full regression + typecheck**

Run: `cd python && uv run pytest -q`
Expected: no new failures vs. baseline.
Run: `cd python && uv run pyright src/apx_agent/`
Expected: **0 errors** (the touched non-excluded files — `_tool_config.py`, `_agents.py`, `_wiring.py`, `_chat_agent.py` — must be clean; `cli.py` is excluded but don't add errors).
Run: `cd python && uv run python -c "import apx_agent; print('ok')"`

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/__init__.py python/tests/test_tool_config.py
git commit -m "feat(tools): export ToolConfigError, finalize_agent, merge_config_tools"
```

---

## Self-review notes (author)

- **Spec coverage:** schema/dispatcher → T1; env+allow-list+strict → T2; `_analyzed` rebuild → T3; merge+dedup+composition-skip+idempotent → T4; the `finalize_agent` chokepoint promotion → T5 (serve) + T6 (log/deploy, **the governance fix**) + T7 (info); card-visibility + OBO/resources → T8; exports + regression → T9.
- **The governance regression test** (T6 step 1: config genie tool → `genie_space` resource at log time) is the load-bearing proof that the `log_agent` chokepoint works — it's the test the `sub_agents` precedent never had.
- **Retiring the CLI knob call** (T6) is the behavior reconciliation: `log_agent` now finalizes, so the deploy command must not double-apply (idempotency makes a stray double-call harmless, but removing it is cleaner and the intent).
- **Inspect-before-edit flags** left for the implementer where the exact attribute/command name must be confirmed against current code: `ResourceSpec` field name (T6), `AgentCard.skills`/`A2ASkill.name` (T8), the `apx info` subcommand + agent-load path (T7), the async-test marker style in `test_wiring.py` (T8). These are real lookups, not placeholders — the assertion intent is fully specified.
- **Out of scope (E3):** memory/session/guards config; per-leaf composition-root tool targeting; the `sub_agents`-resources log-path gap (scope Q6 — worth a follow-up but not E2).
- **pyright gate:** `_tool_config.py`, `_agents.py`, `_wiring.py`, `_chat_agent.py` are NOT in the `[tool.pyright].exclude` debt list, so they must stay error-clean — run `uv run pyright src/apx_agent/` before each commit that touches them (this is what blocked the E1 merge until fixed).

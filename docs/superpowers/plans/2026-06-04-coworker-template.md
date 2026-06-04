# Coworker Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `coworker` agent — a pre-grounded `DataAgent` that remembers (facts + session) — as a first-class `CoworkerAgent` class + `CoworkerTemplate`, with a single `memory` knob (`off → inmemory → persistent[default] → lakebase`) that never mandates lakebase and degrades gracefully.

**Architecture:** `CoworkerAgent` subclasses `DataAgent` (reuses #133 grounding), adds an optional `persona` woven into instructions, and translates a `memory` knob into declared `MemoryBackendConfig`/`SessionBackendConfig` carried on the agent. The existing finalize/serve wiring (`attach_declared_memory`, `resolve_session_store`) gains an "agent-carried config" fallback, so memory is wired with the app's `ws` at the right time (preserving #133's no-`ws`-at-boot). `CoworkerTemplate` wraps the class; the scaffold gains `--template coworker`.

**Tech Stack:** Python, Pydantic config models, the existing `@template` registry + memory store wiring. No new deps.

**Spec:** `docs/superpowers/specs/2026-06-04-coworker-template-design.md`

---

## File structure

- `python/src/apx_agent/coworker.py` — **new**: `normalize_memory_knob`, `CoworkerAgent` (subclass of `DataAgent`), `CoworkerTemplate` (`@template`, wraps it).
- `python/src/apx_agent/_schema.py` — `build_instructions_from_schema` gains an optional `persona`.
- `python/src/apx_agent/data_agent.py` — thread `persona` through `_build_data_tools_and_instructions` + `DataAgent.__init__`.
- `python/src/apx_agent/_memory_wiring.py` — `attach_declared_memory` + `resolve_session_store` read agent-carried config when no `AgentConfig` block; record degradation on the agent.
- `python/src/apx_agent/_wiring.py` — pass `agent=ctx.agent` into `resolve_session_store`.
- `python/src/apx_agent/_readyz.py` — add a `memory` capability entry.
- `python/src/apx_agent/__init__.py` — export `CoworkerAgent`, `CoworkerTemplate`.
- `python/src/apx_agent/cli.py` — `--template coworker` scaffold (apps target).
- Tests: `tests/test_coworker.py` (new), `tests/test_schema.py`, `tests/test_data_agent.py`, `tests/test_memory_wiring.py`, `tests/test_readyz.py`, `tests/test_cli.py`.

**Knob vocabulary (locked):** `off`, `inmemory` (alias `local`), `persistent` (alias `delta`, **default**). `lakebase` is **not** a bare-knob value — it needs connection details the one-word knob can't carry, so `memory="lakebase"` raises a clear error directing the user to an explicit `[tool.apx.agent.memory]` block (which the wiring already supports and which overrides the agent-carried config).

---

### Task 1: `normalize_memory_knob` helper

**Files:**
- Create: `python/src/apx_agent/coworker.py`
- Test: `python/tests/test_coworker.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_coworker.py`:
```python
"""Tests for the coworker template — memory knob, CoworkerAgent, CoworkerTemplate."""
from __future__ import annotations

import pytest

from apx_agent.coworker import normalize_memory_knob


class TestNormalizeMemoryKnob:
    def test_off_disables_both(self):
        assert normalize_memory_knob("off") == (None, None)

    def test_inmemory_and_alias_local(self):
        for v in ("inmemory", "local", "InMemory", " LOCAL "):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "inmemory" and sess.type == "inmemory"

    def test_persistent_and_alias_delta_default_tier(self):
        for v in ("persistent", "delta"):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "delta" and sess.type == "delta"

    def test_lakebase_errors_to_explicit_block(self):
        with pytest.raises(ValueError, match="lakebase"):
            normalize_memory_knob("lakebase")

    def test_unknown_value_errors_with_valid_rungs(self):
        with pytest.raises(ValueError, match="off|inmemory|persistent"):
            normalize_memory_knob("sometimes")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_coworker.py::TestNormalizeMemoryKnob -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent.coworker'`

- [ ] **Step 3: Implement**

Create `python/src/apx_agent/coworker.py`:
```python
"""Coworker — a pre-grounded DataAgent that remembers (facts + session).

``CoworkerAgent`` is a ``DataAgent`` subclass that adds an optional persona and a
single ``memory`` knob; ``CoworkerTemplate`` wraps it for template-as-config.
Memory is carried as *declared config* (``memory_config`` / ``session_config``)
and wired by the framework's finalize/serve path with the app workspace client —
so construction needs no ``ws`` (same property as DataAgent grounding).
"""

from __future__ import annotations

from ._models import MemoryBackendConfig, SessionBackendConfig

# Bare-knob rungs → backend StoreType. ``lakebase`` is intentionally absent: it
# needs connection details the one-word knob can't express (see normalize).
_KNOB_TO_TYPE: dict[str, str] = {
    "off": "",            # sentinel: disabled
    "inmemory": "inmemory",
    "local": "inmemory",
    "persistent": "delta",
    "delta": "delta",
}


def normalize_memory_knob(
    value: str,
) -> "tuple[MemoryBackendConfig | None, SessionBackendConfig | None]":
    """Map the coworker ``memory`` knob to ``(MemoryBackendConfig,
    SessionBackendConfig)`` for the facts + session subsystems (same tier).

    Returns ``(None, None)`` for ``"off"``. Raises ``ValueError`` for
    ``"lakebase"`` (needs an explicit ``[tool.apx.agent.memory]`` block) and for
    any unknown value.
    """
    v = (value or "").strip().lower()
    if v == "lakebase":
        raise ValueError(
            "memory='lakebase' needs connection details the one-word knob can't "
            "carry — add explicit [tool.apx.agent.memory] and "
            "[tool.apx.agent.session] blocks with type='lakebase' "
            "(host, database, embedding_model, embedding_dim)."
        )
    if v not in _KNOB_TO_TYPE:
        raise ValueError(
            f"memory={value!r} is not a valid tier; use one of: off, inmemory "
            "(alias local), persistent (alias delta), or an explicit "
            "[tool.apx.agent.memory] block for lakebase."
        )
    tier = _KNOB_TO_TYPE[v]
    if not tier:  # "off"
        return (None, None)
    return (MemoryBackendConfig(type=tier), SessionBackendConfig(type=tier))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_coworker.py::TestNormalizeMemoryKnob -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/coworker.py tests/test_coworker.py
git commit -m "feat(coworker): memory knob normalization (off/inmemory/persistent)"
```

---

### Task 2: Persona woven into grounded instructions

**Files:**
- Modify: `python/src/apx_agent/_schema.py` (`build_instructions_from_schema`)
- Modify: `python/src/apx_agent/data_agent.py` (`_build_data_tools_and_instructions`, `DataAgent.__init__`)
- Test: `python/tests/test_schema.py`, `python/tests/test_data_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_schema.py`:
```python
class TestPersona:
    def test_persona_leads_grounded_instructions(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"customer": ["c_custkey(bigint)", "c_name(string)"]}
        out = build_instructions_from_schema("samples", "tpch", tables,
                                             persona="a revenue analyst")
        assert out.startswith("You are a revenue analyst.")
        # grounding is intact
        assert "customer" in out and "c_custkey(bigint)" in out
        assert "SHOW TABLES" in out

    def test_no_persona_keeps_default_lead(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch",
                                             {"customer": ["c_custkey(bigint)"]})
        assert out.startswith("You are a data assistant for samples.tpch.")

    def test_persona_on_ungrounded_too(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch", {},
                                             persona="a revenue analyst")
        assert out.startswith("You are a revenue analyst.")
        assert "confirm what tables and columns are available" in out  # still discovers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_schema.py::TestPersona -q`
Expected: FAIL — `build_instructions_from_schema() got an unexpected keyword argument 'persona'`

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_schema.py`, change the `build_instructions_from_schema` signature to add `persona` and prepend the persona lead. Replace the signature line:
```python
def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
) -> str:
```
with:
```python
def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
    persona: str | None = None,
) -> str:
```
At the very top of the function body (right after the docstring, before `fqn = ...`), add:
```python
    lead = f"You are {persona}. " if persona else ""
```
Then prepend `lead` to **both** return strings: change the ungrounded branch's
`return (\n            f"You are a data assistant for {fqn}. ...` to
`return (\n            lead + f"You are a data assistant for {fqn}. ...` and the
grounded branch's `return (\n        f"You are a data assistant for {fqn}. You already know ...` to
`return (\n        lead + f"You are a data assistant for {fqn}. You already know ...`.

(Result: with persona, instructions read `"You are {persona}. You are a data assistant for {fqn}. …"` — persona colors the role; grounding is unchanged. The test asserts `startswith("You are a revenue analyst.")`, which the leading `lead` satisfies.)

In `python/src/apx_agent/data_agent.py`, thread `persona` through. In `_build_data_tools_and_instructions`, add `persona: str | None,` to the signature (place it right after `instructions: str | None,`). Then change the instruction-building call:
```python
    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables
    )
```
to:
```python
    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables, persona=persona
    )
```
In `DataAgent.__init__`, add `persona: str | None = None,` to the signature (right after `instructions: str | None = None,`) and pass it into the builder call — add `persona=persona,` to the `_build_data_tools_and_instructions(...)` kwargs. Add a docstring Args line after the `instructions:` entry:
```
        persona: Optional role phrase woven into the schema-generated
            instructions ("You are {persona}. …"). Ignored when ``instructions``
            is given explicitly.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_schema.py tests/test_data_agent.py -q`
Expected: PASS (new TestPersona + existing).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_schema.py src/apx_agent/data_agent.py tests/test_schema.py
git commit -m "feat(data-agent): optional persona woven into grounded instructions"
```

---

### Task 3: `CoworkerAgent` class

**Files:**
- Modify: `python/src/apx_agent/coworker.py`
- Test: `python/tests/test_coworker.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_coworker.py`:
```python
class TestCoworkerAgent:
    def test_is_data_agent_with_persona_and_memory_config(self):
        from apx_agent.coworker import CoworkerAgent
        from apx_agent import DataAgent
        cw = CoworkerAgent(
            "samples", "tpch",
            persona="a revenue analyst",
            memory="persistent",
            tables={"customer": ["c_custkey(bigint)"]},
        )
        assert isinstance(cw, DataAgent)
        # persona + grounding in the instructions
        assert cw._instructions.startswith("You are a revenue analyst.")
        assert "c_custkey(bigint)" in cw._instructions
        # memory declared (not yet built — needs ws at wiring time)
        assert cw.memory_config is not None and cw.memory_config.type == "delta"
        assert cw.session_config is not None and cw.session_config.type == "delta"

    def test_memory_off_declares_nothing(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", memory="off",
                           tables={"t": ["a(int)"]})
        assert cw.memory_config is None and cw.session_config is None

    def test_default_memory_is_persistent(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", tables={"t": ["a(int)"]})
        assert cw.memory_config.type == "delta"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_coworker.py::TestCoworkerAgent -q`
Expected: FAIL — `cannot import name 'CoworkerAgent'`

- [ ] **Step 3: Implement**

In `python/src/apx_agent/coworker.py`, add the imports and the class (after `normalize_memory_knob`):
```python
from typing import Any

from .data_agent import DataAgent
from ._template import template
from pydantic import BaseModel, ConfigDict, Field


class CoworkerAgent(DataAgent):
    """A pre-grounded ``DataAgent`` that remembers — persona + memory.

    Adds an optional ``persona`` (woven into the grounded instructions) and a
    single ``memory`` knob covering facts + session. Memory is declared as
    ``memory_config`` / ``session_config`` and wired by the framework's
    finalize/serve path with the app workspace client (so no ``ws`` is needed at
    construction). Composes like any agent: directly, as a ``sub_agent``, or as a
    leaf in a ``SequentialAgent`` / ``RouterAgent``.

    Args:
        memory: Memory tier knob — ``"off"``, ``"inmemory"`` (alias ``"local"``),
            ``"persistent"`` (alias ``"delta"``, the default). For ``lakebase``,
            use explicit ``[tool.apx.agent.memory]`` / ``.session`` blocks.
        persona: Optional role phrase (see ``DataAgent``).
        (All other args are ``DataAgent``'s.)
    """

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        persona: str | None = None,
        memory: str = "persistent",
        **kwargs: Any,
    ) -> None:
        super().__init__(catalog, schema, persona=persona, **kwargs)
        self.memory_config, self.session_config = normalize_memory_knob(memory)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_coworker.py -q`
Expected: PASS (TestNormalizeMemoryKnob + TestCoworkerAgent).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/coworker.py tests/test_coworker.py
git commit -m "feat(coworker): CoworkerAgent (DataAgent + persona + memory knob)"
```

---

### Task 4: Agent-carried config precedence in the wiring

**Files:**
- Modify: `python/src/apx_agent/_memory_wiring.py` (`attach_declared_memory`, `resolve_session_store`)
- Modify: `python/src/apx_agent/_wiring.py:744` (pass `agent=`)
- Test: `python/tests/test_memory_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_memory_wiring.py`:
```python
class TestAgentCarriedConfig:
    def _agent(self):
        # Minimal leaf agent with the registration hooks attach_declared_memory needs.
        from apx_agent import Agent
        return Agent(instructions="x", tools=[])

    def test_attach_uses_agent_memory_config_when_no_block(self):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._models import AgentConfig, MemoryBackendConfig
        agent = self._agent()
        agent.memory_config = MemoryBackendConfig(type="inmemory")  # carried
        cfg = AgentConfig(name="c", description="d")                # no memory block
        attach_declared_memory(agent, cfg, ws=None)
        names = {getattr(fn, "__name__", "") for fn in getattr(agent, "_tool_fns", [])}
        assert any("recall" in n or "remember" in n for n in names)

    def test_explicit_block_overrides_agent_config(self):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._models import AgentConfig, MemoryBackendConfig
        agent = self._agent()
        agent.memory_config = MemoryBackendConfig(type="delta")     # carried (would need ws)
        cfg = AgentConfig(name="c", description="d",
                          memory=MemoryBackendConfig(type="inmemory"))  # explicit wins
        attach_declared_memory(agent, cfg, ws=None)
        names = {getattr(fn, "__name__", "") for fn in getattr(agent, "_tool_fns", [])}
        assert any("recall" in n or "remember" in n for n in names)  # inmemory built (no ws needed)

    def test_resolve_session_uses_agent_config_when_no_block(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig
        agent = self._agent()
        agent.session_config = SessionBackendConfig(type="inmemory")
        cfg = AgentConfig(name="c", description="d")
        store = resolve_session_store(cfg, ws=None, agent=agent)
        assert store is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestAgentCarriedConfig -q`
Expected: FAIL — wiring ignores `agent.memory_config`; `resolve_session_store` has no `agent` param.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_memory_wiring.py`, in `attach_declared_memory`, replace the memory-block opener:
```python
    # --- memory ---
    if config.memory is not None:
        mcfg = config.memory
```
with (fall back to the agent's carried config; record degradation for /readyz):
```python
    # --- memory ---
    # Explicit [tool.apx.agent.memory] block wins; else use the agent-carried
    # config (e.g. CoworkerAgent.memory_config). The framework supplies ``ws``.
    mcfg = config.memory if config.memory is not None else getattr(agent, "memory_config", None)
    if mcfg is not None:
```
Inside that block, in the existing `if store is None and ws is None and mcfg.type in ("lakebase", "delta"):` branch, add a line recording the degradation on the agent (after the existing `logger.warning(...)`):
```python
            setattr(agent, "_apx_memory_degraded",
                    f"{mcfg.type} memory needs a workspace/warehouse — not active")
```

Still in `_memory_wiring.py`, update `resolve_session_store` to accept + use `agent`:
```python
def resolve_session_store(
    config: "AgentConfig",
    ws: Any | None,
    override: Any | None = None,
    agent: Any | None = None,
) -> Any | None:
    """Return a SessionStore for this agent, or None.

    Precedence: explicit ``override`` arg > config ``session`` block >
    agent-carried ``session_config`` (e.g. CoworkerAgent) > None.
    """
    if override is not None:
        return override
    scfg = config.session if config.session is not None else getattr(agent, "session_config", None)
    if scfg is None:
        return None
    try:
        return _build_session_store(scfg, ws)
    except (ValueError, ImportError) as exc:
        logger.warning(
            "[tool.apx.agent.session] build failed — no session store: %s", exc
        )
        return None
```

In `python/src/apx_agent/_wiring.py` at the `resolve_session_store(` call (~line 744), add the agent argument:
```python
                    session_store=resolve_session_store(
                        ctx.config,
                        ws=app.state.workspace_client,
                        override=session_store,
                        agent=ctx.agent,
                    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_memory_wiring.py -q`
Expected: PASS (new class + existing).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_memory_wiring.py src/apx_agent/_wiring.py tests/test_memory_wiring.py
git commit -m "feat(memory): wire agent-carried memory/session config (CoworkerAgent)"
```

---

### Task 5: `CoworkerTemplate` + exports

**Files:**
- Modify: `python/src/apx_agent/coworker.py` (add `CoworkerTemplate`)
- Modify: `python/src/apx_agent/__init__.py` (export)
- Test: `python/tests/test_coworker.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_coworker.py`:
```python
class TestCoworkerTemplate:
    def test_registered_and_builds_coworker_agent(self):
        from apx_agent._template import template_registry
        from apx_agent.coworker import CoworkerAgent
        tmpl = template_registry.get("coworker")
        spec = tmpl.Spec(catalog="samples", schema="tpch",
                         persona="a revenue analyst", memory="persistent")
        agent = tmpl.build(spec, ws=None)
        assert isinstance(agent, CoworkerAgent)
        assert agent.memory_config.type == "delta"
        assert agent._instructions.startswith("You are a revenue analyst.")

    def test_data_template_still_resolves(self):
        from apx_agent._template import template_registry
        assert template_registry.get("data") is not None

    def test_exported_from_package(self):
        import apx_agent
        assert hasattr(apx_agent, "CoworkerAgent")
        assert hasattr(apx_agent, "CoworkerTemplate")
```

(If `template_registry.get(name)` is not the accessor, use the same lookup `test_*` uses for `DataTemplate` — check `tests/` for the existing pattern and mirror it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_coworker.py::TestCoworkerTemplate -q`
Expected: FAIL — no `coworker` in registry / no package export.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/coworker.py`, append the template (mirrors `DataTemplate` in `data_agent.py`):
```python
@template
class CoworkerTemplate:
    """A pre-grounded data agent that remembers (facts + session); memory
    upgradeable off → inmemory → persistent → lakebase. Wraps ``CoworkerAgent``."""

    name = "coworker"
    title = "Coworker"
    description = (
        "A pre-grounded data agent that remembers (facts + session); "
        "memory upgradeable off → inmemory → persistent → lakebase."
    )

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")  # 'schema' in config dicts
        warehouse_id: str | None = None
        persona: str | None = None
        memory: str = "persistent"
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec: "CoworkerTemplate.Spec", *, ws: Any | None = None) -> CoworkerAgent:
        return CoworkerAgent(
            spec.catalog,
            spec.schema_name,
            persona=spec.persona,
            memory=spec.memory,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
        )
```

In `python/src/apx_agent/__init__.py`, mirror the `data_agent` export. After the line `from .data_agent import DataAgent, DataTemplate` add:
```python
from .coworker import CoworkerAgent, CoworkerTemplate
```
and add `"CoworkerAgent",` and `"CoworkerTemplate",` to `__all__` next to the `"DataAgent"` / `"DataTemplate"` entries.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_coworker.py -q`
Expected: PASS (all three classes).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/coworker.py src/apx_agent/__init__.py tests/test_coworker.py
git commit -m "feat(coworker): CoworkerTemplate (name=coworker) + package exports"
```

---

### Task 6: `/readyz` memory capability

**Files:**
- Modify: `python/src/apx_agent/_readyz.py` (`readyz` handler, ~line 164)
- Test: `python/tests/test_readyz.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_readyz.py`:
```python
class TestReadyzMemory:
    def _app_for(self, agent):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apx_agent._readyz import mount_readyz
        import apx_agent._readyz as rz
        # Stub the canned probe so the test doesn't need a real model.
        rz._run_canned_probe = lambda a, m: ("hi", "tr-1")  # type: ignore
        app = FastAPI()
        mount_readyz(app, agent)
        return TestClient(app)

    def test_memory_degraded_surfaced(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        agent._apx_memory_degraded = "delta memory needs a workspace/warehouse — not active"
        body = self._app_for(agent).get("/readyz").json()
        assert "delta memory needs" in body["checks"]["memory"]

    def test_memory_ok_when_not_degraded(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        body = self._app_for(agent).get("/readyz").json()
        assert body["checks"]["memory"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_readyz.py::TestReadyzMemory -q`
Expected: FAIL — `KeyError: 'memory'`.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_readyz.py`, in the `readyz()` handler, add a `memory` entry to the initial `checks` dict:
```python
        checks: dict[str, Any] = {
            "llm": "fail",
            "tracing": "unavailable",
            "tools_registered": _count_tools(agent),
            "tool_exec": "skipped",
            "memory": getattr(agent, "_apx_memory_degraded", None) or "ok",
        }
```
`memory` is informational and does **not** flip readiness (a coworker whose
persistent store isn't reachable is healthy-but-not-remembering): leave the
`ready = ...` computation unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_readyz.py -q`
Expected: PASS (new class + existing).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_readyz.py tests/test_readyz.py
git commit -m "feat(readyz): surface memory degradation as a capability entry"
```

---

### Task 7: Scaffold `--template coworker` (apps target)

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`scaffold` command + `_scaffold_apps`)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:
```python
class TestScaffoldCoworker:
    def test_apps_coworker_agent_py(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)  # skip introspection
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="coworker")
        agent_py = (tmp_path / "agent.py").read_text()
        assert "CoworkerAgent(" in agent_py
        assert 'memory="persistent"' in agent_py
        assert "lakebase" in agent_py            # the upgrade-ladder comment
        assert "DataAgent(" not in agent_py

    def test_apps_default_is_data_agent(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="data")
        assert "DataAgent(" in (tmp_path / "agent.py").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py::TestScaffoldCoworker -q`
Expected: FAIL — `_scaffold_apps()` has no `template` parameter.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/cli.py`, add a coworker agent template constant near `_SCAFFOLD_APPS_AGENT`:
```python
_SCAFFOLD_APPS_AGENT_COWORKER = '''\
"""<APP_NAME> — apx-agent coworker (pre-grounded data agent that remembers)."""
from __future__ import annotations

from apx_agent import CoworkerAgent

<EXAMPLE_TOOL>
# A coworker over ``<CATALOG>.<SCHEMA>``: pre-grounded in the schema (it already
# knows the tables/columns) AND remembers across turns (facts + session).
#
# Memory upgrade path — no Lakebase required by default:
#   memory="off"        # stateless
#   memory="inmemory"   # zero infra, forgets on restart
#   memory="persistent" # (default) UC Delta tables — survives restart
#   memory="lakebase"   # production pgvector — use explicit
#                       # [tool.apx.agent.memory]/[.session] type="lakebase" blocks
agent = CoworkerAgent("<CATALOG>", "<SCHEMA>"<EXTRA_TOOLS>, memory="persistent", name="<APP_NAME>")
'''
```
In `_scaffold_apps`, add a `template: str = "data"` parameter to the signature, and choose the agent template:
```python
def _scaffold_apps(
    target: Path, name: str, force: bool, catalog: str, schema: str,
    table: str | None = None, template: str = "data",
) -> None:
```
Where the `files` dict sets `"agent.py"`, pick the body by template:
```python
        "agent.py": _sub(
            _SCAFFOLD_APPS_AGENT_COWORKER if template == "coworker"
            else _SCAFFOLD_APPS_AGENT
        ),
```
Add the CLI option to the `scaffold` command (next to `--target`):
```python
@click.option(
    "--template", "scaffold_template",
    type=click.Choice(["data", "coworker"]),
    default="data",
    show_default=True,
    help="Agent kind: 'data' (pre-grounded SQL agent) or 'coworker' "
         "(pre-grounded + memory). Apps target only for coworker.",
)
```
Add `scaffold_template: str` to the `scaffold(...)` function signature, and pass it through to the apps scaffold call (`_scaffold_apps(target, project_name, force, catalog, schema, table)` → add `template=scaffold_template`). If `scaffold_target == "model-serving"` and `scaffold_template == "coworker"`, raise a clear `click.ClickException("--template coworker requires --target apps (model-serving coworker scaffold is a follow-up).")` before scaffolding.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py::TestScaffoldCoworker -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(scaffold): apx scaffold --template coworker (apps)"
```

---

### Task 8: Gate — full suite + types

- [ ] **Step 1:** `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] **Step 2:** `cd python && uv run pytest -q` — all pass (~1883 + new ~20).
- [ ] **Step 3:** `cd python && uv run pyright src/apx_agent` — 0 errors (the pre-existing `_tool.py:138` warning is acceptable).
- [ ] **Step 4:** `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only intended files; `uv.lock` clean.

---

## Self-review

**Spec coverage:**
- `memory` knob (off/inmemory/persistent, lakebase→explicit, default persistent) → **Task 1** (`normalize_memory_knob`).
- `CoworkerAgent` first-class class (subclass DataAgent, composes) → **Task 3**.
- Persona replaces/leads instructions → **Task 2**.
- Memory carried as declared config, wired by framework with app `ws`; explicit block > agent-carried > none → **Tasks 3 + 4**.
- Session via agent-carried precedence → **Task 4**.
- `CoworkerTemplate` wraps the class; registers by name; data still resolves → **Task 5**.
- Graceful degradation surfaced (log already exists; recorded on agent + `/readyz`) → **Tasks 4 + 6**.
- Scaffold `--template coworker` + ladder comment + `.apx/schema.json` (reused) → **Task 7**.
- Exports → **Task 5**.

**Placeholder scan:** No `TBD`/`TODO`/"add error handling". Every code step shows complete code. Task 5's note to "mirror the existing registry lookup" is a verify-on-execute, with the concrete `template_registry.get("coworker")` form given.

**Type/name consistency:** `normalize_memory_knob` (Task 1) → used in `CoworkerAgent.__init__` (Task 3); `memory_config`/`session_config` set in Task 3 → read in Task 4 (`attach_declared_memory`, `resolve_session_store`) and Task 6 (`_apx_memory_degraded`); `persona` arg threads `_schema` → `data_agent` → `coworker` (Tasks 2, 3); `CoworkerTemplate.build` returns `CoworkerAgent` (Task 5). Knob vocabulary identical across Task 1, Task 7 ladder comment, and the spec.

**Verify-on-execute:** confirm `template_registry`'s public accessor name (`get`?) by checking how `DataTemplate` is looked up in existing tests; confirm `Agent(instructions=, tools=)` is the right minimal leaf for the wiring/readyz tests (it must expose `_register_tool` / `_tool_fns` — Task 4's test relies on it); confirm the `_scaffold_apps` call site passes positional args in the order the new `template=` kwarg expects.

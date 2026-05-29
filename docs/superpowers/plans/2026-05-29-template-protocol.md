# E1 · Template Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the `DataAgent` pattern into a first-class, registrable `Template` abstraction (`spec → tools + grounded instructions → LlmAgent`), with `DataAgent` retrofitted as the reference implementation, fully backward compatible.

**Architecture:** A new `_template.py` defines a `Template` Protocol, a `@template` decorator, a registry (in-process + entry-point discovery), and `TemplateInfo`. `DataAgent` keeps its class identity (topology depends on the class name) but shares one builder helper with a new `DataTemplate`. Persona (model/instructions/knobs) stays in the `[tool.apx.agent]` envelope and is layered onto the built agent via the existing `apply_config_knobs` seam in `_wiring.py`, with instructions now *composed* (grounding base + persona overlay) rather than replaced.

**Tech Stack:** Python 3.11+, Pydantic v2, `importlib.metadata` entry points, pytest. Tests run from `python/` via `uv run pytest` (the repo root `.venv` shadows `src/` — always run from `python/`).

**Spec:** `docs/superpowers/specs/2026-05-29-template-protocol-design.md`

---

## File structure

- **Create** `python/src/apx_agent/_template.py` — `Template` protocol, `TemplateInfo`, `@template` decorator, `TemplateRegistry`, module-level `template_registry`, entry-point discovery. One responsibility: the template abstraction + registry.
- **Modify** `python/src/apx_agent/data_agent.py` — extract a shared `_build_data_tools_and_instructions(...)` helper; add `DataTemplate(Template)`; keep `DataAgent(LlmAgent)` delegating to the helper.
- **Modify** `python/src/apx_agent/_prompt_assembly.py` — add `compose_instructions(base, overlay)`.
- **Modify** `python/src/apx_agent/_wiring.py` — extend `apply_config_knobs` to overlay `config.instructions` onto `agent._instructions` (compose when both present; idempotent via sentinel).
- **Modify** `python/src/apx_agent/__init__.py` — export `Template`, `TemplateInfo`, `template`, `template_registry`, `DataTemplate`.
- **Create** `python/tests/test_template.py` — protocol/registry/discovery tests.
- **Modify** `python/tests/test_data_agent.py` — add DataTemplate parity tests.
- **Modify** `python/tests/test_wiring.py` — add instruction-overlay tests.
- **Modify** `python/tests/test_prompt_assembly.py` — add `compose_instructions` tests.

---

## Task 1: `Template` protocol + `TemplateInfo`

**Files:**
- Create: `python/src/apx_agent/_template.py`
- Test: `python/tests/test_template.py`

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_template.py
from pydantic import BaseModel
from apx_agent._template import Template, TemplateInfo


class _DummySpec(BaseModel):
    x: int = 1


class _DummyTemplate:
    name = "dummy"
    title = "Dummy"
    description = "A dummy template."
    Spec = _DummySpec

    def build(self, spec, *, ws=None):
        return ("agent", spec)


def test_template_info_carries_catalog_fields():
    info = TemplateInfo.from_template(_DummyTemplate())
    assert info.name == "dummy"
    assert info.title == "Dummy"
    assert info.description == "A dummy template."
    # Spec schema is exported as a JSON-schema dict for catalog UIs.
    assert info.spec_schema["properties"]["x"]["default"] == 1


def test_dummy_conforms_to_protocol():
    assert isinstance(_DummyTemplate(), Template)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apx_agent._template'`

- [ ] **Step 3: Write minimal implementation**

```python
# python/src/apx_agent/_template.py
"""Template protocol + registry — the agent-as-config foundation (E1).

A Template turns a small typed spec into a configured leaf agent: it wires
governed tools and produces *grounded* instructions for a role. ``DataAgent``
is the reference implementation. Persona (model, instruction tone, generation
knobs) is NOT a template's job — it stays in the ``[tool.apx.agent]`` envelope
and is layered on afterward via ``apply_config_knobs`` (``_wiring.py``).

Built-in templates register via the ``@template`` decorator at import time.
Third-party / cross-repo templates auto-register via the ``apx_agent.templates``
entry-point group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class Template(Protocol):
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    Spec: ClassVar[type[BaseModel]]

    def build(self, spec: BaseModel, *, ws: Any | None = None) -> Any: ...


@dataclass(frozen=True)
class TemplateInfo:
    """Catalog-facing metadata for a template — no template code needed to render."""

    name: str
    title: str
    description: str
    spec_schema: dict[str, Any]

    @classmethod
    def from_template(cls, tmpl: Template) -> "TemplateInfo":
        return cls(
            name=tmpl.name,
            title=tmpl.title,
            description=tmpl.description,
            spec_schema=tmpl.Spec.model_json_schema(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_template.py python/tests/test_template.py
git commit -m "feat(template): add Template protocol + TemplateInfo"
```

---

## Task 2: Registry + `@template` decorator

**Files:**
- Modify: `python/src/apx_agent/_template.py`
- Test: `python/tests/test_template.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_template.py
import pytest
from apx_agent._template import TemplateRegistry, template


def _fresh_registry():
    return TemplateRegistry()


def test_register_get_build_with_dict_and_instance():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    assert reg.get("dummy").name == "dummy"
    # build accepts a raw dict (validated against Spec)...
    out_kind, spec = reg.build("dummy", {"x": 7})
    assert out_kind == "agent" and spec.x == 7
    # ...and an already-built Spec instance.
    out_kind, spec2 = reg.build("dummy", _DummySpec(x=9))
    assert spec2.x == 9


def test_list_returns_template_info():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    infos = reg.list()
    assert [i.name for i in infos] == ["dummy"]
    assert infos[0].spec_schema["properties"]["x"]["default"] == 1


def test_unknown_name_raises_listing_available():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    with pytest.raises(ValueError, match="dummy"):
        reg.get("nope")


def test_duplicate_registration_raises():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_DummyTemplate)


def test_decorator_registers_on_module_registry():
    from apx_agent._template import template_registry

    @template
    class _DecoratedTemplate:
        name = "decorated_test"
        title = "Decorated"
        description = "via decorator"
        Spec = _DummySpec

        def build(self, spec, *, ws=None):
            return spec

    try:
        assert template_registry.get("decorated_test").name == "decorated_test"
    finally:
        template_registry._templates.pop("decorated_test", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: FAIL with `ImportError: cannot import name 'TemplateRegistry'`

- [ ] **Step 3: Write minimal implementation**

Add to `python/src/apx_agent/_template.py`:

```python
import logging

logger = logging.getLogger(__name__)


class TemplateRegistry:
    """Name → Template registry. Built-ins via @template; third-party via entry points."""

    ENTRY_POINT_GROUP: ClassVar[str] = "apx_agent.templates"

    def __init__(self) -> None:
        self._templates: dict[str, Template] = {}
        self._discovered = False

    def register(self, tmpl_cls: type[Template]) -> type[Template]:
        inst = tmpl_cls()
        name = inst.name
        if name in self._templates:
            raise ValueError(
                f"Template {name!r} already registered "
                f"(by {type(self._templates[name]).__module__})."
            )
        self._templates[name] = inst
        return tmpl_cls

    def get(self, name: str) -> Template:
        self._ensure_discovered()
        if name not in self._templates:
            available = ", ".join(sorted(self._templates)) or "(none)"
            raise ValueError(f"Unknown template {name!r}. Available: {available}.")
        return self._templates[name]

    def list(self) -> list[TemplateInfo]:
        self._ensure_discovered()
        return [TemplateInfo.from_template(t) for t in self._templates.values()]

    def build(self, name: str, spec: "dict | BaseModel", *, ws: Any = None) -> Any:
        tmpl = self.get(name)
        validated = spec if isinstance(spec, BaseModel) else tmpl.Spec.model_validate(spec)
        return tmpl.build(validated, ws=ws)

    def _ensure_discovered(self) -> None:
        if self._discovered:
            return
        self._discovered = True  # set first so a failure doesn't retry-loop
        self._load_entry_points()

    def _load_entry_points(self) -> None:
        from importlib.metadata import entry_points

        try:
            eps = entry_points(group=self.ENTRY_POINT_GROUP)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Template entry-point discovery failed: %s", e)
            return
        for ep in eps:
            try:
                self.register(ep.load())
            except Exception as e:
                logger.warning("Skipping bad template entry point %r: %s", ep.name, e)


template_registry = TemplateRegistry()


def template(tmpl_cls: type[Template]) -> type[Template]:
    """Decorator: register a Template class on the module-level registry."""
    return template_registry.register(tmpl_cls)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: PASS (all template tests)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_template.py python/tests/test_template.py
git commit -m "feat(template): registry + @template decorator with dict-spec build"
```

---

## Task 3: Entry-point discovery (broken-endpoint resilience)

**Files:**
- Test: `python/tests/test_template.py`

Note: discovery code already exists from Task 2; this task locks its behavior with tests for the two paths that matter (a good entry point loads; a broken one is skipped, not fatal).

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_template.py
from importlib.metadata import EntryPoint


class _GoodEP:
    name = "good"
    def load(self):
        class _GoodTemplate:
            name = "good_ep"
            title = "Good EP"
            description = "loaded via entry point"
            Spec = _DummySpec
            def build(self, spec, *, ws=None):
                return spec
        return _GoodTemplate


class _BadEP:
    name = "bad"
    def load(self):
        raise ImportError("boom")


def test_entry_point_discovery_loads_good_skips_bad(monkeypatch):
    import apx_agent._template as mod

    def fake_entry_points(*, group):
        assert group == "apx_agent.templates"
        return [_GoodEP(), _BadEP()]

    monkeypatch.setattr(mod, "entry_points", fake_entry_points, raising=False)
    # Patch the name the registry imports locally:
    monkeypatch.setattr(
        "importlib.metadata.entry_points", fake_entry_points, raising=True
    )

    reg = mod.TemplateRegistry()
    infos = {i.name for i in reg.list()}  # triggers discovery
    assert "good_ep" in infos          # good one registered
    # bad one skipped without raising — list() returned normally
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_template.py::test_entry_point_discovery_loads_good_skips_bad -v`
Expected: FAIL (if the monkeypatch target is wrong, fix the patch target to match the `from importlib.metadata import entry_points` inside `_load_entry_points`). The import is local to the method, so patch `importlib.metadata.entry_points`.

- [ ] **Step 3: Adjust implementation if needed**

No code change expected — `_load_entry_points` already imports `entry_points` locally and wraps each `.load()` in try/except. If the test reveals the local import can't be patched, refactor to a module-level `from importlib.metadata import entry_points` import at top of `_template.py` and call `entry_points(...)`, so the test can `monkeypatch.setattr(mod, "entry_points", ...)`.

```python
# At top of _template.py, replace the local import with module-level:
from importlib.metadata import entry_points
# ...and in _load_entry_points, drop the inner import and call entry_points(group=...) directly.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_template.py python/tests/test_template.py
git commit -m "test(template): lock entry-point discovery (load good, skip broken)"
```

---

## Task 4: `compose_instructions` helper

**Files:**
- Modify: `python/src/apx_agent/_prompt_assembly.py`
- Test: `python/tests/test_prompt_assembly.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_prompt_assembly.py
from apx_agent._prompt_assembly import compose_instructions


def test_compose_overlays_persona_above_grounding():
    out = compose_instructions(base="GROUNDING", overlay="PERSONA")
    # Persona first, then a separator, then grounding.
    assert out.index("PERSONA") < out.index("GROUNDING")
    assert "PERSONA" in out and "GROUNDING" in out


def test_compose_returns_single_side_when_other_empty():
    assert compose_instructions(base="GROUNDING", overlay="") == "GROUNDING"
    assert compose_instructions(base="", overlay="PERSONA") == "PERSONA"
    assert compose_instructions(base="  ", overlay=None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_prompt_assembly.py -k compose -v`
Expected: FAIL with `ImportError: cannot import name 'compose_instructions'`

- [ ] **Step 3: Write minimal implementation**

Add to `python/src/apx_agent/_prompt_assembly.py`:

```python
def compose_instructions(*, base: str | None, overlay: str | None) -> str:
    """Compose a persona ``overlay`` above a grounded ``base`` system prompt.

    Used when a template produced grounded instructions (``base``) AND the
    config envelope supplied persona instructions (``overlay``). The overlay
    goes first so tone/role framing precedes the concrete grounding. When only
    one side is non-empty, that side is returned verbatim (fill semantics).
    """
    base_s = (base or "").strip()
    overlay_s = (overlay or "").strip()
    if base_s and overlay_s:
        return f"{overlay_s}\n\n---\n\n{base_s}"
    return overlay_s or base_s
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_prompt_assembly.py -k compose -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_prompt_assembly.py python/tests/test_prompt_assembly.py
git commit -m "feat(prompt): compose_instructions (persona overlay + grounding base)"
```

---

## Task 5: Persona instruction overlay in `apply_config_knobs`

**Files:**
- Modify: `python/src/apx_agent/_wiring.py` (inside `apply_config_knobs`)
- Test: `python/tests/test_wiring.py`

This extends the seam shipped in `apply_config_knobs`. New behavior: when the
agent already has grounded instructions AND the envelope sets instructions,
*compose* them; when only one side is present, keep current fill behavior; make
it idempotent so `setup_agent` running twice (it can, via `mount_mcp_endpoints`)
doesn't double-overlay.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py (top imports may already cover these)
from apx_agent import Agent, AgentConfig
from apx_agent._wiring import apply_config_knobs


def _cfg(**kw):
    return AgentConfig(name="t", **kw)


def test_instructions_compose_when_both_present():
    agent = Agent(tools=[], instructions="GROUNDING")
    apply_config_knobs(agent, _cfg(instructions="PERSONA"))
    assert agent._instructions.index("PERSONA") < agent._instructions.index("GROUNDING")


def test_instructions_fill_when_agent_empty():
    agent = Agent(tools=[], instructions="")
    apply_config_knobs(agent, _cfg(instructions="PERSONA"))
    assert agent._instructions == "PERSONA"


def test_plain_agent_no_envelope_instructions_unchanged():
    # Regression: matches behavior shipped with the knob fix — no compose when
    # there's nothing to compose with.
    agent = Agent(tools=[], instructions="GROUNDING")
    apply_config_knobs(agent, _cfg(instructions=""))
    assert agent._instructions == "GROUNDING"


def test_instruction_overlay_is_idempotent():
    agent = Agent(tools=[], instructions="GROUNDING")
    cfg = _cfg(instructions="PERSONA")
    apply_config_knobs(agent, cfg)
    once = agent._instructions
    apply_config_knobs(agent, cfg)  # second call (e.g. mount_mcp_endpoints)
    assert agent._instructions == once  # no double overlay
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py -k "instructions or overlay or plain_agent" -v`
Expected: FAIL — `test_instructions_compose_when_both_present` fails (instructions untouched today).

- [ ] **Step 3: Write minimal implementation**

In `python/src/apx_agent/_wiring.py`, add to the top of the file:

```python
from ._prompt_assembly import compose_instructions
```

Then inside `apply_config_knobs`, after the existing knob loop, add:

```python
    # Persona instruction overlay. The compile path reads ``agent._instructions``
    # as the system prompt. A template may have set grounded instructions; the
    # envelope may carry persona instructions. Compose (overlay above grounding)
    # when both are present; otherwise fill. Idempotent via a sentinel so a
    # second call (e.g. mount_mcp_endpoints re-running setup_agent) is a no-op.
    if config.instructions and hasattr(agent, "_instructions"):
        if not getattr(agent, "_persona_overlaid", False):
            agent._instructions = compose_instructions(
                base=agent._instructions, overlay=config.instructions
            )
            agent._persona_overlaid = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py -v`
Expected: PASS (new tests + all existing knob/setup_agent tests)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_wiring.py python/tests/test_wiring.py
git commit -m "feat(wiring): compose persona instructions over template grounding"
```

---

## Task 6: `DataTemplate` + `DataAgent` refactor (reference implementation)

**Files:**
- Modify: `python/src/apx_agent/data_agent.py`
- Test: `python/tests/test_data_agent.py`

Constraint: `_topology.py:325` keys off the class name `"DataAgent"`. Keep
`DataAgent` a class subclassing `LlmAgent`. Extract the wiring/grounding into a
shared helper used by both `DataAgent.__init__` and `DataTemplate.build`.
`DataTemplate.build` returns a `DataAgent` instance (so topology still resolves).
Use a `schema`-aliased Pydantic field to dodge the `BaseModel.schema()` shadow.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_data_agent.py
from apx_agent import DataAgent, DataTemplate, template_registry


class TestDataTemplate:
    def test_registered_in_global_registry(self):
        assert template_registry.get("data").name == "data"

    def test_build_returns_dataagent_equivalent_to_constructor(self):
        ws = _ws_with_schema({"orders": ["id(INT)", "total(DOUBLE)"]})
        spec = DataTemplate.Spec(catalog="main", schema="sales")
        built = DataTemplate().build(spec, ws=ws)
        direct = DataAgent("main", "sales", ws=ws)
        assert type(built) is DataAgent
        assert built._instructions == direct._instructions
        assert [t.__name__ for t in built._tool_fns] == [t.__name__ for t in direct._tool_fns]

    def test_build_from_dict_via_registry_alias(self):
        # config/UI callers pass a dict using the "schema" alias.
        agent = template_registry.build("data", {"catalog": "main", "schema": "sales"})
        assert type(agent) is DataAgent
        assert agent.schema == "sales"

    def test_topology_node_type_still_dataagent_for_built(self):
        from apx_agent._topology import _agent_class_to_node_type
        agent = DataTemplate().build(DataTemplate.Spec(catalog="main", schema="sales"))
        assert _agent_class_to_node_type(agent) == "DataAgent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_data_agent.py -k DataTemplate -v`
Expected: FAIL with `ImportError: cannot import name 'DataTemplate'`

- [ ] **Step 3: Write minimal implementation**

Rewrite `python/src/apx_agent/data_agent.py` to extract the shared builder and add `DataTemplate`, keeping `DataAgent` as a class. Full file:

```python
"""DataAgent — an LlmAgent specialized for governed Unity Catalog data access.

Also the reference implementation of the Template protocol: ``DataTemplate``
wraps the same builder behind a typed Spec + registry entry, so the data agent
can be created by name/config as well as directly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._agents import LlmAgent
from ._resources import ResourceSpec, attach_resources
from ._schema import build_instructions_from_schema, introspect_schema
from ._template import template


def _build_data_tools_and_instructions(
    *,
    catalog: str,
    schema: str,
    warehouse_id: str | None,
    ws: Any | None,
    include_functions: bool,
    genie_space: str | None,
    vector_index: str | None,
    instructions: str | None,
    extra_tools: list[Any] | None,
) -> tuple[list[Any], str]:
    """Shared builder: returns (tools, instructions) for the data agent shape."""
    from .genie import genie_tool
    from .sql_tools import sql_tool
    from .vector_search import vector_search_tool

    tables = introspect_schema(ws, catalog, schema, warehouse_id) if ws else {}

    sql = sql_tool(warehouse_id=warehouse_id)
    if tables:
        attach_resources(
            sql,
            [ResourceSpec("uc_table", f"{catalog}.{schema}.{t}") for t in tables],
        )

    tools: list[Any] = [sql]
    if include_functions and ws is not None:
        from .catalog import uc_function_toolkit

        tools += uc_function_toolkit(f"{catalog}.{schema}", ws=ws)
    if genie_space:
        tools.append(genie_tool(genie_space))
    if vector_index:
        tools.append(vector_search_tool(vector_index))
    if extra_tools:
        tools += extra_tools

    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables
    )
    return tools, resolved_instructions


class DataAgent(LlmAgent):
    """An ``LlmAgent`` wired to a Unity Catalog schema. (See module docstring.)"""

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        warehouse_id: str | None = None,
        ws: Any | None = None,
        include_functions: bool = True,
        genie_space: str | None = None,
        vector_index: str | None = None,
        instructions: str | None = None,
        name: str | None = None,
        extra_tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.catalog = catalog
        self.schema = schema
        tools, resolved_instructions = _build_data_tools_and_instructions(
            catalog=catalog,
            schema=schema,
            warehouse_id=warehouse_id,
            ws=ws,
            include_functions=include_functions,
            genie_space=genie_space,
            vector_index=vector_index,
            instructions=instructions,
            extra_tools=extra_tools,
        )
        super().__init__(
            tools=tools,
            instructions=resolved_instructions,
            name=name or f"{schema}_data_agent",
            **kwargs,
        )


@template
class DataTemplate:
    """Reference Template: 'talk to my governed Unity Catalog schema.'"""

    name = "data"
    title = "Data Analyst"
    description = "Talks to a governed Unity Catalog schema (SQL + UC functions, optional Genie/Vector Search)."

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        # Field name avoids shadowing Pydantic's BaseModel.schema(); accepts the
        # "schema" key via alias (and "schema_name" via populate_by_name).
        schema_name: str = Field(alias="schema")
        warehouse_id: str | None = None
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec: "DataTemplate.Spec", *, ws: Any | None = None) -> DataAgent:
        return DataAgent(
            spec.catalog,
            spec.schema_name,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_data_agent.py -v`
Expected: PASS — both the new `TestDataTemplate` tests AND every pre-existing `DataAgent` test (parity is the safety net).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/data_agent.py python/tests/test_data_agent.py
git commit -m "feat(template): DataTemplate ref impl; DataAgent shares one builder"
```

---

## Task 7: Public exports

**Files:**
- Modify: `python/src/apx_agent/__init__.py`
- Test: `python/tests/test_template.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_template.py
def test_public_exports_and_data_template_registered():
    import apx_agent

    assert hasattr(apx_agent, "Template")
    assert hasattr(apx_agent, "TemplateInfo")
    assert hasattr(apx_agent, "template")
    assert hasattr(apx_agent, "template_registry")
    assert hasattr(apx_agent, "DataTemplate")
    # importing the package registers the built-in DataTemplate
    assert "data" in {i.name for i in apx_agent.template_registry.list()}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_template.py::test_public_exports_and_data_template_registered -v`
Expected: FAIL with `AttributeError: module 'apx_agent' has no attribute 'Template'`

- [ ] **Step 3: Write minimal implementation**

In `python/src/apx_agent/__init__.py`, after the existing DataAgent import block (the `from .data_agent import DataAgent` line), add:

```python
# Template protocol + registry (agent-as-config foundation)
from ._template import Template, TemplateInfo, template, template_registry
from .data_agent import DataTemplate
```

Add the new names to `__all__` if the module defines one (search for `__all__` in the file; append `"Template"`, `"TemplateInfo"`, `"template"`, `"template_registry"`, `"DataTemplate"` to the list). If there is no `__all__`, skip this.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_template.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/__init__.py python/tests/test_template.py
git commit -m "feat(template): export Template, registry, DataTemplate"
```

---

## Task 8: Full-suite regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the affected suites**

Run: `cd python && uv run pytest tests/test_template.py tests/test_data_agent.py tests/test_wiring.py tests/test_prompt_assembly.py tests/test_compile.py tests/test_topology.py -q`
Expected: all pass, 0 failures.

- [ ] **Step 2: Run the full suite (catch unexpected coupling)**

Run: `cd python && uv run pytest -q`
Expected: no new failures vs. the pre-change baseline. If a pre-existing failure is unrelated to these files, note it; do not fix unrelated breakage here.

- [ ] **Step 3: Import smoke test**

Run: `cd python && uv run python -c "import apx_agent; print(sorted(i.name for i in apx_agent.template_registry.list()))"`
Expected: prints a list containing `'data'`.

- [ ] **Step 4: Commit (only if anything was adjusted)**

```bash
git add -A && git commit -m "test(template): full-suite regression green for E1"
```

---

## Self-review notes (author)

- **Spec coverage:** §2 protocol → Task 1; §3 registry/discovery → Tasks 2–3; §4 grounding/degradation → preserved by Task 6 (parity tests cover `ws`/no-`ws`); §5 persona compose → Tasks 4–5; §6 DataAgent refactor → Task 6; exports → Task 7. Error-handling table → Tasks 2 (unknown/duplicate), 3 (broken EP); graceful degradation reuses existing DataAgent behavior (parity test `test_introspection_failure_degrades_gracefully` already in the suite).
- **`schema` gotcha** (spec open question): resolved via `Field(alias="schema")` + `populate_by_name=True` (Task 6). Field named `schema_name` so no `BaseModel.schema()` shadow; dict/alias and attribute access both work.
- **Registry symbol name** (spec open question): resolved to `template_registry` + `@template` decorator.
- **Idempotency of persona overlay:** sentinel `_persona_overlaid` (Task 5) — guards the double `setup_agent` path.
- **Out of scope (later specs):** config-by-name expansion `[tool.apx.agent].template` (E3), `[tool.apx.tools]` (E2), Coworker (C1/C2).

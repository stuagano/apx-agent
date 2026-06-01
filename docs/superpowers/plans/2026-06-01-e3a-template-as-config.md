# E3a · Template-as-Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an agent to be declared entirely as data in `[tool.apx.agent]` by adding a `template` inline-table: `template = { name = "data", catalog = "main", schema = "sales" }`. `resolve_agent` builds the leaf agent from the template registry when no pre-constructed agent is available; `finalize_agent` layers persona/knobs/tools on top — exactly as E1 designed. The serve path (`setup_agent` / `create_app`) and the `_load_finalized_agent` family (CLI `info`, `lint`, `eval`, `run`) support template-only projects without an `agent.py`/`agent` variable. The model-serving deploy path (`apx deploy`) also supports template-only projects via `resolve_agent` at the `_load_agent` site.

**Architecture:** A new `resolve_agent(module_spec, config, *, ws=None) -> BaseAgent` function in `_wiring.py` is the single resolution seam. If `config.template` is set, it calls `template_registry.build(name, spec, ws=ws)` to materialize the agent; otherwise it falls back to the existing module-import path (reimplemented inline without importing `cli.py` to avoid a circular import). `finalize_agent` is then called by the caller as usual — no change to the finalize contract. Three call-site sets are updated:

1. **`cli._load_finalized_agent`** — load config first; pass both `module_spec` and `config` into `resolve_agent`; call `finalize_agent(agent, config)`.
2. **`cli` deploy command** (`_load_agent` bare site at line 1792) — same pattern: load config, `resolve_agent(module_spec, config, ws=_ws_for_template(config))`, then `log_agent` finalizes as before. `ws` follows the same lazy rule as `_load_finalized_agent`: attempt `_make_workspace_client()` only when `config.template` is set; degrade gracefully on failure.
3. **`setup_agent` / `create_app`** — `agent` becomes `Optional[BaseAgent]`; after the `if config is None: return None` guard, if `agent is None` then `agent = resolve_agent(None, config, ws=getattr(app.state, "workspace_client", None))`. `create_app`'s lifespan is patched to thread through `ctx.agent` to `mount_invocations_route` (instead of the outer `agent` variable that may be `None`).

**`ws` at serve-time:** `app.state.workspace_client` is set by the lifespan (line 595 of `_wiring.py`) **before** `setup_agent` is called (line 607) — it is available via `getattr(app.state, "workspace_client", None)`. No lazy `_make_workspace_client()` in the serve path.

**`ws` at CLI-time:** `_make_workspace_client()` called lazily — only when a `template` field is present (template build may need `ws`). Extracted as a one-liner `_ws_for_template(config)` shared by both `_load_finalized_agent` and the deploy site, so the rule lives in one place. If `_make_workspace_client` fails (no creds), `ws` falls back to `None`; `DataTemplate.build(spec, ws=None)` gracefully degrades, so the template still builds a working (ungrounded) agent. Deploy with `ws=None` produces a permanently ungrounded model — the caller gets a `logger.warning` so the issue is visible without crashing startup.

**Template field shape (locked):** `template: dict[str, Any] | None = None` on `AgentConfig`. Resolution: `name = template["name"]` (missing key → clear `TemplateConfigError`); `spec = {k: v for k, v in template.items() if k != "name"}`; `template_registry.build(name, spec, ws=ws)`. The flat dict matches the spec's inline-table TOML and delegates validation to each template's `Spec.model_validate` (e.g. `DataTemplate.Spec`'s `populate_by_name=True` handles the `schema` alias automatically — no special-casing in `resolve_agent`).

**Ordering (locked):** resolve → finalize (knobs → persona compose → config tools → E3c guards). The template's `build()` sets grounded `_instructions`; `apply_config_knobs` then composes `config.instructions` over them via the existing `compose_instructions` seam (E1) — already implemented and tested.

**Tech Stack:** Python 3.11+, Pydantic v2, `tomllib`, pytest, pyright (CI gate — `_wiring.py`, `_models.py`, `_template.py`, `_inspection.py` are NOT in the type-debt exclude list → 0-error required; `cli.py` IS excluded — do not add new errors).

**Spec:** `docs/superpowers/specs/2026-05-29-e3-declarative-agent-config-design.md` (E3a section)
**E1 spec:** `docs/superpowers/specs/2026-05-29-template-protocol-design.md`
**E2 plan (reference):** `docs/superpowers/plans/2026-05-30-e2-declarative-tools.md`

**Decisions locked (2026-06-01):**
1. `template: dict[str, Any] | None = None` flat field on `AgentConfig`. No `TemplateRef` wrapper — flat dict matches TOML inline-table and delegates Spec validation to the template class.
2. `resolve_agent(module_spec, config, *, ws=None) -> BaseAgent` lives in `_wiring.py`. Module-import done inline (not via `cli._load_agent`) to avoid the `cli → _wiring` circular import.
3. Resolution logic: template field present → `template_registry.build`; else → local `importlib` import. Neither present (no template, `module_spec` is `None` or empty) → `TemplateConfigError(ValueError)` with clear message. Named `TemplateConfigError` to avoid collision with any future generic `ConfigError` symbol (only `ToolConfigError` exists today).
4. Serve path: `setup_agent` and `create_app` make `agent` `Optional[BaseAgent] = None`. If `None`, `resolve_agent` is called after the `if config is None: return None` guard. `create_app` threads `ctx.agent` into `mount_invocations_route`.
5. CLI `_load_finalized_agent` loads config first, then calls `resolve_agent(module_spec, config, ws=lazy)`.
6. CLI deploy path (`_load_agent` bare call at line 1792): same `resolve_agent` swap. Deploy's `log_agent` still finalizes; no double-finalize.
7. `ws` in serve: `getattr(app.state, "workspace_client", None)`. `ws` in CLI: `_make_workspace_client()` lazily (only when `config.template` is set; template `build()` degrades gracefully on `ws=None`).
8. `apx info`, `lint`, `eval`, `run` go through `_load_finalized_agent` — they inherit the fix automatically.

**Convention:** run everything from `python/` via `uv run …` (repo-root `.venv` is stale and shadows `src/`).

---

## File structure

- **Modify** `python/src/apx_agent/_models.py` — add `template: dict[str, Any] | None = None` field to `AgentConfig`. Add `Any` to the imports.
- **Modify** `python/src/apx_agent/_wiring.py` — add `resolve_agent(module_spec, config, *, ws=None) -> BaseAgent`; make `agent` optional in `setup_agent` and `create_app`; thread `ctx.agent` through to `mount_invocations_route` in the lifespan.
- **Modify** `python/src/apx_agent/cli.py` — update `_load_finalized_agent` to load config and call `resolve_agent`; update the bare `_load_agent` call in the deploy command.
- **Modify** `docs/configuration.md` — add "Template-as-config" section after the `[[tool.apx.tools]]` block.
- **Modify** `python/tests/test_wiring.py` — append E3a tests (field loading, resolve_agent unit tests, persona-over-template characterization, serve-path integration).
- **Modify** `python/tests/test_cli.py` — append CLI integration tests.

---

## Task 1: `template` field on `AgentConfig`

**Files:**
- Modify: `python/src/apx_agent/_models.py`
- Test: `python/tests/test_wiring.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import textwrap

from apx_agent._models import AgentConfig
from apx_agent._inspection import _load_agent_config


class TestTemplateField:
    def test_template_field_absent_defaults_to_none(self):
        cfg = AgentConfig(name="t")
        assert cfg.template is None

    def test_template_field_accepts_dict(self):
        cfg = AgentConfig(name="t", template={"name": "data", "catalog": "main", "schema": "sales"})
        assert cfg.template["name"] == "data"
        assert cfg.template["catalog"] == "main"
        assert cfg.template["schema"] == "sales"

    def test_template_loads_from_toml_inline_table(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "sales-coworker"
            model = "databricks-claude-sonnet-4-6"
            template = { name = "data", catalog = "main", schema = "sales" }
        """))
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.template == {"name": "data", "catalog": "main", "schema": "sales"}

    def test_template_schema_alias_passes_through_unmodified(self, tmp_path):
        # The flat dict carries "schema" as-is; DataTemplate.Spec handles the alias.
        # This verifies the loader doesn't mangle the key before it reaches the template.
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "t"
            template = { name = "data", catalog = "main", schema = "sales" }
        """))
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert "schema" in config.template   # key preserved, not renamed to schema_name
        assert config.template["schema"] == "sales"

    def test_template_absent_from_toml_gives_none(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text('[tool.apx.agent]\nname = "minimal"\n')
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.template is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py::TestTemplateField -v`
Expected: FAIL — `AgentConfig` has no `template` field; the TOML dict loads as an unknown key and is dropped by the `k in AgentConfig.model_fields` filter in `_inspection.py:179`.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_models.py`, add `Any` to the existing imports and add the `template` field to `AgentConfig`:

```python
# Extend existing top-level imports (typing block) — add Any if not present:
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias
```

In `AgentConfig`, insert after the `api_prefix` field (currently the last field):

```python
    template: dict[str, Any] | None = None
    """Template-as-config: ``{ name = "data", catalog = "main", schema = "sales" }``.

    When set, ``resolve_agent`` builds the leaf agent from the named template
    via ``template_registry.build(name, spec, ws=ws)`` rather than importing a
    Python module. The ``name`` key selects the template; all other keys become
    the spec dict passed to the template's ``Spec.model_validate``. The
    ``[tool.apx.agent]`` envelope (instructions, model, knobs) is layered on top
    afterward via ``finalize_agent`` as usual — template builds the leaf, persona
    overlays. See E3a plan and E1 spec for the full design.
    """
```

The `_inspection.py` loader at line 179 already passes `k in AgentConfig.model_fields`, so once `template` is a declared field, the inline-table dict from TOML will flow through without any loader change.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py::TestTemplateField -v`
Expected: PASS (5 passed). Then:

```bash
cd python && uv run pyright src/apx_agent/_models.py
```
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_models.py python/tests/test_wiring.py
git commit -m "feat(models): AgentConfig.template field for template-as-config (E3a)"
```

---

## Task 2: `resolve_agent` — build-from-template or import-module

**Files:**
- Modify: `python/src/apx_agent/_wiring.py`
- Test: `python/tests/test_wiring.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import importlib
import sys

import pytest

from apx_agent import AgentConfig, LlmAgent
from apx_agent._wiring import resolve_agent
from apx_agent.data_agent import DataAgent


class TestResolveAgent:
    def test_template_config_builds_data_agent(self):
        """template dict → DataAgent built via registry (no module needed)."""
        config = AgentConfig(
            name="t",
            template={"name": "data", "catalog": "main", "schema": "sales"},
        )
        agent = resolve_agent(None, config, ws=None)
        assert isinstance(agent, DataAgent)
        # ws=None → graceful degradation: sql tool present with name "run_sql"
        tool_names = [fn.__name__ for fn in agent._tool_fns]
        assert "run_sql" in tool_names

    def test_template_config_uses_schema_alias(self):
        """'schema' in the dict must reach DataTemplate.Spec via populate_by_name."""
        config = AgentConfig(
            name="t",
            template={"name": "data", "catalog": "main", "schema": "sales"},
        )
        agent = resolve_agent(None, config, ws=None)
        assert isinstance(agent, DataAgent)

    def test_no_template_with_module_spec_imports_agent(self, tmp_path, monkeypatch):
        """No template → falls back to module-import path."""
        agent_file = tmp_path / "my_agent.py"
        agent_file.write_text(
            "from apx_agent import Agent\nmy_var = Agent(tools=[])\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        config = AgentConfig(name="t")
        agent = resolve_agent("my_agent:my_var", config, ws=None)
        assert isinstance(agent, LlmAgent)
        # clean up to avoid polluting other tests
        sys.modules.pop("my_agent", None)

    def test_neither_template_nor_module_raises_template_config_error(self):
        """No template and no module_spec → clear TemplateConfigError."""
        from apx_agent._wiring import TemplateConfigError
        config = AgentConfig(name="t")
        with pytest.raises(TemplateConfigError, match="[Nn]o agent"):
            resolve_agent(None, config, ws=None)

    def test_unknown_template_name_raises_listing_available(self):
        """Unknown template name → ValueError from registry listing known names."""
        config = AgentConfig(
            name="t",
            template={"name": "does_not_exist", "catalog": "c"},
        )
        with pytest.raises(ValueError, match="does_not_exist"):
            resolve_agent(None, config, ws=None)

    def test_missing_name_key_in_template_raises_clearly(self):
        """template dict without 'name' key → TemplateConfigError mentioning 'name'."""
        from apx_agent._wiring import TemplateConfigError
        config = AgentConfig(
            name="t",
            template={"catalog": "main", "schema": "sales"},
        )
        with pytest.raises(TemplateConfigError, match="name"):
            resolve_agent(None, config, ws=None)

    def test_no_template_no_module_none_config_raises(self):
        """config is None and module_spec is None → TemplateConfigError."""
        from apx_agent._wiring import TemplateConfigError
        with pytest.raises(TemplateConfigError, match="[Nn]o agent"):
            resolve_agent(None, None, ws=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py::TestResolveAgent -v`
Expected: FAIL — `cannot import name 'resolve_agent' from 'apx_agent._wiring'`

- [ ] **Step 3: Write the implementation**

Add `resolve_agent` to `python/src/apx_agent/_wiring.py` after `finalize_agent` (and before `_resolve_env_var`). The function must not import from `cli.py` — module loading is done inline with `importlib` to avoid the `cli → _wiring` cycle:

```python
class TemplateConfigError(ValueError):
    """Raised when an agent cannot be resolved from the given template config or module."""


def _ws_for_template(config: "AgentConfig | None") -> Any:
    """Return a WorkspaceClient for template resolution, or None.

    Only attempts construction when config has a template field (template.build
    may need ws for live schema introspection). Degrades gracefully on failure —
    DataTemplate.build(spec, ws=None) still returns a working agent.
    """
    if config is None or config.template is None:
        return None
    try:
        return _make_workspace_client()
    except Exception as e:
        logger.warning(
            "Could not build workspace client for template resolution: %s. "
            "Template will build with ws=None (graceful degradation — "
            "grounded instructions require live introspection).",
            e,
        )
        return None


def resolve_agent(
    module_spec: str | None,
    config: "AgentConfig | None",
    *,
    ws: Any | None = None,
) -> "BaseAgent":
    """Resolve a ``BaseAgent`` from either a template config or a module import.

    This runs BEFORE ``finalize_agent``. Its job is to produce the leaf agent
    object. The caller then runs ``finalize_agent(agent, config)`` to layer
    knobs, persona, tools, and guards on top.

    Resolution order:

    1. If ``config.template`` is set, build via ``template_registry.build``.
       ``config.template`` must contain a ``"name"`` key (selects the template);
       all other keys form the spec dict passed to the template's
       ``Spec.model_validate``. The ``schema`` alias in ``DataTemplate.Spec``
       is handled automatically by ``populate_by_name=True`` — no special-casing
       here.
    2. Otherwise, import ``module_spec`` (``"module:variable"`` format) using
       ``importlib`` directly (NOT via ``cli._load_agent`` — that would create a
       ``cli → _wiring`` import cycle). Add cwd to ``sys.path`` as ``_load_agent``
       does.
    3. If neither is available (no template, no module_spec, or module_spec is
       empty), raise ``TemplateConfigError`` with a clear diagnostic.

    ``ws`` is passed to ``template.build(spec, ws=ws)``. Templates that perform
    live Databricks introspection (e.g. ``DataTemplate``) use it if present and
    degrade gracefully when ``None`` — never raise for lack of ``ws``.

    Does NOT import ``cli`` — the ``cli`` module imports ``_wiring`` lazily
    (deliberately, to break initialization cycles), so a top-level back-import
    here would make that cycle unconditional.
    """
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    from ._template import template_registry

    # 1. Template path
    template_dict: dict[str, Any] | None = None
    if config is not None and config.template is not None:
        template_dict = config.template

    if template_dict is not None:
        tname = template_dict.get("name")
        if not tname:
            raise TemplateConfigError(
                "AgentConfig.template must include a 'name' key to select the template. "
                f"Got: {template_dict!r}"
            )
        spec = {k: v for k, v in template_dict.items() if k != "name"}
        # ValueError from the registry (unknown name) propagates as-is — it lists
        # available names, which is actionable.
        # build() returns Any; pyright accepts this without a type: ignore because
        # Any is compatible with the BaseAgent return annotation. Add a type: ignore
        # ONLY if pyright actually flags it — don't pre-add suppression for a
        # theoretical error (reportUnnecessaryTypeIgnoreComment would then fire).
        return template_registry.build(tname, spec, ws=ws)

    # 2. Module-import path
    if not module_spec:
        raise TemplateConfigError(
            "No agent to resolve: config has no 'template' field and no module_spec "
            "was provided. Either add 'template = { name = \"...\", ... }' to "
            "[tool.apx.agent] or pass a 'module:variable' module_spec."
        )
    if ":" not in module_spec:
        raise TemplateConfigError(
            f"module_spec must be 'module:variable', got {module_spec!r}."
        )
    mod_path, _, var_name = module_spec.partition(":")
    if not mod_path or not var_name:
        raise TemplateConfigError(
            f"Both module and variable must be non-empty in module_spec, got {module_spec!r}."
        )
    cwd = str(_Path.cwd())
    if cwd not in _sys.path:
        _sys.path.insert(0, cwd)
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        raise TemplateConfigError(
            f"Failed to import {mod_path!r}: {e}. "
            "Make sure the module is on PYTHONPATH or in the current directory."
        ) from e
    if not hasattr(mod, var_name):
        raise TemplateConfigError(
            f"Module {mod_path!r} has no attribute {var_name!r}."
        )
    return getattr(mod, var_name)
```

Also add `TemplateConfigError` to the public re-exports in `python/src/apx_agent/__init__.py` — but hold that for Task 5 (exports + regression). For now it's an internal `_wiring` detail.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py::TestResolveAgent -v`
Expected: PASS (7 passed). Then:

```bash
cd python && uv run pyright src/apx_agent/_wiring.py
```
Expected: 0 errors. `template_registry.build()` returns `Any` and `getattr(mod, var_name)` is `Any`; both are compatible with the `BaseAgent` return annotation without any `# type: ignore`. Do NOT pre-add type: ignore comments — if `reportUnnecessaryTypeIgnoreComment` is enabled in the repo's pyright config (plausible for a 0-error-gated file), a spurious ignore becomes an error. Only add `# type: ignore[return-value]` if pyright actually flags it on the step-4 run.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_wiring.py python/tests/test_wiring.py
git commit -m "feat(wiring): resolve_agent — template-registry build or module-import (E3a)"
```

---

## Task 3: Persona overlay over template grounding — characterization test

**Files:**
- Test: `python/tests/test_wiring.py` (append)

This task has **no new implementation** — `apply_config_knobs` already composes `config.instructions` over `agent._instructions` via `compose_instructions` (confirmed at `_wiring.py:99–113`). A template-built `DataAgent` already has grounded instructions set by `_build_data_tools_and_instructions`. The purpose of this task is a **regression guard**: verify that `resolve_agent → finalize_agent` produces a correctly composed `_instructions` and is idempotent, so the E3a path exercises the exact compose seam E1 designed.

- [ ] **Step 1: Write the characterization test**

```python
# Append to python/tests/test_wiring.py
from apx_agent._wiring import finalize_agent, resolve_agent
from apx_agent.data_agent import DataAgent


class TestPersonaOverTemplate:
    def test_resolve_then_finalize_composes_persona_over_grounding(self):
        """Grounded instructions from template + persona from config → both present."""
        config = AgentConfig(
            name="t",
            instructions="Be concise and professional.",
            template={"name": "data", "catalog": "main", "schema": "sales"},
        )
        agent = resolve_agent(None, config, ws=None)
        assert isinstance(agent, DataAgent)
        # Template build set grounded instructions (generic when ws=None).
        grounding_before = getattr(agent, "_instructions", "")

        # finalize composes persona (overlay) above grounding (base).
        finalize_agent(agent, config)
        composed = getattr(agent, "_instructions", "")

        # Both sides present in the composed result.
        assert "Be concise and professional." in composed
        assert len(composed) >= len(grounding_before)

    def test_persona_compose_is_idempotent(self):
        """Second finalize_agent call must not double-compose the persona."""
        config = AgentConfig(
            name="t",
            instructions="Be concise.",
            template={"name": "data", "catalog": "main", "schema": "sales"},
        )
        agent = resolve_agent(None, config, ws=None)
        finalize_agent(agent, config)
        instructions_after_first = getattr(agent, "_instructions", "")
        finalize_agent(agent, config)  # idempotent
        assert getattr(agent, "_instructions", "") == instructions_after_first

    def test_template_only_no_persona_uses_grounding_verbatim(self):
        """No envelope instructions → template grounding used unchanged."""
        config = AgentConfig(
            name="t",
            # no instructions
            template={"name": "data", "catalog": "main", "schema": "sales"},
        )
        agent = resolve_agent(None, config, ws=None)
        grounding = getattr(agent, "_instructions", "")
        finalize_agent(agent, config)
        assert getattr(agent, "_instructions", "") == grounding
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `cd python && uv run pytest tests/test_wiring.py::TestPersonaOverTemplate -v`
Expected: PASS (3 passed) — these tests prove the existing seam works correctly for the template path. If any fail, the bug is either in `compose_instructions` (wrong separator or order) or in `apply_config_knobs`'s idempotency sentinel (`_persona_overlaid`), not in E3a itself. Diagnose and fix before continuing.

- [ ] **Step 3: No implementation step**

No source changes. All green = confirmation the E1 persona-compose seam handles the template path without modification.

- [ ] **Step 4: Confirm pyright still clean**

Run: `cd python && uv run pyright src/apx_agent/_wiring.py`
Expected: 0 errors (no source touched).

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_wiring.py
git commit -m "test(wiring): persona-over-template characterization guard (E3a)"
```

---

## Task 4: Wire `resolve_agent` into the serve path + CLI

**Files:**
- Modify: `python/src/apx_agent/_wiring.py` — make `agent` optional in `setup_agent` and `create_app`; thread `ctx.agent` through.
- Modify: `python/src/apx_agent/cli.py` — update `_load_finalized_agent`; update bare `_load_agent` in deploy.
- Test: `python/tests/test_wiring.py` (append)
- Test: `python/tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# Append to python/tests/test_wiring.py
import textwrap

import pytest
from fastapi import FastAPI

from apx_agent._wiring import setup_agent
from apx_agent.data_agent import DataAgent


class TestServePath:
    @pytest.mark.asyncio
    async def test_setup_agent_with_none_agent_builds_from_template(self, tmp_path):
        """setup_agent(agent=None, …) with a template config resolves + serves a DataAgent."""
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "sales-coworker"
            model = "databricks-claude-sonnet-4-6"
            template = { name = "data", catalog = "main", schema = "sales" }
        """))
        app = FastAPI()
        # Simulate the lifespan having set workspace_client before setup_agent.
        app.state.workspace_client = None

        ctx = await setup_agent(app, None, pyproject_path=str(pp))

        assert ctx is not None
        assert isinstance(ctx.agent, DataAgent)
        # sql tool ("run_sql") must appear in the A2A card skills list.
        skill_names = [s.name for s in ctx.card.skills]
        assert "run_sql" in skill_names

    @pytest.mark.asyncio
    async def test_setup_agent_explicit_agent_wins_over_template(self, tmp_path):
        """An explicit pre-built agent is used as-is even when template config is present."""
        from apx_agent import Agent, AgentConfig

        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "t"
            template = { name = "data", catalog = "main", schema = "sales" }
        """))
        explicit_agent = Agent(tools=[])
        app = FastAPI()
        app.state.workspace_client = None

        ctx = await setup_agent(app, explicit_agent, pyproject_path=str(pp))

        assert ctx is not None
        # Explicit agent passed in — should not be replaced by a DataAgent.
        assert ctx.agent is explicit_agent

    @pytest.mark.asyncio
    async def test_setup_agent_none_agent_no_config_returns_none(self):
        """setup_agent(agent=None) without config still returns None (existing behavior)."""
        app = FastAPI()
        ctx = await setup_agent(app, None, pyproject_path="/nonexistent/pyproject.toml")
        assert ctx is None
```

```python
# Append to python/tests/test_cli.py
import textwrap
from click.testing import CliRunner
from apx_agent.cli import main


def test_apx_info_with_template_config(tmp_path, monkeypatch):
    """apx info with a template config (no agent.py) resolves and lists tools."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "sales-coworker"
        model = "databricks-claude-sonnet-4-6"
        template = { name = "data", catalog = "main", schema = "sales" }
    """))
    # No agent.py — module_spec defaults to something (inspect the `info` command
    # to confirm the default; if it requires --module, adjust the invocation).
    # The info command at cli.py:3059 calls _load_finalized_agent(module).
    # Check the default: `apx info` uses module from config or falls back to
    # a default. Pass an explicit nonsense module so it must resolve via template.
    res = CliRunner().invoke(main, ["info", "--module", "nonexistent:agent"])
    # With template config, resolve_agent should use template and ignore module.
    assert res.exit_code == 0, res.output
    assert "run_sql" in res.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_wiring.py::TestServePath tests/test_cli.py::test_apx_info_with_template_config -v`
Expected: FAIL — `setup_agent` requires a non-None `agent`; `_load_finalized_agent` uses `_load_agent` which hard-fails on `"nonexistent:agent"`.

- [ ] **Step 3: Write the implementation**

**`_wiring.py` — `setup_agent` signature + body:**

Change the signature (inspect the current declaration at line 172 before editing):
```python
async def setup_agent(
    app: FastAPI,
    agent: "BaseAgent | None",
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> AgentContext | None:
```

After the `if config is None: return None` guard (lines 192–196), insert the template resolution before the `sub_agents` block:
```python
    # E3a: resolve from template if no agent was passed in.
    # Do this after the config guard (we need config.template to branch),
    # before sub_agents merge (which mutates agent._sub_agent_urls).
    if agent is None:
        agent = resolve_agent(
            None,
            config,
            ws=getattr(app.state, "workspace_client", None),
        )
```

**`_wiring.py` — `create_app` lifespan:**

Change the `create_app` signature to accept `Optional`. Inspect the current signature (verified at `_wiring.py:547–553`) before editing:
```python
def create_app(
    agent: "BaseAgent | None" = None,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
    session_store: Any | None = None,
) -> FastAPI:
```

In the lifespan (currently at lines 607–617), after `ctx = await setup_agent(...)` returns, replace the `mount_invocations_route(app, agent, ...)` call to use `ctx.agent`. **The `agent` outer variable is the only place in `create_app`'s body that touches the parameter** (verified: `create_app` returns `FastAPI(lifespan=lifespan)` at line 640 with no pre-lifespan agent access) — so making `agent=None` is safe:

```python
        ctx = await setup_agent(
            app, agent, config, pyproject_path=pyproject_path
        )

        if ctx is not None:
            try:
                from ._invocations import mount_invocations_route
                # Use ctx.agent — when agent=None was passed, ctx.agent is the
                # template-resolved agent. Using the outer `agent` variable here
                # would pass None to mount_invocations_route.
                mount_invocations_route(app, ctx.agent, ctx.config, session_store=session_store)
            except Exception as exc:
                logger.warning("Skipping /invocations mount: %s", exc)
```

**`cli.py` — `_load_finalized_agent`:**

Update to load config first, then call `resolve_agent` (using the shared `_ws_for_template` helper from `_wiring`):
```python
def _load_finalized_agent(module_spec: str) -> Any:
    """Resolve + finalize an agent for CLI introspection commands.

    Loads [tool.apx.agent] config first, then calls resolve_agent so
    template-only projects (no agent.py) work via apx info / lint / eval / run.
    Falls back to the module-import path for code-defined agents.
    """
    from ._wiring import finalize_agent, resolve_agent, _ws_for_template
    from ._inspection import _load_agent_config

    config = _load_agent_config(pyproject_path=None)
    agent = resolve_agent(module_spec, config, ws=_ws_for_template(config))
    finalize_agent(agent, config, pyproject_path=None)
    return agent
```

**`cli.py` — deploy command bare `_load_agent` (line 1792):**

Replace:
```python
    agent = _load_agent(effective_module)
```
With:
```python
    # E3a: resolve_agent handles both template-only and module-defined agents.
    # The model-serving deploy path still defers finalization to log_agent.
    from ._wiring import resolve_agent as _resolve_agent, _ws_for_template as _deploy_ws
    from ._inspection import _load_agent_config as _load_cfg
    _deploy_config = _load_cfg(pyproject_path=None)
    agent = _resolve_agent(effective_module, _deploy_config, ws=_deploy_ws(_deploy_config))
```

`_ws_for_template` uses the same lazy-build + graceful-degradation pattern as `_load_finalized_agent`. Deploy with `ws=None` (creds unavailable) bakes a permanently ungrounded model — the `_ws_for_template` warning is the user's signal to fix auth before shipping.

(Inspect lines 1789–1793 before editing to confirm the exact surrounding variable names and indentation.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_wiring.py::TestServePath -v`
Expected: PASS (3 passed).

Run: `cd python && uv run pytest tests/test_cli.py::test_apx_info_with_template_config -v`
Expected: PASS. If `apx info` requires the `module` argument and has no default, adjust the `--module` value or inspect the command's `@click.argument` default. The test intent is: template config → agent resolved from registry → tools reported.

Then run the full wiring suite to check for regressions:
Run: `cd python && uv run pytest tests/test_wiring.py tests/test_cli.py -v`
Expected: PASS (no pre-existing regressions).

Then typecheck:
```bash
cd python && uv run pyright src/apx_agent/_wiring.py
```
Expected: 0 errors. (cli.py is in the type-debt exclude list — do not add new pyright errors there, but it is not gated. Use `type: ignore` annotations where needed for the new local imports inside functions.)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_wiring.py python/src/apx_agent/cli.py \
        python/tests/test_wiring.py python/tests/test_cli.py
git commit -m "feat(e3a): wire resolve_agent into setup_agent, create_app, and CLI paths"
```

---

## Task 5: Docs, exports, and full regression

**Files:**
- Modify: `python/src/apx_agent/__init__.py`
- Modify: `docs/configuration.md`
- Test: `python/tests/test_wiring.py` (append — export check)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py

def test_public_exports_resolve_agent_and_template_config_error():
    import apx_agent
    from apx_agent._wiring import TemplateConfigError, resolve_agent
    assert callable(resolve_agent)
    assert issubclass(TemplateConfigError, ValueError)
    # resolve_agent and TemplateConfigError are internal (_wiring module symbols);
    # no __all__ entry required, but they must be importable.
    # The template field is the public surface — verify it round-trips through AgentConfig.
    assert "template" in apx_agent.AgentConfig.model_fields
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `cd python && uv run pytest tests/test_wiring.py::test_public_exports_resolve_agent_and_template_config_error -v`
Expected: PASS (Task 2 added `TemplateConfigError` and `resolve_agent` to `_wiring.py`; `AgentConfig.template` is in `model_fields` from Task 1). If it fails, diagnose before adding the docs.

- [ ] **Step 3: Add configuration docs**

In `docs/configuration.md`, add a "Template-as-config" section immediately after the `[[tool.apx.tools]]` trust/env-var block (after the `APX_TOOLS_STRICT` line):

````markdown
## Template-as-config — `template = { name = "...", ... }`

> Python only. Declare the entire agent as data — no `agent.py` required.

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"
instructions = "Be concise and warm."        # persona overlay (E1)
template = { name = "data", catalog = "main", schema = "sales" }
```

The `template` inline-table selects a registered template by `name` and passes the remaining keys as the spec. The template wires the governed tools and sets grounded instructions (the **role**). The `[tool.apx.agent]` envelope (`model`, `instructions`, generation knobs) is the **persona** — layered on top afterward via the existing `compose_instructions` seam.

| Key | Purpose |
|---|---|
| `name` | Template registry key (e.g. `"data"`) |
| other keys | Template `Spec` fields (validated by the template's Pydantic `Spec`) |

**Built-in templates:**

| Name | Class | Spec fields |
|---|---|---|
| `data` | `DataTemplate` | `catalog`, `schema`, `warehouse_id?`, `genie_space?`, `vector_index?`, `include_functions?` |

**Interaction with `[[tool.apx.tools]]`:** Config-declared tools are additive — they attach *after* the template builds the leaf agent. A template-built `DataAgent` gets its wired SQL/Genie/UC tools from the template build; `[[tool.apx.tools]]` entries add on top. Code-wired tools always win on name collision.

**Interaction with `[tool.apx.agent.guardrails]`** (E3c): guards attach after persona compose — the full finalize order is: resolve (build-from-template) → apply_config_knobs (persona compose) → merge_config_tools → apply_config_guardrails.

**No `agent.py` required:** With a `template` configured, all runtimes (`apx run`, `apx deploy`, `apx info`, `apx eval`) can build the agent from TOML alone. The `module` key in `[tool.apx.agent]` is optional when `template` is set.

**Cross-repo templates:** Third-party templates register via the `apx_agent.templates` Python entry-point group — they appear automatically in the registry after `pip install`. See the E1 spec and `Template` protocol for authoring a template.
````

- [ ] **Step 4: Run full regression + pyright on all touched gated files**

```bash
cd python && uv run pytest -q
```
Expected: no new failures vs. baseline.

```bash
cd python && uv run pyright src/apx_agent/_models.py
cd python && uv run pyright src/apx_agent/_wiring.py
cd python && uv run pyright src/apx_agent/_template.py
cd python && uv run pyright src/apx_agent/_inspection.py
```
Expected: 0 errors on each.

```bash
cd python && uv run python -c "
from apx_agent import AgentConfig, template_registry
from apx_agent._wiring import resolve_agent
cfg = AgentConfig(name='t', template={'name': 'data', 'catalog': 'c', 'schema': 's'})
agent = resolve_agent(None, cfg, ws=None)
print('resolve_agent OK:', type(agent).__name__)
print('tools:', [fn.__name__ for fn in agent._tool_fns])
"
```
Expected output:
```
resolve_agent OK: DataAgent
tools: ['run_sql']
```

- [ ] **Step 5: Commit**

```bash
git add docs/configuration.md python/tests/test_wiring.py
git commit -m "feat(docs): template-as-config section in configuration.md (E3a)"
```

---

## Open questions (record for follow-up)

**OQ1 — ws at serve-time (resolved for E3a):** Use `getattr(app.state, "workspace_client", None)` in `setup_agent`. The lifespan sets this before calling `setup_agent` (verified at `_wiring.py:595-607`). `DataTemplate.build(spec, ws=None)` gracefully degrades, so `ws=None` (no creds at startup) is safe. Tracking: if a template requires live introspection at serve-time, the serve startup error surfaces from `template.build` — no additional handling needed in E3a.

**OQ2 — E3a/E3b ordering (template → memory → persona):** When E3b (declarative memory) lands, the finalize order becomes: resolve → apply_config_knobs (persona) → merge_config_tools → apply_config_guardrails (E3c). Memory tools from E3b will attach *before* persona if they're added in `finalize_agent` between `resolve` and `apply_config_knobs`, or *after* if added at the end of `finalize_agent`. Confirm ordering in the E3b plan. For E3a alone: template → persona composes over grounding → config tools → guards.

**OQ3 — Tools-only servability (from E2/PR #114):** A project that configures its agent in code (`Agent(name=..., model=...)`) and declares only `[[tool.apx.tools]]` is not served because `setup_agent` returns `None` when there is no `[tool.apx.agent]` section. E3a does not change this — `setup_agent`'s `if config is None: return None` guard is unchanged. The E3 spec recommends a short-term warning and a longer-term fix (synthesize minimal config from the agent instance). Cross-reference: E3a's `agent=None` optional-agent seam is the same theme. Decide in a follow-up E3 issue.

**OQ4 — `apx run` with a template-only project (no `app.py`):** The `apx run` command loads an ASGI app module (e.g. `app:app`) per `_RUN_MODULE_BY_TARGET`. A template-only project still needs an `app.py` that calls `create_app(agent=None)`. The scaffold for template projects (updating scaffold templates to omit `agent.py` and simplify `app.py`) is follow-up. E3a delivers the runtime seam; scaffold changes are separate.

---

## Self-review notes (author)

**Spec coverage:**
- `template` field + TOML round-trip + `schema` alias passthrough → T1.
- `resolve_agent`: template→DataAgent, schema alias, module-import, neither→error, unknown name, missing `name` key → T2.
- Persona compose over template grounding + idempotency → T3 (characterization test, no new impl — confirms E1 seam).
- Serve path (`setup_agent` optional `agent`, `create_app` lifespan thread-through) + CLI (`_load_finalized_agent`, deploy `_load_agent` swap) + CLI `info` via `_load_finalized_agent` → T4.
- Export check + docs → T5.

**Architecture reconciliation:** The spec says "E3a resolves the 'an agent IS a template instance' path" by making `setup_agent`/`log_agent` build from the registry when the user's `module` points at a template ref. This plan implements a cleaner version: `resolve_agent` is the single seam that handles both paths; `setup_agent` gains an `agent=None` option rather than reparsing the module spec. This matches the E2 `finalize_agent` chokepoint pattern and is stated in the Architecture block.

**Circular import protection:** `resolve_agent` replicates the module-import logic from `cli._load_agent` inline (5 lines) rather than importing `cli`. The reason is documented in the implementation comment. `_load_finalized_agent` in `cli.py` imports `_wiring.resolve_agent` lazily (inside the function body, `from ._wiring import ...`), which is the existing pattern for the `cli → _wiring` direction.

**`_ws_for_template` helper:** The deploy path must use ws (not hardcode `ws=None`) so template-built models are grounded at deploy time. The lazy-ws logic is extracted into `_ws_for_template(config)` in `_wiring.py` so both `_load_finalized_agent` (CLI) and the deploy bare-`_load_agent` site use identical try/except degradation. The warning on failure is the user's signal that their deployed model will ship ungrounded.

**`TemplateConfigError(ValueError)`:** Named to avoid collision with any future bare `ConfigError`. Currently only `ToolConfigError` exists (`_tool_config.py:21`). `TemplateConfigError` is an internal `_wiring` symbol — no `__all__` entry. Both `TemplateConfigError` and `resolve_agent` are importable from `apx_agent._wiring`; the test in T5 verifies this.

**No pre-emptive `type: ignore`:** `template_registry.build()` returns `Any`; `getattr(mod, var)` returns `Any`. Both are compatible with `BaseAgent` without annotation suppression. Pre-adding `# type: ignore[return-value]` risks a `reportUnnecessaryTypeIgnoreComment` error on the pyright gate. Add only if pyright actually flags the return on the T2 step-4 run.

**`create_app` body verified:** The `agent` parameter is used exclusively inside the lifespan closure (lines 607–617); `create_app` itself ends with `return FastAPI(lifespan=lifespan)` at line 640 with no pre-lifespan agent access. Making `agent: BaseAgent | None = None` is safe.

**Pyright gate:** `_models.py`, `_wiring.py`, `_template.py`, `_inspection.py` are NOT in the type-debt exclude list. The `template: dict[str, Any] | None = None` field requires `Any` in `_models.py`'s imports — confirm `Any` is already in the `typing` import or add it. `cli.py` IS excluded — don't add new errors there.

**Test realism — ws=None:** All serve-path and CLI tests build `DataTemplate` with `ws=None` (no live Databricks creds in test). A ws-less `DataAgent` has exactly one tool: `sql_tool()` with `__name__ == "run_sql"` (confirmed in `sql_tools.py:26-27`). All assertions use `"run_sql"`, not UC-function tool names that only appear after live schema introspection.

**Inspect-before-edit flags for the implementer:**
- `_models.py` existing `typing` import line — confirm `Any` is present before adding the `template` field annotation.
- `_wiring.py` `create_app` exact signature and lifespan lines 607/617 — read before editing `agent` → optional and the `mount_invocations_route` arg swap.
- `setup_agent` line 172 — read the existing signature before changing `agent: BaseAgent` → `agent: BaseAgent | None`.
- `cli.py` lines 1789–1793 — read the exact surrounding `_load_agent` call and variable names before patching to `resolve_agent`.
- `apx info` `module` argument default (cli.py:~3025–3042) — confirm whether `--module` is required or has a default to write the CLI test correctly.

**Out of scope for E3a:**
- Scaffold changes for template-only project layout (`app.py` without `agent.py`).
- E3b memory/session declarative config.
- `apx run` ASGI entry-point for template-only projects.
- Per-leaf tool targeting for template-built composition roots.

# Sub-project 3b-3b: `generate_project` writes Python and `[tool.apx.deploy]` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project generated from a YAML spec carries every runtime setting as keyword arguments on the root agent in `agent.py`, and only `[tool.apx.deploy]` in `pyproject.toml`. A read-back test proves the effective config equals the spec.

**Architecture:**
1. Settings get ready to arrive from generated code:
   - `_set_settings` validates values into their models;
   - `workflows`, `data`, `executor` and `example` join the app-wide settings;
   - every agent class accepts `description`, which merges TOML-first like `memory`/`session`.
2. `_agent_config_fields` fills an undeclared `target` from a static read of `agent.py`, so deploy routes correctly before import.
3. `render_agent_py` appends the declared settings to the root constructor; graph roots reject the `LlmAgent`-only fields.
4. `_build_pyproject` writes `[project]` and `[tool.apx.deploy]` only.

**Tech Stack:** Python ≥3.10, pydantic v2 (`TypeAdapter`), pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3b-3b: generators write Python and `[tool.apx.deploy]`" (the `generate_project` part; the scaffold templates are 3b-3c).

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`; `pre-commit run --files <changed files>`; the PR CI subset under xdist. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **Graph-root error text, verbatim:** `{field} can't go on a graph root; set it on a leaf in agents:`
- **`description` merges TOML-first, with no conflict.** The TOML wins, and the root agent fills the gap. An empty description isn't a declaration.
- **Spec-only inputs never become settings:** `tools`, `skills`, `agents`, `root`, `template`.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Run `git commit --quiet --message …` alone in its command. Never `git stash`.

## Review Focus

1. **A YAML spec with a third-party template** (not in `_TEMPLATE_TARGETS`), built through `template_registry.build(...)`. Expected: settings are still applied, and the read-back still matches. Pinned in Task 3.
2. **A durable spec with no explicit session** (auto-filled managed session). Expected: no `session=` is emitted, because the auto-filled session isn't declared, and the read-back's session still equals the spec's. Pinned in Task 3.
3. **A spec whose `target` is `responses_agent`.** Expected: deploy reads it before import, from the static `target=` in `agent.py`. Pinned in Task 2.
4. **`service_policies` with attachments and `bindings` with dict bindings.** Expected: they round-trip through `repr` into Python and back through validation to equal models. Pinned in Task 3.
5. **An agent whose `description` is set in both the TOML and Python, with different text.** Expected: the TOML wins, with no error (unchanged behavior). Pinned in Task 1.

---

### Task 1: Settings ready for generated code

**Files:**
- Modify: `python/src/apx_agent/_agents.py`:
  - `AgentSettings`: add `workflows`, `data`, `executor`, `example`;
  - add `CompositeSettings`, which also carries `description`;
  - `BaseAgent`: new class attributes, `_description`, and `_set_settings` normalization;
  - the composite constructors use `Unpack[CompositeSettings]`.
- Modify: `python/src/apx_agent/_settings.py`: `APP_WIDE`, `_ROOT_ONLY`.
- Modify: `python/src/apx_agent/cli.py`: drop `_effective_config`'s `description` fill, now that `resolve_settings` merges it.
- Test: `python/tests/test_generated_settings.py` (create)

**Interfaces:**
- Produces:
  - `AgentSettings` gains `workflows: list[ExampleWorkflow]`, `data: DataConfig`, `executor: str` and `example: ExampleBackendConfig`.
  - `CompositeSettings(AgentSettings)` adds `description: str`.
  - `_set_settings` validates every value with `TypeAdapter(AgentConfig.model_fields[key].annotation)`.
  - `resolve_settings` merges `description` from the root's `_description`, with the TOML winning.

- [ ] **Step 1: Write the failing tests** (`python/tests/test_generated_settings.py`)

```python
"""Generated agent.py settings (sub-project 3b-3b)."""

from __future__ import annotations

import pytest

from apx_agent import Agent, SequentialAgent
from apx_agent._models import AgentConfig, ServicePoliciesConfig
from apx_agent._settings import resolve_settings


def test_dict_settings_become_models() -> None:
    agent = Agent(name="a", tools=[], service_policies={"local_mode": "mirror", "native_mode": "off"})
    assert isinstance(agent.service_policies, ServicePoliciesConfig)


@pytest.mark.parametrize(("key", "value"), [
    ("workflows", [{"name": "w", "question": "q?"}]),
    ("executor", "langgraph"),
    ("data", {"tables": []}),
])
def test_yaml_only_fields_are_app_wide_settings(key: str, value) -> None:
    config = resolve_settings(Agent(name="a", tools=[], **{key: value}), None)
    assert getattr(config, key) == getattr(AgentConfig.model_validate({"name": "x", key: value}), key)


def test_composite_roots_take_a_description() -> None:
    root = SequentialAgent([Agent(name="leaf", tools=[])], description="Routes orders.")
    assert resolve_settings(root, None).description == "Routes orders."


def test_description_toml_wins_without_conflict() -> None:
    toml = AgentConfig.model_validate({"name": "a", "description": "from toml"})
    assert resolve_settings(Agent(name="a", tools=[], description="from python"), toml).description == "from toml"


def test_an_empty_description_is_not_a_declaration() -> None:
    assert "description" not in resolve_settings(Agent(name="a", tools=[]), None).model_fields_set
```

Check field shapes before running:
- `ExampleWorkflow`'s required fields (`grep -n "class ExampleWorkflow" -A8 python/src/apx_agent/_models.py`);
- `DataConfig`'s minimal valid value (`grep -n "class DataConfig" -A10 python/src/apx_agent/_models.py`).

Adjust the parametrized values to the smallest valid ones, keeping the intent: each field round-trips as an app-wide setting.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_generated_settings.py -q -p no:cacheprovider`
Expected: FAIL. The dict isn't converted to a model, `workflows`/`data`/`executor` are unknown settings, and `SequentialAgent` rejects `description`.

- [ ] **Step 3: Implement.**

`_agents.py`:
- Import `DataConfig`, `ExampleBackendConfig` and `ExampleWorkflow` from `._models` (add them to the existing import list).
- Add to `AgentSettings`:

```python
    workflows: list[ExampleWorkflow]
    data: DataConfig
    executor: str
    example: ExampleBackendConfig
```

- After `AgentSettings`, add:

```python
class CompositeSettings(AgentSettings, total=False):
    """AgentSettings plus ``description``, which LlmAgent takes as its own argument."""

    description: str
```

- On `BaseAgent`, add these class attributes next to the others:

```python
    workflows: list[ExampleWorkflow] | None = None
    data: DataConfig | None = None
    executor: str | None = None
    example: ExampleBackendConfig | None = None
    _description: str | None = None
```

- Change `_set_settings(self, settings: AgentSettings)` to accept `CompositeSettings`. Check unknown keys against `CompositeSettings.__annotations__`, and replace the final loop with:

```python
        from pydantic import TypeAdapter

        from ._models import AgentConfig

        for key, value in settings.items():
            if key == "description":
                self._description = value  # same attribute LlmAgent uses
                continue
            # Generated agent.py passes dicts; validate into the AgentConfig field's type.
            setattr(self, key, TypeAdapter(AgentConfig.model_fields[key].annotation).validate_python(value))
```

  `CompositeSettings.__annotations__` includes the inherited keys at runtime. If it doesn't, use `{**AgentSettings.__annotations__, "description": str}`.
- In each composite constructor (`LoopAgent`, `SequentialAgent`, `ParallelAgent`, `RouterAgent`, `HandoffAgent`, `KeywordRouter`), change `**settings: Unpack[AgentSettings]` to `**settings: Unpack[CompositeSettings]`. `LlmAgent` keeps `Unpack[AgentSettings]`, because `description` is its own explicit parameter.

`_settings.py`:
- Append `"workflows", "data", "executor", "example"` to `APP_WIDE`.
- Add `"description": "_description"` to `_ROOT_ONLY`, so it fills gaps and the TOML wins.
- In the `_ROOT_ONLY` comprehension, treat an empty string as unset by adding `and getattr(agent, attr) != ""`.

`cli.py` `_effective_config`: delete the block that fills `description` from `agent._description` (from "description is a constructor argument" through `config = config.model_copy(...)`). `resolve_settings` now does it.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_generated_settings.py tests/test_effective_config.py tests/test_resolve_settings.py tests/test_agent_settings.py tests/test_agents.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_agents.py python/src/apx_agent/_settings.py python/src/apx_agent/cli.py python/tests/test_generated_settings.py
git commit --quiet --message "feat: settings validate into models; workflows/data/executor/example and description on any agent

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: Deploy reads `target` statically when the TOML omits it

**Files:**
- Modify: `python/src/apx_agent/_inspection.py` (`_agent_config_fields`)
- Test: `python/tests/test_generated_settings.py`

**Interfaces:**
- Consumes: `static_agent_setting(agent_py, key)` (3b-2).

- [ ] **Step 1: Write the failing test** (append)

```python
def test_loader_takes_target_from_agent_py_when_toml_omits_it(tmp_path) -> None:
    from apx_agent._inspection import _load_agent_config

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nhost = "apps"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', target='responses_agent', tools=[])\n")
    assert _load_agent_config(pyproject_path=tmp_path / "pyproject.toml").target == "responses_agent"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_generated_settings.py -q -p no:cacheprovider -k target_from_agent_py`
Expected: FAIL. The target is `durable_agent_server` (the default).

- [ ] **Step 3: Implement.** In `_agent_config_fields`, after `fields = _fold_deploy_table(data, fields)`:

```python
        if fields and "target" not in fields:
            # Deploy routes on target before importing agent.py; read a literal one statically.
            target = static_agent_setting(path.parent / "agent.py", "target")
            if target is not None:
                fields["target"] = target
```

(`static_agent_setting` is defined later in the same module, so the call resolves at runtime.)

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_generated_settings.py tests/test_deploy_table.py tests/test_cli_metadata.py tests/test_effective_config.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_inspection.py python/tests/test_generated_settings.py
git commit --quiet --message "feat: config loader reads a literal target from agent.py when the TOML omits it

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: `render_agent_py` emits settings; `_build_pyproject` drops the agent table

**Files:**
- Modify: `python/src/apx_agent/_project_gen.py`:
  - `render_agent_py` [~632];
  - `_render_graph_agent_py` [~590];
  - `_build_pyproject` [~106-250];
  - a new `_settings_kwargs` helper.
- Test: `python/tests/test_generated_settings.py`; update `python/tests/test_project_gen.py` where it asserts `[tool.apx.agent]` content.

**Interfaces:**
- Produces: `_settings_kwargs(config: AgentConfig, *, llm_root: bool) -> dict[str, Any]`, the declared runtime fields to emit, as JSON-able values.

- [ ] **Step 1: Write the failing tests** (append)

```python
import sys
import tomllib
from pathlib import Path

from apx_agent._project_gen import generate_project

_SPECS = {
    "plain": {"name": "plain", "model": "system.ai.claude-sonnet-4-6", "instructions": "Be brief.",
              "description": "A plain agent.", "examples": ["Hi"], "identity": "obo"},
    "coworker": {"name": "cw", "model": "m", "template": {"name": "coworker", "catalog": "main", "schema": "hr"}},
    "responses": {"name": "resp", "model": "m", "target": "responses_agent",
                  "session": {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "resp"}},
    "policies": {"name": "pol", "model": "m",
                 "service_policies": {"local_mode": "mirror", "native_mode": "off"},
                 "bindings": {"wh": "warehouse-id"}},
}


def _load_generated(root: Path, monkeypatch):
    import importlib.util

    from apx_agent._inspection import _load_agent_config

    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("agent", root / "agent.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "agent", module)
    spec.loader.exec_module(module)
    return resolve_settings(module.agent, _load_agent_config(pyproject_path=root / "pyproject.toml"))


@pytest.mark.parametrize("key", sorted(_SPECS))
def test_generated_project_reads_back_as_its_spec(tmp_path: Path, monkeypatch, key: str) -> None:
    from ctk import Artifact, verify

    spec = AgentConfig.model_validate(_SPECS[key])
    generate_project(spec, tmp_path)
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "agent" not in data["tool"]["apx"], "generated pyproject must not declare [tool.apx.agent]"
    verify(Artifact(str(tmp_path / "agent.py"), min_bytes=40, must_contain=f"name={spec.name!r}"))
    effective = _load_generated(tmp_path, monkeypatch)
    for field in spec.model_fields_set - {"template", "tools", "skills", "agents", "root"}:
        assert getattr(effective, field) == getattr(spec, field), field


def test_graph_root_rejects_llm_only_fields(tmp_path: Path) -> None:
    spec = AgentConfig.model_validate({
        "name": "g", "model": "m", "instructions": "top-level",
        "agents": {"a": {"instructions": "leaf"}}, "root": {"type": "sequential", "agents": ["a"]},
    })
    with pytest.raises(ValueError, match="instructions can't go on a graph root; set it on a leaf in agents:"):
        generate_project(spec, tmp_path)
```

Before running, check:
- the YAML graph shape `AgentConfig` accepts for `agents`/`root` (`grep -n "agents:\|root:" python/src/apx_agent/_models.py | head` and `grep -n "def test_.*graph" -A15 python/tests/test_project_gen.py | head -40`). Make the graph spec use the real accepted shape, leaving the intent unchanged.
- whether the coworker template needs `.apx/schema.json` or a workspace at import. If importing the generated `agent.py` needs more, follow how `test_project_gen.py` imports generated coworker projects (`grep -n "exec(\|import_module\|spec_from_file" python/tests/test_project_gen.py`).

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_generated_settings.py -q -p no:cacheprovider -k "reads_back or graph_root"`
Expected: FAIL. `[tool.apx.agent]` is still generated, and the graph root accepts `instructions`.

- [ ] **Step 3: Implement.** In `_project_gen.py`:

```python
_SPEC_ONLY = frozenset({"tools", "skills", "agents", "root", "template", "deploy", "host", "name"})
_LLM_ONLY = ("instructions", "temperature", "max_tokens", "max_iterations", "sub_agents", "memory", "session")


def _settings_kwargs(config: "AgentConfig", *, llm_root: bool) -> dict[str, Any]:
    """Declared runtime fields of *config*, as constructor kwargs for the root agent (3b-3b)."""
    out: dict[str, Any] = {}
    for field in sorted(config.model_fields_set - _SPEC_ONLY):
        value = getattr(config, field)
        if value is None:
            continue
        if field in _LLM_ONLY and not llm_root:
            raise ValueError(f"{field} can't go on a graph root; set it on a leaf in agents:")
        out[field] = value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else (
            [v.model_dump(mode="json", exclude_none=True) if hasattr(v, "model_dump") else v for v in value]
            if isinstance(value, list) else value)
    return out
```

- Graph roots (`_render_graph_agent_py`): after `root = _render_root(...)`, compute `extra = _settings_kwargs(config, llm_root=False)`. If it's non-empty, rewrite the root line by inserting the kwargs before its final `)`: `root = root[:-1] + ", " + _ctor_kwargs(extra) + ")"`.
- Plain `LlmAgent` (no template): change the emitted line to `agent = LlmAgent(name=…, tools=…, {_ctor_kwargs(_settings_kwargs(config, llm_root=True))})`. Strip the trailing `, ` when it's empty.
- Known templates (`_TEMPLATE_TARGETS`): merge `_settings_kwargs(config, llm_root=True)` into `spec` before `_ctor_kwargs(spec)`. Keys the template spec already sets win: `spec.setdefault(k, v)`.
- Third-party templates (`template_registry.build(...)`): after the build line, append

```python
agent._set_settings({...})  # runtime settings (sub-project 3b-3b)
```

  …rendered with `repr(_settings_kwargs(config, llm_root=True))`, and only when non-empty. Record a ruling: generated code calls a private method because `build()` takes no settings.
- `_build_pyproject`: delete the `[tool.apx.agent]` section and all `tool.apx.agent.*` sub-sections (bindings, service_policies, memory, session). Keep `[project]` and the dependency-extras logic. Write:

```python
    deploy_table: dict[str, Any] = {"name": config.name}
    if config.host is not None:
        deploy_table["host"] = config.host
    if config.deploy is not None:
        deploy_table.update(config.deploy.model_dump(exclude_none=True, exclude={"autoscale"}))
    _write_toml_section(lines, "tool.apx.deploy", deploy_table)
    if config.deploy is not None and config.deploy.autoscale is not None:
        _write_toml_section(lines, "tool.apx.deploy.autoscale", config.deploy.autoscale.model_dump())
```

  `DeployConfig` defaults such as `backend="agentbricks"` and empty `include`/`env` are written only if declared. Use `config.deploy.model_dump(exclude_none=True, exclude_unset=True, exclude={"autoscale"})` so defaults stay out of the TOML.

- [ ] **Step 4: Update existing tests.** `tests/test_project_gen.py` asserts the old `[tool.apx.agent]` content, for example `project["tool"]["apx"]["agent"]["memory"]["store_name"]`. Update each assertion to read the same value from the generated project's effective config, via the `_load_generated` helper copied into that file, or from `[tool.apx.deploy]` for deploy fields. List every assertion you change in the report. Keep each assertion's intent; only the source moves.

Run: `cd python && uv run pytest tests/test_generated_settings.py tests/test_project_gen.py tests/test_cli_scaffold_yaml.py tests/test_cli_deploy_yaml.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_project_gen.py python/tests/test_generated_settings.py python/tests/test_project_gen.py
git commit --quiet --message "feat: generate_project writes settings into agent.py and only [tool.apx.deploy]

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: Gate, docs, changelog

- [ ] **Step 1: Gate.** Run `make check`; `pre-commit run --files $(git diff --name-only origin/main...HEAD)`; the PR CI subset under xdist; and `uv run pyright` on the changed modules. Expected: all green. Any generated-project test failing elsewhere (for example `scaffolds-a-runnable-project`, or the data-inspector template check) is in scope: fix it here.
- [ ] **Step 2: Changelog** under `### Added`:

```markdown
- **Generated projects declare settings in Python.** `generate_project` (YAML specs) writes every runtime setting as keyword arguments on the root agent in `agent.py`, and only `[tool.apx.deploy]` in `pyproject.toml`. Every agent class accepts `description`, and `workflows`, `data`, `executor` and `example` are app-wide settings. Settings passed as dicts validate into their models. The config loader reads a literal `target` from `agent.py` when the TOML omits it.
```

- [ ] **Step 3: Commit** (its own command)

```bash
git add CHANGELOG.md
git commit --quiet --message "docs: generated projects declare settings in Python

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- The `scaffold` templates (3b-3c).
- Migrating the in-repo projects (3c), and rejecting `[tool.apx.agent]` (3d).

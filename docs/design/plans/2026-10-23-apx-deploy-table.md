# Sub-project 3a: homes for the last `[tool.apx.agent]` fields — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy settings get a `[tool.apx.deploy]` table, dev-UI `examples` become an agent setting, and `apx-agent agents migrate-config` moves a project off `[tool.apx.agent]` safely.

**Architecture:**
- `_inspection._agent_config_fields` folds `[tool.apx.deploy]` into the fields it already returns, so `_load_agent_config`, project detection and doctor see one config.
- `examples` joins `AgentSettings`/`APP_WIDE`, and `setup_agent` resolves settings so the dev UI shows Python-declared examples.
- A new `_migrate_config.py` plans the migration as data:
  - which deploy table to write;
  - which runtime fields Python doesn't declare yet;
  - whether `[tool.apx.agent]` can go.
- The CLI command applies the plan as a text edit, then reads the result back.

**Tech Stack:** Python ≥3.10, `tomllib`, pydantic v2 (`AgentConfig`, `DeployConfig`), click, pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3a: homes for the last fields".

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`, plus `pre-commit run --files <changed files>`. `--all-files` fails on main for unrelated example lint debt.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **No new dependencies.** There's no `tomlkit`; TOML is written with `_project_gen._write_toml_section` and `_toml_value`.
- **Error text, verbatim:**
  - Conflict: `{field} is declared in both [tool.apx.agent{suffix}] and [tool.apx.deploy] with different values; keep [tool.apx.deploy].`, where `{field}` is `name`, `host` or `deploy.<key>`, and `{suffix}` is `""` for `name`/`host` and `".deploy"` for deploy keys.
  - Dirty file: `pyproject.toml has uncommitted changes; commit them first (git is migrate-config's undo).`
  - Module: `module = {value!r} is not supported; rename it so the agent is agent:agent.`
  - Template: `template = {...} is not supported; generate an agent.py from the template first.`
  - YAML-only field: `{field} belongs in the YAML spec, not [tool.apx.agent].`
  - Read-back: `migrate-config changed the effective config; restored pyproject.toml.`
- **`name` default:** `[project].name`, only when neither table sets `name`.
- **Behavior must not change** for projects without `[tool.apx.deploy]`, and the existing suite stays green.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Use long flags (`git commit --quiet --message …`), because a hook misreads `-qm`. Never `git stash`.

## Review Focus

1. **A project with only `[tool.apx.deploy]` and no `agent.py`** (a deploy-only template project). Expected: the loader returns a config named after `[project].name`, with no crash about a missing agent. Pinned in Task 1.
2. **`[tool.apx.deploy]` with an empty `[tool.apx.agent]` table** (header but no keys). Expected: it behaves as deploy-only, with no conflict. Pinned in Task 1.
3. **Running `migrate-config` twice after it has fully migrated** (no `[tool.apx.agent]` left). Expected: a no-op success message, with the file unchanged. Pinned in Task 4.
4. **A `pyproject.toml` with a multi-line array in `[tool.apx.agent]`** (for example `examples = [\n "a",\n]`). Expected: the text cut removes the whole section, and the next table survives intact. Pinned in Task 4.
5. **`APX_AGENT_NAME` set at runtime while `[tool.apx.deploy].name` is set.** Expected: the env override still wins for the runtime name, as it does today. Pinned in Task 1.

---

### Task 1: The loader folds `[tool.apx.deploy]`

**Files:**
- Modify: `python/src/apx_agent/_inspection.py` (`_agent_config_fields`, ~line 207)
- Modify: `python/src/apx_agent/_doctor_durable.py:~265` (raw `declared = …get("agent")` detection)
- Test: `python/tests/test_deploy_table.py` (create)

**Interfaces:**
- Produces: `_agent_config_fields(path, section_path)` returns the agent fields with `[tool.apx.deploy]` folded in, when `section_path == ("tool", "apx", "agent")`. It returns `None` only when neither table declares anything. It raises `ValueError` on a conflict. `_load_agent_config` is unchanged and inherits this.

- [ ] **Step 1: Write the failing tests** (`python/tests/test_deploy_table.py`)

```python
"""[tool.apx.deploy]: deploy settings outside [tool.apx.agent] (sub-project 3a)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent._inspection import _load_agent_config


def _project(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "orders-agent"\nversion = "0"\n\n' + body)
    return path


def test_deploy_table_alone_builds_a_config(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\nhost = "apps"\ninstances = 2\n')
    config = _load_agent_config(pyproject_path=path)
    assert config is not None
    assert (config.name, config.host) == ("orders-agent", "apps")
    assert config.deploy is not None and config.deploy.instances == 2


def test_deploy_table_name_wins_over_project_name(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\nname = "custom"\n')
    assert _load_agent_config(pyproject_path=path).name == "custom"


def test_agent_table_name_is_not_overridden_by_the_default(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\nname = "legacy"\n\n[tool.apx.deploy]\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("legacy", "s")


def test_equal_values_in_both_tables_pass(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "s"\n\n'
                              '[tool.apx.deploy]\nname = "a"\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("a", "s")


@pytest.mark.parametrize(("body", "message"), [
    ('[tool.apx.agent]\nname = "a"\n\n[tool.apx.deploy]\nname = "b"\n',
     r"name is declared in both \[tool.apx.agent\] and \[tool.apx.deploy\] with different values; keep \[tool.apx.deploy\]."),
    ('[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "x"\n\n[tool.apx.deploy]\nspace = "y"\n',
     r"deploy.space is declared in both \[tool.apx.agent.deploy\] and \[tool.apx.deploy\] with different values; keep \[tool.apx.deploy\]."),
])
def test_different_values_fail(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _load_agent_config(pyproject_path=_project(tmp_path, body))


def test_neither_table_is_none(tmp_path: Path) -> None:
    assert _load_agent_config(pyproject_path=_project(tmp_path, "")) is None


def test_empty_agent_table_with_deploy_table_is_deploy_only(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\n\n[tool.apx.deploy]\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("orders-agent", "s")


def test_deploy_validation_fires_from_the_new_table(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\ninstances = 2\n\n[tool.apx.deploy.autoscale]\nmin = 1\nmax = 3\n')
    with pytest.raises(ValueError):
        _load_agent_config(pyproject_path=path)


def test_runtime_name_env_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_AGENT_NAME", "from-env")
    path = _project(tmp_path, '[tool.apx.deploy]\nname = "custom"\n')
    assert _load_agent_config(pyproject_path=path).name == "from-env"


def test_durable_doctor_reads_a_deploy_only_project(tmp_path: Path) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    (root / "agent.py").write_text("from apx_agent import LlmAgent\nagent = LlmAgent(name='p', tools=[])\n")
    checks = {c.name: c for c in check_durable_readiness(root)}
    assert checks["Config"].status is Status.OK
```

Before running, confirm that `DeployConfig` rejects `instances` together with `autoscale`: `grep -n "instances.*autoscale\|XOR" python/src/apx_agent/_models.py`. If the autoscale field names aren't `min`/`max`, use the real names from `AutoscaleConfig`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_deploy_table.py -q -p no:cacheprovider`
Expected: FAIL. The deploy-only tests get `None`, and the conflict tests don't raise.

- [ ] **Step 3: Implement.** In `_inspection.py`, replace `_agent_config_fields` with:

```python
_AGENT_SECTION = ("tool", "apx", "agent")


def _fold_deploy_table(data: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Merge ``[tool.apx.deploy]`` (name, host, DeployConfig keys) into agent *fields*."""
    table = data.get("tool", {}).get("apx", {}).get("deploy")
    if not table:
        return fields
    merged = dict(fields)
    deploy = dict(merged.get("deploy", {}))
    for key, value in table.items():
        if key in ("name", "host"):
            target, label, suffix = merged, key, ""
        else:
            target, label, suffix = deploy, f"deploy.{key}", ".deploy"
        if key in target and target[key] != value:
            raise ValueError(f"{label} is declared in both [tool.apx.agent{suffix}] and [tool.apx.deploy] "
                             "with different values; keep [tool.apx.deploy].")
        target[key] = value
    if deploy:
        merged["deploy"] = deploy
    if "name" not in merged:
        name = data.get("project", {}).get("name")
        if name:
            merged["name"] = name
    return merged


def _agent_config_fields(path: Path, section_path: tuple[str, ...]) -> dict[str, Any] | None:
    """AgentConfig fields declared at ``section_path`` in *path*, or None.

    For the APX section, ``[tool.apx.deploy]`` is folded in (sub-project 3a)."""
    import tomllib

    with open(path, "rb") as f:
        data = tomllib.load(f)
    section = data
    for key in section_path:
        section = section.get(key, {})
        if not section:
            break
    fields = {k: v for k, v in section.items() if k in AgentConfig.model_fields} if section else {}
    if section_path == _AGENT_SECTION:
        fields = _fold_deploy_table(data, fields)
    # Deploy-envelope keys alone do not declare an agent config.
    return fields if fields else None
```

Note: with only `[project]` and no apx tables, `_fold_deploy_table` returns the empty `fields` unchanged (no deploy table, so no name default), and the function returns `None`. That's what `test_neither_table_is_none` requires.

In `_doctor_durable.py` (~line 265), the Config stage detects "declared" from the raw agent table. Replace:

```python
    declared = data.get("tool", {}).get("apx", {}).get("agent")
```

with:

```python
    apx = data.get("tool", {}).get("apx", {})
    declared = apx.get("agent") or apx.get("deploy")
```

Keep the rest of that stage. Its `.toml` branch already calls `_agent_config_fields(project / "pyproject.toml", ("tool", "apx", "agent"))`, which now folds the deploy table in.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_deploy_table.py tests/test_inspection.py tests/test_doctor_durable.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Deploy reads the table before importing the agent.** Add this test to `python/tests/test_deploy_apps.py`, next to `test_agent_prerequisites_block_deployment_before_upload`, reusing its `scaffold` fixture and helpers:

```python
def test_deploy_table_only_prerequisites_block_before_import(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent import LlmAgent
    from databricks.sdk.errors import DatabricksError

    (scaffold / "databricks.yml").unlink()
    (scaffold / "pyproject.toml").write_text(
        '[project]\nname = "my-app"\nversion = "0"\n\n'
        '[tool.apx.deploy]\nhost = "apps"\n\n'
        '[tool.apx.agent.memory]\ntype = "managed"\nstore_name = "agent-memory"\n')
    calls = _install_subprocess_mock(monkeypatch)
    ws = MagicMock()
    ws.api_client.do.side_effect = DatabricksError("hidden backend detail", error_code="FEATURE_DISABLED")
    monkeypatch.setattr("apx_agent.cli._make_scaffold_workspace_client", lambda profile: ws)
    loaded = MagicMock(return_value=LlmAgent(name="my-app"))
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", loaded)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps", "--profile", "chosen"])
    assert result.exit_code != 0
    loaded.assert_not_called()
    assert not any(call[:2] in (["bundle", "deploy"], ["bundle", "run"]) for call in calls)
```

Read the existing test first (`grep -n "def test_agent_prerequisites_block_deployment_before_upload" -A45 python/tests/test_deploy_apps.py`). If the existing scaffold needs an `agent.py` or the generated `start_server.py`, copy those setup lines in. Run: `cd python && uv run pytest tests/test_deploy_apps.py -q -p no:cacheprovider -k "prerequisites"`. Expected: PASS. It may already pass after Step 3, because the loader now sees the memory declaration through the folded config. A pass here confirms the seam, so don't change production code for it.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/_inspection.py python/src/apx_agent/_doctor_durable.py python/tests/test_deploy_table.py python/tests/test_deploy_apps.py
git commit --quiet --message "feat: [tool.apx.deploy] holds deploy settings outside [tool.apx.agent]

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: `examples=` is an agent setting

**Files:**
- Modify: `python/src/apx_agent/_agents.py` (`AgentSettings` TypedDict and `BaseAgent` class attributes)
- Modify: `python/src/apx_agent/_settings.py` (`APP_WIDE`)
- Modify: `python/src/apx_agent/_wiring.py` (`setup_agent`, after the `agent = resolve_agent(...)` block, ~line 770)
- Test: `python/tests/test_resolve_settings.py`

**Interfaces:**
- Consumes: `resolve_settings(agent, toml) -> AgentConfig`.
- Produces: `examples: list[str]` in `AgentSettings`; `BaseAgent.examples: list[str] | None = None`; `"examples"` in `APP_WIDE`.

- [ ] **Step 1: Write the failing tests** (append to `python/tests/test_resolve_settings.py`)

```python
def test_examples_round_trip_and_are_app_wide() -> None:
    from apx_agent._settings import SettingsConflict

    assert resolve_settings(_leaf(examples=["Top 5 customers"]), None).examples == ["Top 5 customers"]
    with pytest.raises(SettingsConflict, match="examples is app-wide"):
        resolve_settings(SequentialAgent([_leaf("child", examples=["x"])]), None)


def test_dev_ui_context_carries_python_examples() -> None:
    from fastapi import FastAPI

    from apx_agent._wiring import setup_agent

    app = FastAPI()
    agent = _leaf("demo", examples=["Show me sample customers"])
    ctx = setup_agent(app, agent=agent, config=AgentConfig(name="demo"))
    assert ctx is not None and ctx.config.examples == ["Show me sample customers"]
```

Check `setup_agent`'s signature first: `grep -n "^def setup_agent" -A8 python/src/apx_agent/_wiring.py`. Adjust the call's keyword names to match (it takes `app`, `agent`, `config`, `pyproject_path`).

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_resolve_settings.py -q -p no:cacheprovider -k "examples"`
Expected: FAIL with `TypeError: unknown agent setting(s): examples`.

- [ ] **Step 3: Implement.**
  - In `_agents.py`'s `AgentSettings`, add `examples: list[str]`.
  - On `BaseAgent`, add `examples: list[str] | None = None` next to the other settings attributes.
  - In `_settings.py`, add `"examples"` to the end of `APP_WIDE`.
  - In `_wiring.py`'s `setup_agent`, directly after the `if agent is None: agent = resolve_agent(...)` block, add:

```python
    from ._settings import resolve_settings  # noqa: PLC0415

    config = resolve_settings(agent, config)
```

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_resolve_settings.py tests/test_dev_ui_routes.py tests/test_agent_settings.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agents.py python/src/apx_agent/_settings.py python/src/apx_agent/_wiring.py python/tests/test_resolve_settings.py
git commit --quiet --message "feat: dev-UI examples are an agent setting

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Plan the migration (`_migrate_config.py`)

**Files:**
- Create: `python/src/apx_agent/_migrate_config.py`
- Test: `python/tests/test_migrate_config.py` (create)

**Interfaces:**
- Consumes: `_inspection._load_agent_config`, `_settings.resolve_settings`, `_project_gen._write_toml_section`.
- Produces:
  - `@dataclass(frozen=True) class Migration: text: str; missing: dict[str, Any]; removed_agent_table: bool`
  - `plan_migration(pyproject: Path, agent: BaseAgent) -> Migration`. It raises `ValueError` with the Global Constraints messages for `module =`, `template =` and YAML-only fields. `agent` is the *raw* imported agent, with no `finalize_agent` run.
  - `cut_sections(text: str, prefix: str) -> str`, which removes every `[prefix]` and `[prefix.*]` section.
  - `snippet(missing: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing tests** (`python/tests/test_migrate_config.py`)

```python
"""migrate-config: move a project off [tool.apx.agent] (sub-project 3a)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from apx_agent import Agent
from apx_agent._migrate_config import cut_sections, plan_migration, snippet

_PYPROJECT = '''[project]
name = "inspector"
version = "0"

[tool.apx.agent]
name = "inspector"
host = "apps"
model = "system.ai.claude-sonnet-4-6"
instructions = "Inspect tables."
examples = [
    "Show tables",
    "Count rows",
]

[tool.apx.agent.deploy]
space = "shared"

[tool.other]
keep = true
'''


def _write(tmp_path: Path, text: str = _PYPROJECT) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(text)
    return path


def test_cut_sections_removes_table_and_subtables_including_multiline_arrays() -> None:
    out = cut_sections(_PYPROJECT, "tool.apx.agent")
    data = tomllib.loads(out)
    assert "agent" not in data["tool"].get("apx", {})
    assert data["tool"]["other"] == {"keep": True}
    assert data["project"]["name"] == "inspector"


def test_first_run_moves_deploy_fields_and_lists_missing_runtime_fields(tmp_path: Path) -> None:
    path = _write(tmp_path)
    migration = plan_migration(path, Agent(name="inspector", tools=[]))
    data = tomllib.loads(migration.text)
    assert data["tool"]["apx"]["deploy"] == {"name": "inspector", "host": "apps", "space": "shared"}
    assert "deploy" not in data["tool"]["apx"]["agent"]
    assert "host" not in data["tool"]["apx"]["agent"]
    assert migration.removed_agent_table is False
    assert migration.missing == {
        "model": "system.ai.claude-sonnet-4-6", "instructions": "Inspect tables.",
        "examples": ["Show tables", "Count rows"],
    }


def test_table_is_removed_once_python_declares_everything(tmp_path: Path) -> None:
    path = _write(tmp_path)
    agent = Agent(name="inspector", tools=[], model="system.ai.claude-sonnet-4-6",
                  instructions="Inspect tables.", examples=["Show tables", "Count rows"])
    migration = plan_migration(path, agent)
    data = tomllib.loads(migration.text)
    assert migration.removed_agent_table is True and migration.missing == {}
    assert "agent" not in data["tool"]["apx"]
    assert data["tool"]["apx"]["deploy"]["space"] == "shared"


def test_fully_migrated_project_is_a_no_op(tmp_path: Path) -> None:
    text = '[project]\nname = "inspector"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "shared"\n'
    path = _write(tmp_path, text)
    migration = plan_migration(path, Agent(name="inspector", tools=[]))
    assert migration.text == text and migration.missing == {} and migration.removed_agent_table is False


@pytest.mark.parametrize(("extra", "message"), [
    ('module = "bot:assistant"\n', r"module = 'bot:assistant' is not supported; rename it so the agent is agent:agent."),
    ('template = { name = "x" }\n', r"template = \{...\} is not supported; generate an agent.py from the template first."),
    ('executor = "openai-compat"\n', r"executor belongs in the YAML spec, not \[tool.apx.agent\]."),
])
def test_unsupported_keys_fail(tmp_path: Path, extra: str, message: str) -> None:
    text = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\n' + extra
    with pytest.raises(ValueError, match=message):
        plan_migration(_write(tmp_path, text), Agent(name="p", tools=[]))


def test_snippet_is_paste_ready_keyword_arguments() -> None:
    assert snippet({"model": "m", "examples": ["a"]}) == "Agent(..., model='m', examples=['a'])"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'apx_agent._migrate_config'`.

- [ ] **Step 3: Implement** `python/src/apx_agent/_migrate_config.py`

```python
"""apx-agent agents migrate-config: move a project off [tool.apx.agent] (sub-project 3a).

Deploy fields move to [tool.apx.deploy]; runtime fields must already be declared
on the Python agent before the table is removed. See
docs/design/agent-toml-source-of-truth.md, "Sub-project 3a"."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._models import AgentConfig

if TYPE_CHECKING:
    from ._agents import BaseAgent

_YAML_ONLY = ("data", "executor", "workflows", "example")
# Agent constructor arguments that aren't AgentSettings: compared to the agent's attribute.
_CONSTRUCTOR = {
    "instructions": "_instructions", "description": "_description", "temperature": "_temperature",
    "max_tokens": "_max_tokens", "max_iterations": "_max_iterations", "sub_agents": "_sub_agent_urls",
}
_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


@dataclass(frozen=True)
class Migration:
    text: str
    missing: dict[str, Any]
    removed_agent_table: bool


def cut_sections(text: str, prefix: str) -> str:
    """Remove every ``[prefix]`` / ``[prefix.*]`` section (header through the next header)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        match = _HEADER.match(line)
        if match:
            name = match.group(1)
            skipping = name == prefix or name.startswith(prefix + ".")
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _deploy_block(table: dict[str, Any]) -> str:
    from ._project_gen import _write_toml_section

    lines: list[str] = []
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    _write_toml_section(lines, "tool.apx.deploy", scalars)
    for key, value in table.items():
        if isinstance(value, dict):
            _write_toml_section(lines, f"tool.apx.deploy.{key}", value)
    return "\n".join(lines)


def _covered(field: str, value: Any, agent: BaseAgent, after: AgentConfig) -> bool:
    from ._settings import resolve_settings

    declared = AgentConfig.model_validate({"name": "x", field: value})
    if field in _CONSTRUCTOR:
        return getattr(agent, _CONSTRUCTOR[field], None) == getattr(declared, field)
    return getattr(resolve_settings(agent, after), field) == getattr(declared, field)


def plan_migration(pyproject: Path, agent: BaseAgent) -> Migration:
    """The new pyproject text and what Python still has to declare."""
    text = pyproject.read_text()
    data = tomllib.loads(text)
    apx = data.get("tool", {}).get("apx", {})
    raw = apx.get("agent")
    if raw is None:
        return Migration(text, {}, False)
    module = raw.get("module")
    if module is not None and module != "agent:agent":
        raise ValueError(f"module = {module!r} is not supported; rename it so the agent is agent:agent.")
    if "template" in raw:
        raise ValueError("template = {...} is not supported; generate an agent.py from the template first.")
    for field in _YAML_ONLY:
        if field in raw:
            raise ValueError(f"{field} belongs in the YAML spec, not [tool.apx.agent].")

    deploy = dict(apx.get("deploy", {}))
    for key in ("name", "host"):
        if key in raw:
            deploy[key] = raw[key]
    deploy.update(raw.get("deploy", {}))
    deploy_only = {"tool": {"apx": {"deploy": deploy}}, "project": data.get("project", {})}
    from ._inspection import _fold_deploy_table

    after = AgentConfig.model_validate(_fold_deploy_table(deploy_only, {}))
    runtime = {k: v for k, v in raw.items()
               if k in AgentConfig.model_fields and k not in ("name", "host", "deploy")}
    missing = {k: v for k, v in runtime.items() if not _covered(k, v, agent, after)}

    remaining = {k: v for k, v in raw.items() if k not in ("name", "host", "deploy", "module")}
    new = cut_sections(cut_sections(text, "tool.apx.deploy"), "tool.apx.agent")
    if missing or set(remaining) - set(runtime):
        from ._project_gen import _write_toml_section

        lines: list[str] = []
        _write_toml_section(lines, "tool.apx.agent", {k: v for k, v in remaining.items() if not isinstance(v, dict)})
        for key, value in remaining.items():
            if isinstance(value, dict):
                _write_toml_section(lines, f"tool.apx.agent.{key}", value)
        new = new.rstrip("\n") + "\n\n" + "\n".join(lines)
        removed = False
    else:
        removed = True
    new = new.rstrip("\n") + "\n\n" + _deploy_block(deploy)
    return Migration(new.rstrip("\n") + "\n", missing, removed)


def snippet(missing: dict[str, Any]) -> str:
    """Keyword arguments to paste into the agent's constructor."""
    return "Agent(..., " + ", ".join(f"{k}={v!r}" for k, v in missing.items()) + ")"
```

Notes for the implementer:
- The remaining `[tool.apx.agent]` keys are rewritten with `_write_toml_section`. That function renders scalars and lists with `_toml_value` and nested tables as sub-sections. If `_toml_value` can't render a value (for example a list of tables), let the `TypeError` surface. Don't special-case it.
- Fields that aren't `AgentConfig` fields (`module`, `profile`, and so on) are kept in the table, because `set(remaining) - set(runtime)` is non-empty. So the table stays, and the read-back in Task 4 confirms nothing was lost. `module = "agent:agent"` is dropped, because it's the default.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_migrate_config.py python/tests/test_migrate_config.py
git commit --quiet --message "feat: plan_migration moves deploy fields and lists what Python must declare

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: `apx-agent agents migrate-config`

**Files:**
- Modify: `python/src/apx_agent/cli.py` (new command after `migrate_agent_toml`, ~line 4980)
- Test: `python/tests/test_migrate_config.py`

**Interfaces:**
- Consumes: `plan_migration`, `snippet`, `Migration` (Task 3); `_load_agent_config`; `resolve_settings`.
- Produces: the CLI command `apx-agent agents migrate-config`. It takes no options and runs in the cwd.

- [ ] **Step 1: Write the failing tests** (append to `python/tests/test_migrate_config.py`)

```python
import subprocess
import sys

from click.testing import CliRunner

from apx_agent.cli import main

_AGENT_PY_BARE = "from apx_agent import Agent\nagent = Agent(name='inspector', tools=[])\n"
_AGENT_PY_FULL = (
    "from apx_agent import Agent\n"
    "agent = Agent(name='inspector', tools=[], model='system.ai.claude-sonnet-4-6',\n"
    "              instructions='Inspect tables.', examples=['Show tables', 'Count rows'])\n"
)


def _repo(tmp_path: Path, agent_py: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    (tmp_path / "agent.py").write_text(agent_py)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return tmp_path


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent", None)
    return CliRunner().invoke(main, ["agents", "migrate-config"])


def _commit_all(tmp_path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)


def test_two_runs_migrate_a_real_shaped_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ctk import Artifact, verify

    root = _repo(tmp_path, _AGENT_PY_BARE)
    first = _run(root, monkeypatch)
    assert first.exit_code == 0, first.output
    assert "Agent(..., model='system.ai.claude-sonnet-4-6'" in first.output
    verify(Artifact(str(root / "pyproject.toml"), min_bytes=50, must_contain=["[tool.apx.deploy]", "[tool.apx.agent]"]))

    (root / "agent.py").write_text(_AGENT_PY_FULL)
    _commit_all(root)
    second = _run(root, monkeypatch)
    assert second.exit_code == 0, second.output
    verify(Artifact(str(root / "pyproject.toml"), min_bytes=50, must_contain=["[tool.apx.deploy]", 'space = "shared"']))
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "agent" not in data["tool"]["apx"]
    assert data["tool"]["other"] == {"keep": True}

    _commit_all(root)
    third = _run(root, monkeypatch)
    assert third.exit_code == 0 and "nothing to migrate" in third.output


def test_refuses_a_dirty_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / "pyproject.toml").write_text(_PYPROJECT + "\n# local edit\n")
    result = _run(root, monkeypatch)
    assert result.exit_code != 0
    assert "pyproject.toml has uncommitted changes; commit them first (git is migrate-config's undo)." in result.output


def test_a_read_back_mismatch_restores_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _migrate_config

    root = _repo(tmp_path, _AGENT_PY_FULL)
    original = (root / "pyproject.toml").read_text()
    real = _migrate_config.plan_migration

    def lossy(pyproject: Path, agent):
        planned = real(pyproject, agent)
        return _migrate_config.Migration(planned.text.replace('space = "shared"', 'space = "other"'),
                                         planned.missing, planned.removed_agent_table)

    monkeypatch.setattr(_migrate_config, "plan_migration", lossy)
    result = _run(root, monkeypatch)
    assert result.exit_code != 0
    assert "migrate-config changed the effective config; restored pyproject.toml." in result.output
    assert (root / "pyproject.toml").read_text() == original
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider -k "runs or dirty or read_back"`
Expected: FAIL with `No such command 'migrate-config'`.

- [ ] **Step 3: Implement.** Add this to `cli.py` directly after `migrate_agent_toml`:

```python
@agents.command("migrate-config")
def migrate_config() -> None:
    """Move [tool.apx.agent] to [tool.apx.deploy] and the Python agent (safe to re-run)."""
    import importlib
    import subprocess

    from . import _migrate_config
    from ._inspection import _load_agent_config
    from ._settings import resolve_settings

    cwd = Path.cwd()
    pyproject = cwd / "pyproject.toml"
    status = subprocess.run(["git", "status", "--porcelain", "--", "pyproject.toml"],
                            cwd=cwd, capture_output=True, text=True, check=False)
    if status.returncode != 0 or status.stdout.strip():
        raise click.ClickException(
            "pyproject.toml has uncommitted changes; commit them first (git is migrate-config's undo).")
    if str(cwd) not in sys.path:
        sys.path.insert(0, str(cwd))
    sys.modules.pop("agent", None)
    agent = importlib.import_module("agent").agent  # raw: no finalize_agent, so only what Python declares
    try:
        before = _load_agent_config(pyproject_path=pyproject)
        migration = _migrate_config.plan_migration(pyproject, agent)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    original = pyproject.read_text()
    if migration.text == original:
        click.echo("nothing to migrate: no [tool.apx.agent] in pyproject.toml")
        return
    pyproject.write_text(migration.text)
    try:
        after = _load_agent_config(pyproject_path=pyproject)
        # Constructor fields were checked against the agent by plan_migration; once the
        # table is gone they fall back to defaults in the config, so they're excluded here.
        skip = set(_migrate_config._CONSTRUCTOR)
        same = (before is not None and after is not None
                and resolve_settings(agent, after).model_dump(exclude=skip)
                == resolve_settings(agent, before).model_dump(exclude=skip))
    except ValueError:
        same = False
    if not same:
        pyproject.write_text(original)
        raise click.ClickException("migrate-config changed the effective config; restored pyproject.toml.")
    click.echo("moved deploy settings to [tool.apx.deploy]")
    if migration.removed_agent_table:
        click.echo("removed [tool.apx.agent]: every field is declared in Python")
    else:
        click.echo("kept [tool.apx.agent]; add these to the root agent, then re-run:")
        click.echo("  " + _migrate_config.snippet(migration.missing))
```

Notes:
- `sys` and `Path` are already imported in `cli.py`. Confirm with `grep -n "^import sys\|^from pathlib import Path" python/src/apx_agent/cli.py`.
- Equality semantics: while `[tool.apx.agent]` is kept, the constructor fields stay in it and both sides carry them. When it's removed, `plan_migration` has already required each constructor field to equal the agent's attribute, so they're excluded from the config comparison.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider`
Expected: PASS (11 tests).

- [ ] **Step 5: Gate**

Run: `make check`, then `pre-commit run --files $(git diff --name-only origin/main...HEAD)`.
Expected: both green.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_migrate_config.py
git commit --quiet --message "feat: apx-agent agents migrate-config moves a project off [tool.apx.agent]

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 5: Docs and changelog

**Files:**
- Modify: `docs/reference/pyproject-toml.md`
- Modify: `CHANGELOG.md` (the `### Added` list under `## [Unreleased]`)

- [ ] **Step 1: Reference section.** Add this after the "Setting these in Python" subsection:

````markdown
### `[tool.apx.deploy]` — deploy settings

Deploy settings live in their own table, which deploy reads without importing
`agent.py`:

```toml
[tool.apx.deploy]
name = "orders-agent"     # optional; defaults to [project].name
host = "apps"             # apps | model_serving
space = "shared-space"    # an App Space picks compute; without one:
# instances = 2           #   fixed count, or [tool.apx.deploy.autoscale] min/max, 1-5
backend = "agentbricks"   # | apps_api (+ entrypoint, include, env)
```

A field set both here and in `[tool.apx.agent]` (or `[tool.apx.agent.deploy]`)
must have the same value.

To move a project off `[tool.apx.agent]`, commit your work and run
`apx-agent agents migrate-config`. It moves the deploy settings here, then
prints the keyword arguments to add to your root agent. Re-run it after
adding them, and it removes `[tool.apx.agent]`.
````

- [ ] **Step 2: Changelog entry** under `### Added`:

```markdown
- **`[tool.apx.deploy]` and `migrate-config`.** Deploy settings (`name`, `host`, `space`, instances/autoscale, `backend`) have their own table, dev-UI `examples=` is an agent setting, and `apx-agent agents migrate-config` moves a project off `[tool.apx.agent]`, keeping the table until Python declares every field.
```

- [ ] **Step 3: Verify and commit**

Run: `pre-commit run --files docs/reference/pyproject-toml.md CHANGELOG.md` and `cd python && uv run pytest tests/test_docs* -q -p no:cacheprovider`.

```bash
git add docs/reference/pyproject-toml.md CHANGELOG.md
git commit --quiet --message "docs: [tool.apx.deploy] and migrate-config

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- Switching every other reader and the scaffold/YAML generator (3b).
- Migrating the in-repo projects (3c).
- Rejecting `[tool.apx.agent]` (3d).

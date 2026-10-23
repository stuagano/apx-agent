# Sub-project 3b-2: agent-setting readers use the effective config — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every reader of agent settings (`name`, `model`, `description`, `data`, `target`) gets them from the merged Python + TOML view instead of `[tool.apx.agent]`, without importing `agent.py` where that's unsafe.

**Architecture:** Four readers, one per safety context:

| Helper | Where | Used by |
|---|---|---|
| `_effective_config(cwd)` | `cli.py` | commands on the current project; it imports, finalizes and resolves |
| `_with_agent_settings(flat, cwd)` | `cli.py` | the same commands; overlays only *declared* agent settings onto `_read_apx_agent_config`'s dict, so each call site changes one line |
| `static_agent_setting(agent_py, key)` | `_inspection.py` | heuristics; reads literal keyword arguments from `agent.py` with `ast` and never imports |
| `effective_config_isolated(project)` | `_doctor_durable.py` | doctor; imports under the existing `_isolated` harness |

Durable discovery takes its config from `compile_agent`. The dev-UI grounding action stops writing `pyproject.toml`.

**Tech Stack:** Python ≥3.10, `ast`, pydantic v2, click, pytest.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3b-2: agent-setting readers use the effective config".

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`; `pre-commit run --files <changed files>`; and the PR CI subset under xdist. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **Declared values only.** A reader sees a setting only if the TOML or the agent declared it, meaning it's in the resolved config's `model_fields_set`. Defaults never appear, because "no model declared" must keep meaning "skip" or "error", as it does today. `name` counts as declared only if the TOML sets it or the agent has a `_name`.
- **Heuristics never import `agent.py`.** That covers `_detect_target`, `_find_runnable_agents`, `_materialize_yaml_project`, and `status` with its prompt string.
- **Error text, verbatim:** `could not import agent.py: {type(exc).__name__}: {exc}`. `ImportError` keeps `_load_finalized_agent`'s existing message.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Use long flags, and run `git commit` alone in its command (a hook misreads other flags in the same command). Never `git stash`.

## Review Focus

1. **A Python-only project with an unnamed root agent.** Expected: `name` isn't overlaid (it would be the placeholder `"agent"`), and callers keep their own fallback (for example `app_name`). Pinned in Task 1.
2. **`agent.py` whose `agent` is built through a variable or a function call** (`agent = build()`). Expected: the static read returns `None`, and heuristics fall back to the TOML. No crash. Pinned in Task 1.
3. **`doctor` on a project whose `agent.py` raises at import.** Expected: one FAIL check names the import error, and doctor's other checks still run. Pinned in Task 4.
4. **`eval` with no model declared anywhere.** Expected: the same "no model" usage error as today, not a silent default model. Pinned in Task 2.
5. **A durable app compiled without a config** (`compile_agent(config=None)`). Expected: discovery is disabled cleanly, as it is today for a config-less project. Pinned in Task 5.

---

### Task 1: The four readers

**Files:**
- Modify: `python/src/apx_agent/_inspection.py` (add `static_agent_setting`)
- Modify: `python/src/apx_agent/cli.py` (add `_effective_config`, `_with_agent_settings` and `_AGENT_SETTING_KEYS` after `_read_apx_agent_config`)
- Modify: `python/src/apx_agent/_doctor_durable.py` (add `effective_config_isolated` after `_isolated`)
- Test: `python/tests/test_effective_config.py` (create)

**Interfaces:**
- Produces:
  - `static_agent_setting(agent_py: Path, key: str) -> Any`: the literal value, or `None`.
  - `_effective_config(cwd: Path) -> AgentConfig | None`: raises `click.ClickException` on import or settings errors.
  - `_with_agent_settings(flat: dict[str, Any], cwd: Path | None = None) -> dict[str, Any]`
  - `effective_config_isolated(project: Path) -> AgentConfig | None`: raises whatever importing `agent.py` raises.

- [ ] **Step 1: Write the failing tests** (`python/tests/test_effective_config.py`)

```python
"""Effective agent settings for readers (sub-project 3b-2)."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest

from apx_agent._inspection import static_agent_setting


def _project(tmp_path: Path, agent_py: str | None, toml: str = "") -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n' + toml)
    if agent_py is not None:
        (tmp_path / "agent.py").write_text(agent_py)
    return tmp_path


@pytest.fixture
def in_project(monkeypatch: pytest.MonkeyPatch):
    def enter(root: Path) -> Path:
        monkeypatch.chdir(root)
        monkeypatch.syspath_prepend(str(root))
        monkeypatch.setenv("APX_PYPROJECT", str(root / "pyproject.toml"))
        monkeypatch.delitem(sys.modules, "agent", raising=False)
        return root
    return enter


def test_static_read_returns_literal_keywords(tmp_path: Path) -> None:
    root = _project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='n', target='durable_agent_server', tools=[])\n")
    assert static_agent_setting(root / "agent.py", "target") == "durable_agent_server"
    assert static_agent_setting(root / "agent.py", "name") == "n"
    assert static_agent_setting(root / "agent.py", "model") is None


@pytest.mark.parametrize("source", [
    "from apx_agent import Agent\nagent = build()\n",
    "from apx_agent import Agent\nT = 'durable_agent_server'\nagent = Agent(name='n', target=T, tools=[])\n",
    "this is not python(\n",
])
def test_static_read_never_guesses(tmp_path: Path, source: str) -> None:
    assert static_agent_setting(_project(tmp_path, source) / "agent.py", "target") is None


def test_python_only_project_overlays_declared_settings(tmp_path: Path, in_project) -> None:
    from apx_agent.cli import _with_agent_settings

    root = in_project(_project(tmp_path, "from apx_agent import Agent\n"
                                         "agent = Agent(name='orders', model='m1', description='d', tools=[])\n"))
    flat = _with_agent_settings({"profile": "dev"}, root)
    assert flat == {"profile": "dev", "name": "orders", "model": "m1", "description": "d"}


def test_defaults_and_placeholder_names_are_not_overlaid(tmp_path: Path, in_project) -> None:
    from apx_agent.cli import _with_agent_settings

    root = in_project(_project(tmp_path, "from apx_agent import SequentialAgent, Agent\n"
                                         "agent = SequentialAgent([Agent(name='leaf', tools=[])])\n"))
    assert _with_agent_settings({}, root) == {}


def test_toml_only_project_is_unchanged(tmp_path: Path, in_project) -> None:
    from apx_agent.cli import _with_agent_settings

    root = in_project(_project(tmp_path, None, '[tool.apx.agent]\nname = "t"\nmodel = "tm"\n'))
    assert _with_agent_settings({"name": "t", "model": "tm"}, root) == {"name": "t", "model": "tm"}


def test_mixed_conflict_is_a_clean_error(tmp_path: Path, in_project) -> None:
    from apx_agent.cli import _effective_config

    root = in_project(_project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='t', model='py', tools=[])\n",
                               '[tool.apx.agent]\nname = "t"\nmodel = "toml"\n'))
    with pytest.raises(click.ClickException, match="Set it in one place"):
        _effective_config(root)


def test_broken_agent_py_is_a_clean_error(tmp_path: Path, in_project) -> None:
    from apx_agent.cli import _effective_config

    root = in_project(_project(tmp_path, "raise RuntimeError('boom')\n"))
    with pytest.raises(click.ClickException, match="could not import agent.py: RuntimeError: boom"):
        _effective_config(root)


def test_isolated_effective_config_imports_under_the_doctor_harness(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import effective_config_isolated

    root = _project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='iso', model='mi', tools=[])\n")
    before = set(sys.modules)
    config = effective_config_isolated(root)
    assert config is not None and (config.name, config.model) == ("iso", "mi")
    assert "agent" not in set(sys.modules) - before
```

Check how other CLI tests load a project agent before running (`grep -n "_load_finalized_agent" python/tests/test_cli.py | head`). If `_load_finalized_agent("agent:agent")` needs more than cwd on `sys.path` (for example `APX_PYPROJECT`), set it in the fixture as shown.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'static_agent_setting'`.

- [ ] **Step 3: Implement.**

`_inspection.py` (module level, after `REGISTRATION_KEYS`):

```python
def static_agent_setting(agent_py: Path, key: str) -> Any:
    """Literal ``key=`` passed to the constructor assigned to ``agent`` in *agent_py*, or None.

    Never imports: heuristics (target detection, scans across projects, the shell
    prompt) must not run another project's import-time code. Anything that isn't a
    literal keyword on ``agent = Something(...)`` is None (sub-project 3b-2)."""
    import ast

    try:
        tree = ast.parse(agent_py.read_text())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if not any(isinstance(t, ast.Name) and t.id == "agent" for t in targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            return None
        for keyword in call.keywords:
            if keyword.arg == key:
                try:
                    return ast.literal_eval(keyword.value)
                except ValueError:
                    return None
        return None
    return None
```

`cli.py`, directly after `_read_apx_agent_config`:

```python
_AGENT_SETTING_KEYS = ("name", "model", "description", "data")


def _effective_config(cwd: Path) -> AgentConfig | None:
    """The current project's merged config: TOML + the Python agent's settings (3b-2).

    Imports and finalizes agent.py, so use it only for the project the command runs in."""
    from ._inspection import _load_agent_config
    from ._settings import resolve_settings

    toml = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    if not (cwd / "agent.py").is_file():
        return toml
    try:
        agent = _load_finalized_agent("agent:agent")
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — any import-time failure is the user's agent.py
        raise click.ClickException(f"could not import agent.py: {type(exc).__name__}: {exc}") from exc
    return resolve_settings(agent, toml)


def _with_agent_settings(flat: dict[str, Any], cwd: Path | None = None) -> dict[str, Any]:
    """*flat* with the agent settings the project declares (TOML or Python) merged in.

    Declared means in the resolved config's ``model_fields_set``; defaults never
    appear, so "no model declared" still reads as missing. A placeholder name
    (an unnamed root agent) isn't a declaration."""
    config = _effective_config(cwd or Path.cwd())
    if config is None:
        return flat
    merged = dict(flat)
    for key in _AGENT_SETTING_KEYS:
        if key not in config.model_fields_set:
            continue
        value = getattr(config, key)
        if value is None or (key == "name" and value == "agent" and "name" not in flat):
            continue
        merged[key] = value.model_dump() if hasattr(value, "model_dump") else value
    return merged
```

`AgentConfig` may need importing at the top of `cli.py`. Check with `grep -n "^from ._models import\|AgentConfig" python/src/apx_agent/cli.py | head -3`. If it's only imported locally, annotate the return as the string `"AgentConfig | None"` and add `from ._models import AgentConfig` under `TYPE_CHECKING`.

`_doctor_durable.py`, directly after `_isolated`:

```python
def effective_config_isolated(project: Path) -> AgentConfig | None:
    """The project's merged config, importing agent.py under the doctor's isolation (3b-2)."""
    from ._inspection import _load_agent_config
    from ._settings import resolve_settings
    from ._wiring import resolve_agent

    project = project.resolve()
    toml = _load_agent_config(pyproject_path=project / "pyproject.toml")
    if not (project / "agent.py").is_file():
        return toml
    with _isolated(project):
        agent = resolve_agent("agent:agent", toml, ws=_FakeWorkspaceClient())
        return resolve_settings(agent, toml)
```

Import `AgentConfig` in `_doctor_durable.py` if it isn't already (`grep -n "AgentConfig" python/src/apx_agent/_doctor_durable.py | head -3`).

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_inspection.py python/src/apx_agent/cli.py python/src/apx_agent/_doctor_durable.py python/tests/test_effective_config.py
git commit --quiet --message "feat: effective, static and isolated readers for agent settings

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: CLI commands read declared agent settings

**Files:**
- Modify: `python/src/apx_agent/cli.py`. Each site is named here by function, with today's line in brackets:
  - `eval_cmd` [~6674]: `config = _read_apx_agent_config() if endpoint_url else _read_apx_agent_config(_resolve_agent_pyproject())`
  - `deploy` [~7297]: `config = _read_apx_agent_config()`
  - `_register_apps_manifest_step` [~10438]: `config = _read_apx_agent_config(cwd / "pyproject.toml")`
  - `_do_advertise` [~11994]: `cfg = _read_apx_agent_config()`
  - `supervisor_add` [~12203]: `cfg = _read_apx_agent_config()`
  - `register_agent_cmd` [~14323]: `config = _read_apx_agent_config()`
  - `lint_cmd` [~13372] and `test_cmd` [~13671]: `effective_model = model or _read_apx_agent_config().get("model")`
- Test: `python/tests/test_effective_config.py`

**Interfaces:**
- Consumes: `_with_agent_settings`, `_effective_config` (Task 1).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_lint_uses_the_python_declared_model(tmp_path: Path, in_project, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent import cli

    root = in_project(_project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='l', model='py-model', tools=[])\n"))
    seen: list[str] = []
    monkeypatch.setattr(cli, "_run_lint", lambda agent, model, fmt: seen.append(model), raising=False)
    CliRunner().invoke(cli.main, ["eval", "lint"])
    assert seen == ["py-model"]


def test_eval_without_a_model_anywhere_is_still_a_usage_error(tmp_path: Path, in_project) -> None:
    from click.testing import CliRunner

    from apx_agent import cli

    root = in_project(_project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='e', tools=[])\n"))
    (root / "evals").mkdir()
    result = CliRunner().invoke(cli.main, ["eval", "run"])
    assert result.exit_code != 0 and "model" in result.output.lower()


def test_no_cli_site_reads_agent_settings_from_the_raw_table() -> None:
    import re

    source = (Path(__file__).resolve().parents[1] / "src" / "apx_agent" / "cli.py").read_text()
    raw = re.findall(r'_read_apx_agent_config\([^)]*\)\.get\("(model|name|description|data)"\)', source)
    assert raw == [] or raw == ["name", "name"], raw  # only status / prompt (Task 3 makes them static)
```

Before running, find what `lint_cmd` calls after resolving the model (`sed -n "$(grep -n '^def lint_cmd' python/src/apx_agent/cli.py | cut -d: -f1),+25p" python/src/apx_agent/cli.py`). Patch *that* function to capture `effective_model`, instead of the placeholder `_run_lint`. Likewise check the `eval run` subcommand name and its no-model error text (`grep -n "no LLM model\|requires a model\|--model" python/src/apx_agent/cli.py | sed -n 1,10p`), and assert on the real message. The intent: lint sees `py-model`, and eval with no declared model still errors.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider -k "lint or eval or raw_table"`
Expected: the lint test FAILs because the model is `None`; `test_no_cli_site_reads…` FAILs, listing `model`/`name`/`description`.

- [ ] **Step 3: Implement.** At each site above, wrap the flat dict:
  - `eval_cmd`: `config = _with_agent_settings(_read_apx_agent_config() if endpoint_url else _read_apx_agent_config(_resolve_agent_pyproject()))`. If `endpoint_url` is set, eval targets a remote endpoint and must not import the local agent, so keep that branch unwrapped: `config = _read_apx_agent_config() if endpoint_url else _with_agent_settings(_read_apx_agent_config(_resolve_agent_pyproject()))`.
  - `deploy`: `config = _with_agent_settings(_read_apx_agent_config())`
  - `_register_apps_manifest_step`: `config = _with_agent_settings(_read_apx_agent_config(cwd / "pyproject.toml"), cwd)`
  - `_do_advertise`, `supervisor_add`: `cfg = _with_agent_settings(_read_apx_agent_config())`
  - `register_agent_cmd`: `config = _with_agent_settings(_read_apx_agent_config())`
  - `lint_cmd`, `test_cmd`: the agent is already loaded just above, so resolve without a second import:

```python
    from ._inspection import _load_agent_config
    from ._settings import resolve_settings

    declared = resolve_settings(agent, _load_agent_config())
    effective_model = model or (declared.model if "model" in declared.model_fields_set else None)
```

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_effective_config.py tests/test_cli.py tests/test_deploy_apps.py tests/test_apps_api_deploy.py -q -p no:cacheprovider`
Expected: PASS. If an existing CLI test now imports a stub project's `agent.py` it didn't before, and fails on import, it's a real behavior change. Record it as a ruling: either keep that site unwrapped, or fix the test fixture to provide an importable agent.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/cli.py python/tests/test_effective_config.py
git commit --quiet --message "feat: CLI commands read declared agent settings from Python and TOML

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Heuristics read `agent.py` statically

**Files:**
- Modify: `python/src/apx_agent/cli.py`:
  - `_detect_target` [~464];
  - `_materialize_yaml_project` [~2565];
  - `_find_runnable_agents._is_runnable` [~5925];
  - `_status_prompt_string` [~1204] and `status` [~1259].
- Test: `python/tests/test_effective_config.py`

**Interfaces:**
- Consumes: `static_agent_setting` (Task 1).
- Produces: `_declares_durable(project: Path) -> bool` in `cli.py`.

- [ ] **Step 1: Write the failing tests** (append)

```python
_DURABLE_PY = "from apx_agent import Agent\nagent = Agent(name='d', target='durable_agent_server', tools=[])\n"


def test_detect_target_sees_a_python_declared_durable_target(tmp_path: Path) -> None:
    from apx_agent.cli import _detect_target

    assert _detect_target(_project(tmp_path, _DURABLE_PY)).target == "apps"
    assert "durable" in _detect_target(tmp_path).reason


def test_detect_target_never_imports_agent_py(tmp_path: Path) -> None:
    from apx_agent.cli import _detect_target

    root = _project(tmp_path, "raise RuntimeError('import side effect')\nagent = None\n")
    assert _detect_target(root).target in ("apps", "model-serving")


def test_runnable_scan_finds_a_python_only_durable_project(tmp_path: Path) -> None:
    from apx_agent.cli import _find_runnable_agents

    child = tmp_path / "orders"
    child.mkdir()
    _project(child, _DURABLE_PY)
    assert any(path == child for _, path in _find_runnable_agents(tmp_path))


def test_status_name_comes_from_agent_py_statically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _status_prompt_string

    root = _project(tmp_path, _DURABLE_PY)
    monkeypatch.chdir(root)
    assert "d" in _status_prompt_string(root)
```

Check `_find_runnable_agents`' return shape (`sed -n "$(grep -n '^def _find_runnable_agents' python/src/apx_agent/cli.py | cut -d: -f1),+20p" python/src/apx_agent/cli.py`) and match it in the assertion. Also check whether `_status_prompt_string` needs `_is_apx_project` to be true for the name to show. If so, Task 4's `_is_apx_project` change is a prerequisite: move this one test to Task 4 and note it as a ruling.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider -k "detect_target or runnable or status"`
Expected: FAIL. The Python-only durable target isn't seen.

- [ ] **Step 3: Implement.** Add near `_detect_target`:

```python
def _declares_durable(project: Path) -> bool:
    """A durable target declared in agent.py (read statically) or in the TOML. Never imports."""
    from ._inspection import static_agent_setting

    durable = "durable_agent_server"
    return (static_agent_setting(project / "agent.py", "target") == durable
            or _read_apx_agent_config(project / "pyproject.toml").get("target") == durable)
```

- In `_detect_target`, replace `_read_apx_agent_config(cwd / "pyproject.toml").get("target") == "durable_agent_server"` with `_declares_durable(cwd)`.
- In `_materialize_yaml_project`, replace `_read_apx_agent_config(target / "pyproject.toml").get("target") == "durable_agent_server"` with `_declares_durable(target)`.
- In `_is_runnable`, replace the first clause with `(_declares_durable(d) and (d / "agent.py").is_file())`.
- In `_status_prompt_string` and `status`, replace `_read_apx_agent_config().get("name")` with:

```python
static_agent_setting(cwd / "agent.py", "name") or _read_apx_agent_config().get("name")
```

Import `static_agent_setting` locally from `._inspection`.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_effective_config.py tests/test_cli.py tests/test_cli_navigation_ctk.py -q -p no:cacheprovider`
Expected: PASS. Update `test_no_cli_site_reads_agent_settings_from_the_raw_table` to assert `raw == []`, since the two `name` reads are gone now.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/cli.py python/tests/test_effective_config.py
git commit --quiet --message "feat: target detection, scans and status read agent.py statically

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: Doctor reads the isolated effective config

**Files:**
- Modify: `python/src/apx_agent/_doctor.py`:
  - `check_model_endpoint` [~532];
  - `check_gateway_guardrails` [~606];
  - `_is_apx_project` [~1026].
- Test: `python/tests/test_effective_config.py`

**Interfaces:**
- Consumes: `effective_config_isolated` (Task 1).
- Produces: `_declared_config(cwd: Path) -> AgentConfig | Check | None` in `_doctor.py`. It returns the effective config, a FAIL `Check("Agent import", …)` on an import error, or `None` when there's nothing to read.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_doctor_reads_a_python_declared_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _doctor

    root = _project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='m', model='cat.sch.model', tools=[])\n")
    seen: list[str] = []
    monkeypatch.setattr(_doctor, "_ai_gateway_model_service_name", lambda model: seen.append(model) or None)
    check = _doctor.check_model_endpoint(root, auth_ok=True)
    assert seen == ["cat.sch.model"] and check is not None


def test_a_python_only_project_is_an_apx_project(tmp_path: Path) -> None:
    from apx_agent._doctor import _is_apx_project

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _is_apx_project(_project(tmp_path / "a", "from apx_agent import Agent\nagent = Agent(name='x', tools=[])\n"))
    assert not _is_apx_project(_project(tmp_path / "b", "print('hi')\n"))


def test_doctor_reports_a_broken_agent_py_and_keeps_going(tmp_path: Path) -> None:
    from apx_agent._doctor import Status, check_model_endpoint

    root = _project(tmp_path, "raise RuntimeError('boom')\n")
    check = check_model_endpoint(root, auth_ok=True)
    assert check is not None and check.status is Status.FAIL
    assert "could not import agent.py: RuntimeError: boom" in check.detail
```

The intent: an `agent.py` that uses `apx_agent` counts, and an unrelated `agent.py` doesn't. To check that the other checks keep going, look at how `run_checks` collects results (`grep -n "def run_checks" -A30 python/src/apx_agent/_doctor.py`). If a check returning FAIL doesn't stop `run_checks`, the unit test above is enough. Otherwise add an assertion over `run_checks` output.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider -k doctor`
Expected: FAIL. No model is seen from `agent.py`, and a Python-only project isn't an apx project.

- [ ] **Step 3: Implement.** In `_doctor.py`:

```python
def _declared_config(cwd: Path) -> "AgentConfig | Check | None":
    """Effective config for doctor's checks, imported under isolation; a FAIL check on import error."""
    from ._doctor_durable import effective_config_isolated

    try:
        return effective_config_isolated(cwd)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises, on a broken agent.py
        return Check("Agent import", Status.FAIL, f"could not import agent.py: {type(exc).__name__}: {exc}",
                     "Fix the error in agent.py, then re-run apx-agent doctor.")


def _is_apx_project(cwd: Path) -> bool:
    """True when cwd looks like an apx project: an apx TOML table, or an agent.py using apx_agent."""
    pyproject, agent_py = cwd / "pyproject.toml", cwd / "agent.py"
    try:
        text = pyproject.read_text() if pyproject.exists() else ""
        if "[tool.apx.agent]" in text or "[tool.apx.deploy]" in text:
            return True
        return agent_py.is_file() and "apx_agent" in agent_py.read_text()
    except OSError:
        return False
```

In `check_model_endpoint` and `check_gateway_guardrails`, replace the tomllib block that reads `data["tool"]["apx"]["agent"]` with:

```python
    config = _declared_config(cwd)
    if isinstance(config, Check):
        return config
    if config is None or "model" not in config.model_fields_set:
        return None
    model = config.model
```

In `check_gateway_guardrails`, replace the attachments loop's source `agent.get("service_policies", {}).get("attachments", [])` with:

```python
    attachments = (config.service_policies.model_dump(mode="json")["attachments"]
                   if config.service_policies is not None else [])
    for attachment in attachments:
```

Keep the loop body unchanged; the attachments are dicts again after `model_dump`. Keep the `_detect_target` gate in `check_gateway_guardrails` as it is. Both checks may now run under the isolation harness one after another. That's fine, because `_isolated` restores everything it patches.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_effective_config.py tests/test_doctor.py tests/test_doctor_durable.py -q -p no:cacheprovider`
Expected: PASS. If an existing doctor test builds a project whose `agent.py` isn't importable and expects the model check to read the TOML, decide case by case and ledger the ruling. The spec's intent is that a broken `agent.py` is reported, not ignored.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_doctor.py python/tests/test_effective_config.py
git commit --quiet --message "feat: doctor reads agent settings via an isolated import

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 5: Durable discovery uses the compiled config

**Files:**
- Modify: `python/src/apx_agent/_durable_agent.py`:
  - `compile_to_durable_agent_server` [~302]: add a `config` parameter;
  - delete `_durable_discovery_config` [~93];
  - the discovery line [~362].
- Modify: `python/src/apx_agent/_runtime_targets.py` [~227]: pass `config=config`.
- Test: `python/tests/test_effective_config.py`

**Interfaces:**
- Produces: `compile_to_durable_agent_server(..., config: AgentConfig | None = None)`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_durable_discovery_uses_the_compiled_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import Agent
    from apx_agent import _durable_agent
    from apx_agent._models import AgentConfig

    seen: list[object] = []
    monkeypatch.setattr(_durable_agent, "mount_durable_protocol", lambda app, agent, config: seen.append(config))
    config = AgentConfig(name="disc", model="m", target="durable_agent_server")
    monkeypatch.chdir(tmp_path)  # no pyproject.toml here: discovery must not read the working directory
    try:
        _durable_agent.compile_to_durable_agent_server(Agent(name="disc", tools=[]), model="m", service_ws=None,
                                                       config=config)
    except Exception:  # noqa: BLE001 — later startup wiring may need a workspace; discovery runs first
        pass
    assert seen and seen[0] is config
```

Check where `mount_durable_protocol` is imported in `_durable_agent.py` (module-level, or local inside the function). If it's local, patch it at its source module (`grep -n "mount_durable_protocol" python/src/apx_agent/_durable_agent.py`). If `compile_to_durable_agent_server` fails before reaching discovery without a workspace, follow `tests/test_durable_*` for how they build one (`grep -rln "compile_to_durable_agent_server" python/tests | head -3`), and reuse that fixture.

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_effective_config.py -q -p no:cacheprovider -k durable_discovery`
Expected: FAIL with `TypeError: unexpected keyword argument 'config'`.

- [ ] **Step 3: Implement.**
  - Add `config: AgentConfig | None = None,` to `compile_to_durable_agent_server`'s keyword parameters.
  - Replace `discovery = _durable_discovery_config()`, and the four comment lines above it about rediscovering, with `discovery = config  # compile_agent's resolved config; never re-read from cwd`.
  - Delete `_durable_discovery_config`.
  - In `_runtime_targets.compile_agent`'s durable return, add `config=config` to the `compile_to_durable_agent_server(...)` call.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_effective_config.py tests/test_durable_agent.py tests/test_runtime_targets.py tests/test_doctor_durable.py -q -p no:cacheprovider`
Expected: PASS. If `tests/test_durable_agent.py` doesn't exist, find the durable suites with `ls python/tests | grep -i durable`.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_durable_agent.py python/src/apx_agent/_runtime_targets.py python/tests/test_effective_config.py
git commit --quiet --message "refactor: durable discovery uses compile_agent's config instead of re-reading cwd

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 6: Grounding writes `agent.py` only

**Files:**
- Modify: `python/src/apx_agent/_ui_grounding.py`: delete `ensure_knowledge_in_pyproject` [~138]; the caller is at [~266].
- Modify: `python/tests/test_ui_grounding.py`: remove the import and the test for the deleted function [~18, ~215].
- Test: `python/tests/test_ui_grounding.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_ui_grounding.py`; reuse that file's helpers for building a grounding request)

```python
def test_grounding_never_writes_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-project 3b-2: knowledge= is wired into agent.py only."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\n')
    before = pyproject.read_bytes()
    # Call the grounding entry point exactly as the existing happy-path test in this file does,
    # with tmp_path as the project root.
    ...
    assert pyproject.read_bytes() == before
```

Replace the `...` with the exact call and setup from this file's existing happy-path grounding test (`grep -n "def test_" python/tests/test_ui_grounding.py`). Copy its fixture usage verbatim, changing only the project root. Don't leave `...` in the committed test.

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_ui_grounding.py -q -p no:cacheprovider -k never_writes_pyproject`
Expected: FAIL, because `pyproject.toml` gained `knowledge = "./.apx/okf"`.

- [ ] **Step 3: Implement.**
  - Delete `ensure_knowledge_in_pyproject`.
  - At the caller, remove `knowledge_wired = ensure_knowledge_in_pyproject(root)`, and set the result's `"knowledge_wired": agent_wired`. The response field stays, for the dev UI (`_apx_models.py:839`, `_dev.py:3970`).
  - In `tests/test_ui_grounding.py`, remove the deleted function's import and its dedicated test.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_ui_grounding.py tests/test_dev*.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_ui_grounding.py python/tests/test_ui_grounding.py
git commit --quiet --message "fix: dev-UI grounding wires knowledge= into agent.py only

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 7: No behavior change, gate, changelog

**Files:**
- Test: `python/tests/test_effective_config.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write the regression test** (append)

```python
def test_every_in_repo_example_detects_the_same_target_without_importing() -> None:
    from apx_agent.cli import _detect_target

    root = Path(__file__).resolve().parents[1]
    before = set(sys.modules)
    for path in sorted(p.parent for p in root.glob("examples/**/pyproject.toml") if ".venv" not in p.parts):
        _detect_target(path)
    assert "agent" not in set(sys.modules) - before
```

Expected: it passes immediately. It pins that detection imports nothing, and every example still resolves without an error.

- [ ] **Step 2: Gate**

Run: `make check`; `pre-commit run --files $(git diff --name-only origin/main...HEAD)`; the PR CI subset under xdist (Global Constraints).
Expected: all green.

- [ ] **Step 3: Changelog** under `### Added`:

```markdown
- **Agent settings from Python everywhere.** `eval`, `lint`, deploy registration, `agents register`, `advertise` and `supervisor add` read `model`, `name`, `description` and `data` from the Python agent as well as the TOML. Doctor imports agent.py under isolation. Target detection, project scans and `status` read agent.py statically, without importing it. The dev-UI grounding action no longer edits pyproject.toml.
```

- [ ] **Step 4: Commit** (its own command)

```bash
git add python/tests/test_effective_config.py CHANGELOG.md
git commit --quiet --message "test: target detection imports nothing across the in-repo examples

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- The legacy `template =` readers (3d).
- Scaffold and YAML generation (3b-3).
- Migrating the in-repo projects (3c).

"""Effective agent settings for readers (sub-project 3b-2)."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest

from apx_agent._inspection import static_agent_setting


def _project(tmp_path: Path, agent_py: str | None, toml: str | None = None) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n' + (toml if toml is not None else ""))
    if agent_py is not None:
        (tmp_path / "agent.py").write_text(agent_py)
    return tmp_path


@pytest.fixture
def in_project(monkeypatch: pytest.MonkeyPatch):
    saved = sys.modules.pop("agent", None)

    def enter(root: Path) -> Path:
        monkeypatch.chdir(root)
        monkeypatch.syspath_prepend(str(root))
        monkeypatch.setenv("APX_PYPROJECT", str(root / "pyproject.toml"))
        sys.modules.pop("agent", None)
        return root

    yield enter
    # The project's agent.py was imported as `agent`; never leak it into later tests.
    sys.modules.pop("agent", None)
    if saved is not None:
        sys.modules["agent"] = saved


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


def test_lint_uses_the_python_declared_model(tmp_path: Path, in_project, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent import cli

    in_project(_project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='l', model='py-model', tools=[])\n"))
    seen: list[str | None] = []

    def fake_lint(agent, model=None):
        seen.append(model)
        return []

    monkeypatch.setattr("apx_agent._lint.lint_agent", fake_lint)
    CliRunner().invoke(cli.main, ["eval", "lint"])
    assert seen == ["py-model"]


def test_eval_without_a_model_anywhere_is_still_a_usage_error(tmp_path: Path, in_project) -> None:
    from click.testing import CliRunner

    from apx_agent import cli

    root = in_project(_project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='e', tools=[])\n"))
    (root / "evals").mkdir()
    (root / "evals" / "basic.yaml").write_text("cases: []\n")
    result = CliRunner().invoke(cli.main, ["eval", "run", "evals/basic.yaml"])
    assert result.exit_code != 0 and "--model is required" in result.output


def test_no_cli_site_reads_agent_settings_from_the_raw_table() -> None:
    import re

    source = (Path(__file__).resolve().parents[1] / "src" / "apx_agent" / "cli.py").read_text()
    raw = re.findall(r'_read_apx_agent_config\([^)]*\)\.get\("(model|name|description|data)"\)', source)
    assert raw == [], raw


_DURABLE_PY = "from apx_agent import Agent\nagent = Agent(name='durable-d', target='durable_agent_server', tools=[])\n"


def test_detect_target_sees_a_python_declared_durable_target(tmp_path: Path) -> None:
    from apx_agent.cli import _detect_target

    detected = _detect_target(_project(tmp_path, _DURABLE_PY))
    assert detected.target == "apps" and "durable" in detected.reason


def test_detect_target_never_imports_agent_py(tmp_path: Path) -> None:
    from apx_agent.cli import _detect_target

    root = _project(tmp_path, "raise RuntimeError('import side effect')\nagent = None\n")
    assert _detect_target(root).target in ("apps", "model-serving")


def test_runnable_scan_finds_a_python_only_durable_project(tmp_path: Path) -> None:
    from apx_agent.cli import _find_runnable_agents

    child = tmp_path / "orders"
    child.mkdir()
    _project(child, _DURABLE_PY)
    assert ("orders", child) in _find_runnable_agents(tmp_path)


def test_status_name_comes_from_agent_py_statically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _status_prompt_string

    root = _project(tmp_path, _DURABLE_PY, "[tool.apx.agent]\n")
    monkeypatch.chdir(root)
    assert "durable-d" in _status_prompt_string(root)


def test_doctor_reads_a_python_declared_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _doctor

    root = _project(tmp_path, "from apx_agent import Agent\nagent = Agent(name='m', model='cat.sch.model', tools=[])\n")
    seen: list[str] = []

    def fake_service_name(model: str) -> None:
        seen.append(model)
        return None

    monkeypatch.setattr(_doctor, "_ai_gateway_model_service_name", fake_service_name)
    check = _doctor.check_model_endpoint(root, auth_ok=True)
    assert seen == ["cat.sch.model"] and check is not None


def test_a_python_only_project_is_an_apx_project(tmp_path: Path) -> None:
    from apx_agent._doctor import _is_apx_project

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _is_apx_project(_project(tmp_path / "a", "from apx_agent import Agent\nagent = Agent(name='x', tools=[])\n"))
    assert not _is_apx_project(_project(tmp_path / "b", "print('hi')\n"))


def test_doctor_reports_a_broken_agent_py(tmp_path: Path) -> None:
    from apx_agent._doctor import Status, check_model_endpoint

    root = _project(tmp_path, "raise RuntimeError('boom')\n")
    check = check_model_endpoint(root, auth_ok=True)
    assert check is not None and check.status is Status.FAIL
    assert "could not import agent.py: RuntimeError: boom" in check.detail


def test_durable_discovery_uses_the_compiled_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import Agent
    from apx_agent import _durable_agent
    from apx_agent._models import AgentConfig

    seen: list[AgentConfig | None] = []
    monkeypatch.setattr("apx_agent._wiring.mount_durable_protocol",
                        lambda app, agent, config: seen.append(config))
    config = AgentConfig(name="disc", model="m", target="durable_agent_server")
    monkeypatch.chdir(tmp_path)  # no pyproject.toml here: discovery must not read the working directory
    from unittest.mock import MagicMock

    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    _durable_agent.compile_to_durable_agent_server(Agent(name="disc", tools=[]), model="m", service_ws=MagicMock(),
                                                   config=config)
    assert seen and seen[0] == config.model_copy(update={"target": "durable_agent_server"})

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

"""[tool.apx.agent] is an error naming migrate-config (sub-project 3d)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from apx_agent import cli
from apx_agent._inspection import _load_agent_config
from apx_agent._tool_config import ToolConfigError

_LEGACY = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\nmodel = "m"\n'


def _commit(root: Path) -> None:
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=root, check=True)


def test_the_table_is_an_error_naming_migrate_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    with pytest.raises(ToolConfigError, match=r"\[tool\.apx\.agent\] is no longer supported.*apx-agent agents migrate-config"):
        _load_agent_config(pyproject_path=tmp_path / "pyproject.toml")


def test_a_legacy_apx_local_table_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    with pytest.raises(click.ClickException, match="migrate-config"):
        cli._read_apx_agent_config(tmp_path / "pyproject.toml")


def test_finalize_rejects_the_table_at_boot(tmp_path: Path) -> None:
    from apx_agent import Agent
    from apx_agent._wiring import finalize_agent

    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    with pytest.raises(ToolConfigError, match="migrate-config"):
        finalize_agent(Agent(name="p", tools=[]), pyproject_path=str(tmp_path / "pyproject.toml"))


def test_migrate_config_still_migrates_a_legacy_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    (tmp_path / "agent.py").write_text('from apx_agent import Agent\nagent = Agent(name="p", tools=[], model="m")\n')
    _commit(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["agents", "migrate-config"])
    assert result.exit_code == 0, result.output
    assert "removed [tool.apx.agent]" in result.output
    config = _load_agent_config(pyproject_path=tmp_path / "pyproject.toml")
    assert config is not None and config.name == "p"


def test_store_defaults_are_read_from_tool_apx(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n\n[tool.apx]\nmemory_store = "stores:mem"\n'
        'example_store = "stores:ex"\nsummarize_fn = "stores:summarize"\n')
    flat = cli._read_apx_agent_config(tmp_path / "pyproject.toml")
    assert (flat["memory_store"], flat["example_store"], flat["summarize_fn"]) == ("stores:mem", "stores:ex", "stores:summarize")


def test_migrate_config_moves_store_defaults_to_tool_apx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tomllib

    (tmp_path / "pyproject.toml").write_text(_LEGACY + 'memory_store = "stores:mem"\n')
    (tmp_path / "agent.py").write_text('from apx_agent import Agent\nagent = Agent(name="p", tools=[], model="m")\n')
    _commit(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["agents", "migrate-config"])
    assert result.exit_code == 0, result.output
    apx = tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["apx"]
    assert "agent" not in apx and apx["memory_store"] == "stores:mem"


@pytest.mark.parametrize("args", [["agents", "describe"], ["eval", "lint"]])
def test_cli_commands_report_the_table_error_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    (tmp_path / "agent.py").write_text('from apx_agent import Agent\nagent = Agent(name="p", tools=[])\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 1 and "migrate-config" in result.output, (result.exit_code, result.output)
    assert not isinstance(result.exception, ToolConfigError)  # a clean Error: line, not a traceback

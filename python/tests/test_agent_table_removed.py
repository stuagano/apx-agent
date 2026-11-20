"""[tool.apx.agent] is an error naming migrate-config (sub-project 3d)."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from apx_agent import cli
from apx_agent._inspection import _load_agent_config
from apx_agent._tool_config import ToolConfigError

_LEGACY = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\nmodel = "m"\n'


def test_the_table_is_an_error_naming_the_last_release(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    with pytest.raises(ToolConfigError, match=r"\[tool\.apx\.agent\] is no longer supported.*migrate-config.*removed after 0\.7\.7"):
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


def test_store_defaults_are_read_from_tool_apx(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n\n[tool.apx]\nmemory_store = "stores:mem"\n'
        'example_store = "stores:ex"\nsummarize_fn = "stores:summarize"\n')
    flat = cli._read_apx_agent_config(tmp_path / "pyproject.toml")
    assert (flat["memory_store"], flat["example_store"], flat["summarize_fn"]) == ("stores:mem", "stores:ex", "stores:summarize")


@pytest.mark.parametrize("args", [["agents", "describe"], ["eval", "lint"]])
def test_cli_commands_report_the_table_error_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    (tmp_path / "agent.py").write_text('from apx_agent import Agent\nagent = Agent(name="p", tools=[])\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 1 and "migrate-config" in result.output, (result.exit_code, result.output)
    assert not isinstance(result.exception, ToolConfigError)  # a clean Error: line, not a traceback


def test_resolve_agent_needs_an_agent_not_a_template() -> None:
    from apx_agent._models import AgentConfig
    from apx_agent._wiring import TemplateConfigError, resolve_agent

    config = AgentConfig.model_validate({"name": "p", "template": {"name": "data", "catalog": "c", "schema": "s"}})
    with pytest.raises(TemplateConfigError, match="agent.py"):
        resolve_agent(None, config)


def test_a_gallery_yaml_with_a_template_still_generates(tmp_path: Path) -> None:
    from apx_agent._project_gen import generate_project
    from apx_agent._yaml_spec import load_spec

    spec = Path(cli.__file__).parent / "coworker_gallery" / "payroll.yaml"
    generate_project(load_spec(spec, strict=False), tmp_path)
    assert "CoworkerAgent(" in (tmp_path / "agent.py").read_text()


# --- final-review fix pass -------------------------------------------------


def test_status_probes_sub_agents_declared_on_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nname = "p"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import LlmAgent\nagent = LlmAgent(name='p', tools=[], sub_agents=['https://peer.example.com'])\n")
    seen: list[list[str]] = []
    monkeypatch.setattr(cli._doctor_mod, "probe_sub_agents", lambda urls: seen.append(list(urls)) or [])
    cli._probe_local_sub_agents(tmp_path)
    assert seen == [["https://peer.example.com"]]


def test_an_env_ref_sub_agent_is_not_added_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import FastAPI

    from apx_agent import Agent
    from apx_agent._settings import resolve_settings
    from apx_agent._wiring import setup_agent

    monkeypatch.setenv("PEER_URL", "https://peer.example.com")
    agent = Agent(name="p", tools=[], sub_agents=["${PEER_URL}"])
    fetched: list[str] = []
    monkeypatch.setattr("apx_agent._wiring._fetch_sub_agent_card", lambda url, **_: fetched.append(url), raising=False)
    try:
        asyncio.run(setup_agent(FastAPI(), agent, resolve_settings(agent, None)))
    except Exception:  # noqa: BLE001 — card fetch / tool build may fail offline; only the URL list matters
        pass
    assert len(agent._sub_agent_urls) == 1, agent._sub_agent_urls


def test_store_help_points_at_tool_apx() -> None:
    import re

    text = (Path(cli.__file__)).read_text()
    stale = re.findall(r"(?:memory_store|example_store|summarize_fn|_store)\b[^\n]{0,40}\[tool\.apx\.agent\]", text)
    assert stale == [], stale

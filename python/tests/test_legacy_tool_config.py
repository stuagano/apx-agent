"""Removed TOML authoring fails loudly instead of dropping governed tools."""

from pathlib import Path

import pytest

from apx_agent import Agent, finalize_agent
from apx_agent._tool_config import ToolConfigError


def test_finalization_rejects_legacy_tools_before_factory_io(tmp_path: Path, monkeypatch) -> None:
    import apx_agent._tool_config as config

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.apx.agent]\nname = "test"\n'
        '[[tool.apx.tools]]\ntype = "mcp_toolkit"\n'
        'server_url = "https://forbidden.example/mcp"\n'
    )

    def unexpected_factory():
        pytest.fail("legacy declarations must be rejected before tool construction")

    monkeypatch.setattr(config, "_registry", unexpected_factory)
    with pytest.raises(ToolConfigError, match=r"tools/.*agent.py"):
        finalize_agent(Agent(tools=[]), pyproject_path=str(project))


@pytest.mark.parametrize("declaration", ["tools = []", "tools = {}"])
def test_empty_or_malformed_legacy_section_is_not_silently_accepted(
    tmp_path: Path, declaration: str,
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(f"[tool.apx]\n{declaration}\n")
    with pytest.raises(ToolConfigError, match="no longer supported"):
        finalize_agent(Agent(tools=[]), pyproject_path=str(project))


def test_config_loading_rejects_legacy_tools_before_agent_import(tmp_path: Path) -> None:
    from apx_agent._inspection import _load_agent_config

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.apx.agent]\nname = "test"\nmodule = "agent:agent"\n'
        '[[tool.apx.tools]]\ntype = "genie"\nspace_id = "room"\n'
    )
    with pytest.raises(ToolConfigError, match="no longer supported"):
        _load_agent_config(pyproject_path=project)


def test_no_legacy_tools_keeps_python_tool_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[tool.apx.deploy]\nname = "test"\n')

    def echo(text: str) -> str:
        """Return the input."""
        return text

    agent = Agent(tools=[echo])
    finalize_agent(agent, pyproject_path=str(project))
    finalize_agent(agent, pyproject_path=str(project))
    assert agent._tool_fns == [echo]
    assert agent._tool_fns[0]("real result") == "real result"


def test_legacy_tools_rejected_from_project_subdirectory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.apx.tools]]\ntype = "genie"\nspace_id = "room"\n'
    )
    subdir = tmp_path / "src"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    with pytest.raises(ToolConfigError, match="no longer supported"):
        finalize_agent(Agent(tools=[]))

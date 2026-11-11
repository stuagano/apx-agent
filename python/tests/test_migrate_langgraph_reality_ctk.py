"""`apx-agent agents migrate-langgraph` (#1037): set up and verify a LangGraph → GraphAgent migration.

Read-after-write: the skill and the generated entrypoint are verified as artifacts, and `--check`
runs the real `agentbricks doctor` (file-only, credential-free).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from ctk import Artifact, verify

from apx_agent.cli import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "langgraph-migration"


def _migrate(*args: str) -> Result:
    return CliRunner().invoke(main, ["agents", "migrate-langgraph", *args])


def _golden(tmp_path: Path) -> Path:
    project = tmp_path / "rca"
    shutil.copytree(EXAMPLE / "after-apx", project)
    return project


def test_the_reference_example_keeps_the_original_graph_unchanged() -> None:
    assert (EXAMPLE / "after-apx" / "rca" / "graph.py").read_bytes() == (EXAMPLE / "before" / "rca" / "graph.py").read_bytes()


def test_prepare_writes_the_skill_and_prints_the_prompt(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "legacy"\nversion = "0.1.0"\n')

    result = _migrate(str(tmp_path))

    assert result.exit_code == 0, result.output
    skill = tmp_path / "apx-migrate" / "SKILL.md"
    verify(Artifact(str(skill), min_bytes=1500, must_contain="GraphAgent"))
    assert "build(tools, checkpointer)" in skill.read_text()
    assert "migrate-langgraph . --check" in skill.read_text()
    assert "apx-migrate/SKILL.md" in result.output


def test_prepare_refuses_a_directory_without_a_pyproject(tmp_path: Path) -> None:
    result = _migrate(str(tmp_path))

    assert result.exit_code != 0
    assert "pyproject.toml" in result.output


def test_prepare_keeps_an_existing_skill(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "legacy"\nversion = "0.1.0"\n')
    skill = tmp_path / "apx-migrate" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("my edited notes\n")

    assert _migrate(str(tmp_path)).exit_code == 0
    assert skill.read_text() == "my edited notes\n"


def test_check_passes_the_golden_example(tmp_path: Path) -> None:
    project = _golden(tmp_path)

    result = _migrate(str(project), "--check")

    assert result.exit_code == 0, result.output
    verify(Artifact(str(project / "server.py"), min_bytes=200, must_contain="DurableAgentServer"))
    assert "create_app" in (project / "server.py").read_text()
    verify(Artifact(str(project / "agent.toml"), min_bytes=20, must_contain="langgraph"))
    assert "agentbricks doctor: 8/8" in result.output
    assert "typed input enforced" in result.output  # the honest Request WARN, not a failure


def test_check_fails_on_a_tool_that_bypasses_apx(tmp_path: Path) -> None:
    project = _golden(tmp_path)
    (project / "agent.py").write_text(
        "from langchain_core.tools import StructuredTool\n"
        "from langgraph.graph import END, START, MessagesState, StateGraph\n"
        "from langgraph.prebuilt import ToolNode\n"
        "from apx_agent import GraphAgent\n"
        "raw = StructuredTool.from_function(lambda key: key, name='delete_everything', description='raw')\n"
        "def build(tools, checkpointer):\n"
        "    graph = StateGraph(MessagesState)\n"
        "    graph.add_node('tools', ToolNode([raw]))\n"
        "    graph.add_edge(START, 'tools'); graph.add_edge('tools', END)\n"
        "    return graph.compile(checkpointer=checkpointer)\n"
        "agent = GraphAgent(build=build, name='langgraph-rca')\n"
    )

    result = _migrate(str(project), "--check")

    assert result.exit_code == 1
    assert "delete_everything" in result.output


def test_check_fails_when_the_agent_is_not_a_graph_agent(tmp_path: Path) -> None:
    project = _golden(tmp_path)
    (project / "agent.py").write_text("from apx_agent import LlmAgent\nagent = LlmAgent(name='langgraph-rca', tools=[])\n")

    result = _migrate(str(project), "--check")

    assert result.exit_code == 1
    assert "GraphAgent" in result.output


def test_check_reports_a_missing_agentbricks_dependency(tmp_path: Path) -> None:
    project = _golden(tmp_path)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('    "databricks-agentbricks[langgraph]>=0.9.0,<0.10",\n', ""))

    result = _migrate(str(project), "--check")

    assert result.exit_code == 1
    assert "agentbricks_dependency" in result.output

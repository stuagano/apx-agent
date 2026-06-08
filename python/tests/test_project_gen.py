"""Tests for ``_project_gen.generate_project`` — AgentConfig → deployable Apps project.

Covers:
  1. pyproject.toml is created with the correct project name.
  2. [tool.apx.agent.template] section contains join_key when template is set.
  3. [tool.apx.agent.memory] section is present when config.memory is set.
  4. agent_server/start_server.py is created.
  5. databricks.yml contains the agent name.
  6. ``module = "agent:agent"`` does NOT appear when template is set.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from apx_agent._models import AgentConfig, MemoryBackendConfig, SessionBackendConfig
from apx_agent._project_gen import generate_project


@pytest.fixture()
def coworker_config() -> AgentConfig:
    """An AgentConfig that exercises the template + memory + session path."""
    return AgentConfig(
        name="payroll-coworker",
        description="Reconciles hours worked against paychecks issued.",
        model="databricks-claude-sonnet-4-6",
        instructions="You are a payroll analyst.",
        examples=["Who has a mismatch?"],
        template={
            "name": "coworker",
            "catalog": "main",
            "schema": "payroll",
            "persona": "a payroll analyst",
            "join_key": "employee ID",
            "objective": "Surface mismatches.",
            "memory": "persistent",
        },
        memory=MemoryBackendConfig(
            type="delta",
            table_name="main.payroll.apx_memory",
            auto_create=True,
        ),
        session=SessionBackendConfig(
            type="delta",
            table_name="main.payroll.apx_sessions",
            auto_create=True,
        ),
    )


@pytest.fixture()
def minimal_config() -> AgentConfig:
    """A minimal AgentConfig with no template, memory, or session."""
    return AgentConfig(name="my-agent")


# ---------------------------------------------------------------------------
# Test 1: pyproject.toml exists with the correct project name
# ---------------------------------------------------------------------------


def test_pyproject_name(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """pyproject.toml is created with name matching config.name."""
    generate_project(coworker_config, tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml was not created"

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    assert data["project"]["name"] == "payroll-coworker"


# ---------------------------------------------------------------------------
# Test 2: [tool.apx.agent.template] contains join_key when template is set
# ---------------------------------------------------------------------------


def test_template_section_contains_join_key(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """[tool.apx.agent.template] section must contain the join_key field."""
    generate_project(coworker_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    template_section = data.get("tool", {}).get("apx", {}).get("agent", {}).get("template", {})
    assert template_section.get("join_key") == "employee ID", (
        f"join_key not found in [tool.apx.agent.template]: {template_section}"
    )


# ---------------------------------------------------------------------------
# Test 3: [tool.apx.agent.memory] section present when config.memory is set
# ---------------------------------------------------------------------------


def test_memory_section_present(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """[tool.apx.agent.memory] section appears when config.memory is set."""
    generate_project(coworker_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    memory_section = data.get("tool", {}).get("apx", {}).get("agent", {}).get("memory")
    assert memory_section is not None, "[tool.apx.agent.memory] section missing"
    assert memory_section.get("type") == "delta"
    assert memory_section.get("table_name") == "main.payroll.apx_memory"


def test_memory_section_absent_when_not_configured(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """[tool.apx.agent.memory] does NOT appear when config.memory is None."""
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    memory_section = data.get("tool", {}).get("apx", {}).get("agent", {}).get("memory")
    assert memory_section is None, "[tool.apx.agent.memory] should not be present"


# ---------------------------------------------------------------------------
# Test 4: agent_server/start_server.py is created
# ---------------------------------------------------------------------------


def test_start_server_exists(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """agent_server/start_server.py must exist."""
    generate_project(coworker_config, tmp_path)
    assert (tmp_path / "agent_server" / "start_server.py").exists()
    assert (tmp_path / "agent_server" / "__init__.py").exists()


# ---------------------------------------------------------------------------
# Test 5: databricks.yml contains the agent name
# ---------------------------------------------------------------------------


def test_databricks_yml_contains_agent_name(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """databricks.yml must be valid YAML and contain the agent name."""
    generate_project(coworker_config, tmp_path)
    dab = tmp_path / "databricks.yml"
    assert dab.exists(), "databricks.yml was not created"

    data = yaml.safe_load(dab.read_text())
    assert "payroll-coworker" in str(data), "agent name not found in databricks.yml"


# ---------------------------------------------------------------------------
# Test 6: module = "agent:agent" does NOT appear when template is set
# ---------------------------------------------------------------------------


def test_no_module_line_when_template_set(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """module = \"agent:agent\" must not appear in pyproject.toml when template is set."""
    generate_project(coworker_config, tmp_path)
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'module = "agent:agent"' not in content, (
        "module = 'agent:agent' must not be written when template is set"
    )


def test_module_line_present_when_no_template(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """module = \"agent:agent\" IS written when no template is set."""
    generate_project(minimal_config, tmp_path)
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'module = "agent:agent"' in content, (
        "module = 'agent:agent' should be present when no template is set"
    )

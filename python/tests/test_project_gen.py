"""Tests for ``_project_gen.generate_project`` — AgentConfig → deployable Apps project.

Covers:
  1. pyproject.toml is created with the correct project name.
  2. [tool.apx.agent.template] section contains join_key when template is set.
  3. [tool.apx.agent.memory] section is present when config.memory is set.
  4. agent_server/start_server.py is created.
  5. databricks.yml contains the agent name.
  6. ``module = "agent:agent"`` does NOT appear when template is set.
  7. [[tool.apx.tools]] entries are emitted for each tools: dict.
  8. Skills are copied to skills/ and emitted as [[tool.apx.tools]] type=skill entries.
  9. knowledge= is NOT auto-emitted by generate_project (no bundle produced here);
     explicit config.knowledge IS emitted when set.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from apx_agent._models import AgentConfig, MemoryBackendConfig, SessionBackendConfig, SkillConfig
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


# ---------------------------------------------------------------------------
# Test 7: [[tool.apx.tools]] entries from config.tools
# ---------------------------------------------------------------------------


def test_tools_emitted_as_toml_array_of_tables(tmp_path: Path) -> None:
    """Each entry in config.tools becomes a [[tool.apx.tools]] table."""
    config = AgentConfig(
        name="demo",
        tools=[
            {"type": "genie", "space_id": "01ef", "name": "ask_sales"},
            {"type": "sql", "warehouse_id": "wh123"},
        ],
    )
    generate_project(config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    tables = (data.get("tool") or {}).get("apx", {}).get("tools") or []
    assert len(tables) == 2, f"Expected 2 [[tool.apx.tools]] entries, got {len(tables)}"
    assert tables[0]["type"] == "genie"
    assert tables[0]["space_id"] == "01ef"
    assert tables[0]["name"] == "ask_sales"
    assert tables[1]["type"] == "sql"
    assert tables[1]["warehouse_id"] == "wh123"


def test_no_tools_section_when_tools_empty(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """No [[tool.apx.tools]] entries appear when config.tools is empty."""
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    tables = (data.get("tool") or {}).get("apx", {}).get("tools")
    assert tables is None or tables == [], "[[tool.apx.tools]] should be absent when tools=[]"


# ---------------------------------------------------------------------------
# Test 8: skills copied and emitted as type=skill [[tool.apx.tools]] entries
# ---------------------------------------------------------------------------


def test_skill_file_copied_and_toml_entry_emitted(tmp_path: Path) -> None:
    """Skill markdown is copied to skills/<name>.md; TOML entry uses type=skill."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "sql_guide.md").write_text("# SQL Guide\nUse explicit column names.")

    config = AgentConfig(
        name="demo",
        skills=[
            SkillConfig(
                name="sql_guide",
                description="SQL best practices",
                path="sql_guide.md",
            )
        ],
    )
    project_dir = tmp_path / "project"
    generate_project(config, project_dir, source_dir=source_dir)

    # Skill file copied
    skill_file = project_dir / "skills" / "sql_guide.md"
    assert skill_file.exists(), "skills/sql_guide.md was not created"
    assert "SQL Guide" in skill_file.read_text()

    # TOML entry present
    with open(project_dir / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    tables = (data.get("tool") or {}).get("apx", {}).get("tools") or []
    assert len(tables) == 1
    assert tables[0]["type"] == "skill"
    assert tables[0]["name"] == "sql_guide"
    assert tables[0]["description"] == "SQL best practices"
    assert tables[0]["path"] == "skills/sql_guide.md"


def test_skills_copy_step_in_databricks_yml(tmp_path: Path) -> None:
    """When skills are declared, databricks.yml includes the cp -r skills step."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "guide.md").write_text("guide content")

    config = AgentConfig(
        name="demo",
        skills=[SkillConfig(name="guide", description="A guide", path="guide.md")],
    )
    project_dir = tmp_path / "project"
    generate_project(config, project_dir, source_dir=source_dir)

    content = (project_dir / "databricks.yml").read_text()
    assert "cp -r skills .build/" in content


def test_no_skills_copy_step_when_skills_empty(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """When no skills are declared, databricks.yml omits the cp -r skills step."""
    generate_project(minimal_config, tmp_path)
    content = (tmp_path / "databricks.yml").read_text()
    assert "cp -r skills" not in content


# ---------------------------------------------------------------------------
# Test 9: knowledge= coherence for generate_project
#
# generate_project writes NO .apx/ artifact, so it must NOT auto-emit
# knowledge = "./.apx/okf" (that would create a dangling config knob).
# Explicit config.knowledge IS emitted when the caller sets it, because that
# means they're shipping their own bundle via other means.
# ---------------------------------------------------------------------------


def test_knowledge_not_auto_emitted_for_template_with_catalog_and_schema(
    tmp_path: Path, coworker_config: AgentConfig
) -> None:
    """knowledge must NOT appear in [tool.apx.agent] for a template agent via generate_project.

    generate_project writes no .apx/okf bundle, so auto-emitting the knob
    would create a dangling reference.  The CLI scaffold path (apx-agent scaffold)
    is responsible for emitting knowledge= together with the bundle it writes.
    """
    generate_project(coworker_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
    assert agent_section.get("knowledge") is None, (
        "generate_project must not auto-emit knowledge= (no bundle produced); "
        f"got: {agent_section.get('knowledge')!r}"
    )


def test_knowledge_not_emitted_for_plain_module_agent(
    tmp_path: Path, minimal_config: AgentConfig
) -> None:
    """knowledge must NOT appear in [tool.apx.agent] for a plain agent with no template."""
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
    assert agent_section.get("knowledge") is None, (
        f"knowledge should be absent for a plain agent, got: {agent_section.get('knowledge')!r}"
    )


def test_explicit_knowledge_is_emitted_by_generate_project(tmp_path: Path) -> None:
    """When config.knowledge is explicitly set, generate_project DOES emit it.

    This covers the case where the user sets ``knowledge:`` in their YAML spec
    and ships their own bundle separately.
    """
    config = AgentConfig(name="my-agent", knowledge="./.apx/okf")
    generate_project(config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
    assert agent_section.get("knowledge") == "./.apx/okf", (
        f"Explicit config.knowledge must be emitted, got: {agent_section.get('knowledge')!r}"
    )

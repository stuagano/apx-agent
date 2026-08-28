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

import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from apx_agent import BaseAgent, CoworkerAgent, DataAgent, RemoteDatabricksAgent, RouterAgent
from apx_agent._models import (
    AgentConfig,
    ExampleWorkflow,
    MemoryBackendConfig,
    SessionBackendConfig,
    SkillConfig,
)
from apx_agent._project_gen import generate_project, render_agent_py


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
            type="lakebase",
            host="${LAKEBASE_HOST}",
            database="payroll_coworker",
            table_name="main.payroll.apx_memory",
            embedding_model="databricks-bge-large-en",
            embedding_dim=1024,
            auto_create=True,
        ),
        session=SessionBackendConfig(
            type="lakebase",
            host="${LAKEBASE_HOST}",
            database="payroll_coworker",
            table_name="main.payroll.apx_sessions",
            auto_create=True,
        ),
    )


@pytest.fixture()
def minimal_config() -> AgentConfig:
    """A minimal AgentConfig with no template, memory, or session."""
    return AgentConfig(name="my-agent")


def _tool_names(agent: BaseAgent) -> list[str]:
    """Stable list of an agent's tool names, regardless of construction path."""
    return sorted(t.name for t in agent.collect_tools())


def _exec_agent_py(src: str) -> dict[str, Any]:
    """Execute generated agent.py source and return its globals."""
    ns: dict[str, Any] = {}
    exec(compile(src, "agent.py", "exec"), ns)  # noqa: S102 — testing generated code
    return ns


# ---------------------------------------------------------------------------
# Faithfulness: the codegen'd agent.py builds the SAME agent as the template
# registry. This is the core claim of Python-canonical generation.
# ---------------------------------------------------------------------------


def test_render_agent_py_matches_registry_build(coworker_config: AgentConfig) -> None:
    """exec(render_agent_py(config)) yields the same agent template_registry.build does."""
    from apx_agent._template import template_registry

    src = render_agent_py(coworker_config)
    ns = {}
    exec(compile(src, "agent.py", "exec"), ns)  # noqa: S102 — testing generated code
    coded = ns["agent"]
    assert isinstance(coded, CoworkerAgent)

    assert coworker_config.template is not None
    spec = {k: v for k, v in coworker_config.template.items() if k != "name"}
    built = template_registry.build("coworker", spec)
    assert isinstance(built, CoworkerAgent)

    assert coded.join_key == built.join_key == "employee ID"
    assert coded.catalog == built.catalog == "main"
    assert coded.schema == built.schema == "payroll"
    assert _tool_names(coded) == _tool_names(built), (
        "codegen'd agent and registry-built agent expose different tools"
    )


def test_render_agent_py_unknown_template_falls_back_to_registry() -> None:
    """An unknown template name codegens a faithful registry.build() call, not a guess."""
    cfg = AgentConfig(name="x", template={"name": "third_party", "catalog": "c", "schema": "s"})
    src = render_agent_py(cfg)
    assert "template_registry.build('third_party'" in src
    compile(src, "agent.py", "exec")


def test_render_agent_py_graph_router_with_data_and_agent_leaves() -> None:
    """YAML graph declarations materialize into a real RouterAgent tree."""
    cfg = AgentConfig(
        name="revenue_ops",
        agents={
            "sales": {
                "type": "data",
                "catalog": "main",
                "schema": "sales",
                "description": "Handles sales and revenue questions.",
                "knowledge": "./.apx/okf/sales",
            },
            "contracts": {
                "type": "agent",
                "instructions": "Answer contract questions.",
                "description": "Handles contract terms and renewals.",
                "sub_agents": ["$CONTRACT_INSPECTOR_URL"],
            },
        },
        root={
            "type": "router",
            "agents": ["sales", "contracts"],
            "instructions": "Route to the right specialist.",
        },
    )

    ns = _exec_agent_py(render_agent_py(cfg))

    assert isinstance(ns["sales"], DataAgent)
    assert ns["sales"].catalog == "main"
    assert ns["sales"].schema == "sales"
    assert isinstance(ns["agent"], RouterAgent)
    assert ns["contracts"]._sub_agent_urls == ["$CONTRACT_INSPECTOR_URL"]


def test_generate_project_graph_writes_agent_py_not_root_sub_agents(tmp_path: Path) -> None:
    """Graph specs keep remote sub_agents on leaves, avoiding ignored root config merge."""
    cfg = AgentConfig(
        name="graph_agent",
        sub_agents=["$ROOT_REMOTE_IGNORED"],
        agents={
            "sales": {
                "type": "data",
                "catalog": "main",
                "schema": "sales",
                "sub_agents": ["$SALES_INSPECTOR_URL"],
            },
            "general": {"type": "agent", "instructions": "Answer general questions."},
        },
        root={"type": "router", "agents": ["sales", "general"]},
    )

    generate_project(cfg, tmp_path)
    ns = _exec_agent_py((tmp_path / "agent.py").read_text())

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
    assert "sub_agents" not in agent_section
    assert ns["sales"]._sub_agent_urls == ["$SALES_INSPECTOR_URL"]


def test_render_agent_py_graph_colliding_leaf_names_raise() -> None:
    """Leaf names that sanitize to the same Python variable are rejected, not shadowed."""
    cfg = AgentConfig(
        name="collide",
        agents={
            "my-agent": {"type": "agent", "instructions": "A."},
            "my_agent": {"type": "agent", "instructions": "B."},
        },
        root={"type": "router", "agents": ["my-agent", "my_agent"]},
    )

    with pytest.raises(ValueError, match="both map to variable"):
        render_agent_py(cfg)


def test_render_agent_py_graph_handoff_unknown_start_raises() -> None:
    """A handoff start naming no declared leaf fails at codegen, not deploy-time import."""
    cfg = AgentConfig(
        name="triage_flow",
        agents={
            "a": {"type": "agent", "instructions": "A."},
            "b": {"type": "agent", "instructions": "B."},
        },
        root={"type": "handoff", "agents": ["a", "b"], "start": "triage"},
    )

    with pytest.raises(ValueError, match="unknown start agent"):
        render_agent_py(cfg)


def test_render_agent_py_graph_two_python_tool_leaves_emit_helper_once() -> None:
    """The _load_python_tool helper is defined once, not per python-tool leaf."""
    module = ModuleType("apx_test_two_leaf_tools")

    def tool_a() -> str:
        """Tool a."""
        return "a"

    def tool_b() -> str:
        """Tool b."""
        return "b"

    module.tool_a = tool_a  # type: ignore[attr-defined]
    module.tool_b = tool_b  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    try:
        cfg = AgentConfig(
            name="two_tools",
            agents={
                "x": {
                    "type": "agent",
                    "instructions": "X.",
                    "tools": [{"type": "python", "module": "apx_test_two_leaf_tools:tool_a"}],
                },
                "y": {
                    "type": "agent",
                    "instructions": "Y.",
                    "tools": [{"type": "python", "module": "apx_test_two_leaf_tools:tool_b"}],
                },
            },
            root={"type": "router", "agents": ["x", "y"]},
        )

        src = render_agent_py(cfg)
        assert src.count("def _load_python_tool(") == 1
        _exec_agent_py(src)  # still importable
    finally:
        sys.modules.pop(module.__name__, None)


def test_render_agent_py_graph_loop_root() -> None:
    """Loop roots take a single named agent, not an agents list."""
    cfg = AgentConfig(
        name="review_loop",
        agents={"drafter": {"type": "agent", "instructions": "Draft until done."}},
        root={"type": "loop", "agent": "drafter", "max_iterations": 2},
    )

    ns = _exec_agent_py(render_agent_py(cfg))

    assert type(ns["agent"]).__name__ == "LoopAgent"


def test_render_agent_py_graph_leaf_python_tool() -> None:
    """Leaf tools can import plain Python callables by module:attr."""
    module = ModuleType("apx_test_leaf_tools")

    def lookup_order(order_id: str) -> str:
        """Look up an order by id."""
        return order_id

    module.lookup_order = lookup_order  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    try:
        cfg = AgentConfig(
            name="tool_graph",
            agents={
                "orders": {
                    "type": "agent",
                    "instructions": "Answer order questions.",
                    "tools": [
                        {"type": "python", "module": "apx_test_leaf_tools:lookup_order"}
                    ],
                }
            },
            root={"type": "router", "agents": ["orders"]},
        )

        ns = _exec_agent_py(render_agent_py(cfg))

        assert _tool_names(ns["orders"]) == ["lookup_order"]
    finally:
        sys.modules.pop(module.__name__, None)


def test_render_agent_py_graph_leaf_config_tools() -> None:
    """Leaf tools reuse the existing declarative tool factories."""
    cfg = AgentConfig(
        name="tool_graph",
        agents={
            "sales": {
                "type": "agent",
                "tools": [
                    {
                        "type": "skill",
                        "name": "sales_guide",
                        "description": "Load the sales guide.",
                        "path": "skills/sales.md",
                    },
                ],
            }
        },
        root={"type": "router", "agents": ["sales"]},
    )

    ns = _exec_agent_py(render_agent_py(cfg))

    assert _tool_names(ns["sales"]) == ["sales_guide"]


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
# Test 2: a template config codegens a single agent.py (Python-canonical) and
# no [tool.apx.agent.template] section is emitted.
# ---------------------------------------------------------------------------


def test_template_codegens_agent_py(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """A template config writes a top-level agent.py constructing the agent in code."""
    generate_project(coworker_config, tmp_path)

    agent_py = tmp_path / "agent.py"
    assert agent_py.exists(), "agent.py was not generated for a template config"
    src = agent_py.read_text()
    assert "from apx_agent.coworker import CoworkerAgent" in src
    assert "agent = CoworkerAgent(" in src
    assert "'main'" in src and "'payroll'" in src  # catalog, schema positional
    assert "join_key='employee ID'" in src
    # The generated source must be importable Python.
    compile(src, "agent.py", "exec")

    # The declarative envelope is now in code — no [template] section in pyproject.
    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert "template" not in data.get("tool", {}).get("apx", {}).get("agent", {}), (
        "[tool.apx.agent.template] must not be emitted once the agent is codegen'd"
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
    assert memory_section.get("type") == "lakebase"
    assert memory_section.get("table_name") == "main.payroll.apx_memory"


def test_memory_section_absent_when_not_configured(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """[tool.apx.agent.memory] does NOT appear when config.memory is None."""
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    memory_section = data.get("tool", {}).get("apx", {}).get("agent", {}).get("memory")
    assert memory_section is None, "[tool.apx.agent.memory] should not be present"


def test_workflows_section_present_when_configured(tmp_path: Path) -> None:
    config = AgentConfig(
        name="pricing-agent",
        workflows=[
            ExampleWorkflow(
                id="pricing-review",
                title="Pricing review",
                question="Show me the pricing evidence",
                purpose="Move from signal to decision.",
                route=["intelligence", "calibrate"],
                outcome="Reviewable pricing packet",
            )
        ],
    )
    generate_project(config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    workflows = data["tool"]["apx"]["agent"]["workflows"]
    assert workflows[0]["id"] == "pricing-review"
    assert workflows[0]["route"] == ["intelligence", "calibrate"]
    assert workflows[0]["handoffs"] == []


def test_workflows_toml_escapes_control_characters(tmp_path: Path) -> None:
    config = AgentConfig(
        name="control-character-agent",
        workflows=[
            ExampleWorkflow(
                id="control-character-workflow",
                title="First\rSecond\x01",
                question="Show me the evidence",
                purpose="Check generated TOML.",
                route=["inspect"],
            )
        ],
    )
    generate_project(config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    assert data["tool"]["apx"]["agent"]["workflows"][0]["title"] == "First\rSecond\x01"


def test_workflows_section_absent_when_empty(tmp_path: Path, minimal_config: AgentConfig) -> None:
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    assert "workflows" not in data["tool"]["apx"]["agent"]


# ---------------------------------------------------------------------------
# Test 4: agent_server/start_server.py is created
# ---------------------------------------------------------------------------


def test_start_server_exists(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """agent_server/start_server.py must exist."""
    generate_project(coworker_config, tmp_path)
    assert (tmp_path / "agent_server" / "start_server.py").exists()
    assert (tmp_path / "agent_server" / "start_host.py").exists()
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
    app = data["resources"]["apps"]["payroll-coworker"]
    assert ".build/apx_appkit_host" in data["artifacts"]["default"]["build"]
    assert app["config"]["command"] == ["python", "-m", "agent_server.start_host"]
    env = {item["name"]: item["value"] for item in app["config"]["env"]}
    assert env["APX_APPS_HOST"] == "appkit"


# ---------------------------------------------------------------------------
# Test 6: module = "agent:agent" is always written (Python-canonical) — the
# generated agent.py is the single definition whether or not a template is set.
# ---------------------------------------------------------------------------


def test_module_line_present_when_template_set(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """module = \"agent:agent\" IS written when a template is set (it codegens agent.py)."""
    generate_project(coworker_config, tmp_path)
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'module = "agent:agent"' in content, (
        "module = 'agent:agent' should be present — the template codegens agent.py"
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


# ---------------------------------------------------------------------------
# PRD: Harden YAML graph-spec compilation — AC-1..AC-7 gate tests.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_render_agent_py_graph_remote_leaf() -> None:
    """AC-1: a remote leaf renders RemoteDatabricksAgent and execs to an instance."""
    cfg = AgentConfig(
        name="a2a_graph",
        agents={"billing": {"type": "remote", "url": "https://x/.well-known/agent.json"}},
        root={"type": "sequential", "agents": ["billing"]},
    )

    src = render_agent_py(cfg)
    assert "RemoteDatabricksAgent" in src
    assert "billing = RemoteDatabricksAgent('https://x/.well-known/agent.json')" in src

    ns = _exec_agent_py(src)
    assert isinstance(ns["billing"], RemoteDatabricksAgent)


@pytest.mark.parametrize("kind", ["router", "handoff"])
def test_render_agent_py_graph_remote_leaf_under_router_or_handoff_raises(kind: str) -> None:
    """A remote leaf under router/handoff is rejected at codegen, not at import.

    RouterAgent/HandoffAgent read each member's name when building routing tools,
    but RemoteDatabricksAgent has no name until its card is fetched async — so an
    unguarded graph would raise a ValueError only at ``import agent``. The guard
    moves that failure to render time, naming the offending leaf.
    """
    cfg = AgentConfig(
        name="a2a_bad",
        agents={"billing": {"type": "remote", "url": "https://x/.well-known/agent.json"}},
        root={"type": kind, "agents": ["billing"]},
    )

    with pytest.raises(ValueError, match="billing"):
        render_agent_py(cfg)


def test_render_agent_py_graph_root_unknown_key_raises() -> None:
    """AC-2: an unknown root key fails at codegen, naming the offending key."""
    cfg = AgentConfig(
        name="bad_root",
        agents={"a": {"type": "agent", "instructions": "A."}},
        root={"type": "router", "agents": ["a"], "bogus": 1},
    )

    with pytest.raises(ValueError, match="bogus"):
        render_agent_py(cfg)


def test_render_agent_py_graph_leaf_unknown_key_raises() -> None:
    """AC-3: an unknown leaf key raises ValueError naming it, not an import TypeError."""
    cfg = AgentConfig(
        name="typo_leaf",
        agents={"a": {"type": "agent", "instrctions": "x"}},
        root={"type": "router", "agents": ["a"]},
    )

    with pytest.raises(ValueError, match="instrctions"):
        render_agent_py(cfg)


def test_render_agent_py_graph_instructions_on_handoff_loop_raises() -> None:
    """AC-4: instructions on handoff or loop roots is rejected, not silently dropped."""
    handoff = AgentConfig(
        name="ho",
        agents={"a": {"type": "agent", "instructions": "A."}, "b": {"type": "agent", "instructions": "B."}},
        root={"type": "handoff", "agents": ["a", "b"], "instructions": "nope"},
    )
    with pytest.raises(ValueError, match="instructions"):
        render_agent_py(handoff)

    loop = AgentConfig(
        name="lo",
        agents={"a": {"type": "agent", "instructions": "A."}},
        root={"type": "loop", "agent": "a", "instructions": "nope"},
    )
    with pytest.raises(ValueError, match="instructions"):
        render_agent_py(loop)


def test_render_agent_py_graph_loop_max_iterations_validated() -> None:
    """AC-5: loop max_iterations must be a positive int; a valid one renders through."""
    bad = AgentConfig(
        name="lo_bad",
        agents={"a": {"type": "agent", "instructions": "A."}},
        root={"type": "loop", "agent": "a", "max_iterations": "lots"},
    )
    with pytest.raises(ValueError, match="max_iterations"):
        render_agent_py(bad)

    good = AgentConfig(
        name="lo_good",
        agents={"a": {"type": "agent", "instructions": "A."}},
        root={"type": "loop", "agent": "a", "max_iterations": 3},
    )
    assert "max_iterations=3" in render_agent_py(good)


def test_configuration_doc_covers_all_graph_kinds() -> None:
    """AC-6: configuration.md documents every leaf and root kind."""
    doc = (_REPO_ROOT / "docs" / "reference" / "configuration.md").read_text()
    for kind in ("agent", "data", "coworker", "remote", "router", "sequential", "parallel", "handoff", "loop"):
        # Graph specs are authored in YAML (type: x) or pyproject TOML (type = "x");
        # accept either so the doc isn't forced into one syntax.
        assert f'type = "{kind}"' in doc or f"type: {kind}" in doc, (
            f"configuration.md missing example for {kind!r}"
        )


def test_example_graph_spec_compiles(tmp_path: Path) -> None:
    """AC-7: the committed example graph spec compiles and execs to its declared root."""
    from apx_agent._inspection import _load_agent_config

    example = _REPO_ROOT / "examples" / "graph_spec" / "pyproject.toml"
    config = _load_agent_config(pyproject_path=example)
    assert config is not None

    generate_project(config, tmp_path)
    ns = _exec_agent_py((tmp_path / "agent.py").read_text())
    assert isinstance(ns["agent"], RouterAgent)

"""Tests for ``_project_gen.generate_project`` — AgentConfig → deployable Apps project.

Covers:
  1. pyproject.toml is created with the correct project name.
  2. [tool.apx.agent.template] section contains join_key when template is set.
  3. [tool.apx.agent.memory] section is present when config.memory is set.
  4. agent_server/start_server.py is created.
  5. databricks.yml contains the agent name.
  6. ``module = "agent:agent"`` does NOT appear when template is set.
  7. tools: dicts become explicit Python factory calls in the tools/ package.
  8. Skills are copied to skills/ and exposed as Python skill_tool declarations.
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


@pytest.mark.parametrize("target", ["durable_agent_server"])
def test_generated_dependencies_use_published_extras(tmp_path: Path, target: str) -> None:
    from packaging.requirements import Requirement

    generate_project(AgentConfig(name="dependencies", target=target), tmp_path)
    project = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    package = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    published = package["project"]["optional-dependencies"]
    apx = next(dep for dep in requirements if dep.name == "apx-agent")
    assert apx.extras <= published.keys()
    assert "eval" in apx.extras
    assert ("agentbricks" in apx.extras) == (target == "durable_agent_server")
    assert not any(dep.name == "mlflow" for dep in requirements)
    assert "mlflow[databricks]>=3.14,<3.15" in published["eval"]


@pytest.mark.parametrize("backend", ["session", "memory", "example"])
@pytest.mark.parametrize("store_type", ["lakebase", "inmemory"])
def test_declared_storage_selects_dependency_extra(tmp_path: Path, backend: str, store_type: str) -> None:
    from packaging.requirements import Requirement

    config = AgentConfig.model_validate({
        "name": "storage", "target": "responses_agent", backend: {"type": store_type},
    })
    generate_project(config, tmp_path)
    project = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    dependency = Requirement(project["project"]["dependencies"][0])
    assert ("lakebase" in dependency.extras) == (store_type == "lakebase")


@pytest.mark.parametrize("target", ["durable_agent_server"])
def test_managed_memory_generated_dependency(tmp_path: Path, target: str) -> None:
    config = AgentConfig(name="memory-agent", target=target, model="test",
                         memory={"type": "managed", "store_name": "agent-memory"})
    generate_project(config, tmp_path)
    project = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "apx-agent[eval,agentbricks]" in project["project"]["dependencies"]
    assert _effective(tmp_path).memory.store_name == "agent-memory"


def test_native_project_roundtrip_needs_no_bundle_cleanup(tmp_path: Path) -> None:
    from ctk import Artifact, verify
    from apx_agent._inspection import _load_agent_config

    config = AgentConfig(
        name="native-proof", target="durable_agent_server", model="test-model",
        deploy={"space": "shared-space"},
        session={"type": "managed", "store_name": "conversation-store"},
    )
    from apx_agent._yaml_spec import load_spec

    spec = tmp_path / "native.yaml"
    spec.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)))
    generate_project(load_spec(spec), tmp_path)
    verify(Artifact(str(tmp_path / "databricks.yml"), must_contain="shared-space"))
    verify(Artifact(str(tmp_path / "agent_server" / "start_server.py"), min_bytes=200,
                    must_contain="resolve_settings(agent, _load_agent_config())"))
    assert _effective(tmp_path).model_dump() == config.model_dump()
    namespace: dict[str, Any] = {}
    exec((tmp_path / "agent.py").read_text(), namespace)
    assert namespace["agent"]._name == config.name
    project = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "apx-agent[eval,agentbricks]" in project["project"]["dependencies"]
    bundle = yaml.safe_load((tmp_path / "databricks.yml").read_text())
    app = bundle["resources"]["apps"]["native-proof"]
    assert app["space"] == "shared-space"
    assert not {"resources", "user_api_scopes", "permissions", "compute_min_instances", "compute_max_instances"} & app.keys()
    assert set(bundle["resources"]) == {"apps"}
    env = {e["name"]: e["value"] for e in app["config"]["env"]}
    assert env["APX_RUNTIME"] == "apps"
    assert "APX_APPS_HOST" not in env
    assert "AGENT_SESSION_STORE" not in env
    assert "MLFLOW_EXPERIMENT_ID" not in env


@pytest.mark.parametrize("fields", [
    {"target": "responses_agent", "deploy": {"space": "shared"}},
    {"deploy": {"space": "shared", "instances": 2}},
    {"session": {"type": "managed", "store_name": " "}},
    {"session": {"type": "inmemory"}},
    {"target": "responses_agent", "session": {"type": "managed", "store_name": "sessions"}},
])
def test_unsupported_target_configuration_fails_before_generation(fields: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_project(AgentConfig(name="invalid", **fields), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_automatic_store_names_survive_project_and_native_manifest_roundtrip(tmp_path: Path) -> None:
    from ctk import Artifact, verify
    from apx_agent import LlmAgent
    from apx_agent._apps_authorization import compile_authorization_plan
    from apx_agent._durable_agent import build_native_manifest
    from apx_agent._inspection import _load_agent_config

    config = AgentConfig(name="orders", target="durable_agent_server",
                         memory={"type": "managed"}, session={"type": "managed"})
    generate_project(config, tmp_path)
    verify(Artifact(str(tmp_path / "agent.py"), must_contain="apx-orders-sessions"))  # 3b-3b: on the agent
    loaded = _effective(tmp_path)
    assert loaded.memory.store_name == "apx-orders-memory"
    assert loaded.session.store_name == "apx-orders-sessions"
    agent = LlmAgent(name="orders")
    plan = compile_authorization_plan(agent, model=config.model)
    manifest = tomllib.loads(build_native_manifest(config=loaded, authorization_plan=plan, agent=agent))
    assert manifest["memory_store"]["name"] == loaded.memory.store_name
    assert manifest["session_store"]["name"] == loaded.session.store_name


@pytest.fixture()
def coworker_config() -> AgentConfig:
    """An AgentConfig that exercises the template + memory + session path."""
    return AgentConfig(
        name="payroll-coworker",
        target="responses_agent",
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
    return AgentConfig(name="my-agent", target="responses_agent")


def _tool_names(agent: BaseAgent) -> list[str]:
    """Stable list of an agent's tool names, regardless of construction path."""
    return sorted(t.name for t in agent.collect_tools())


def _effective(root: Path) -> AgentConfig:
    """The generated project's effective config: its agent.py settings merged with its TOML (3b-3b)."""
    import runpy

    from apx_agent._inspection import _load_agent_config
    from apx_agent._settings import resolve_settings

    sys.path.insert(0, str(root))
    try:
        agent = runpy.run_path(str(root / "agent.py"))["agent"]
    finally:
        sys.path.remove(str(root))
    toml = _load_agent_config(pyproject_path=root / "pyproject.toml")
    return resolve_settings(agent, toml)


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

    assert "$ROOT_REMOTE_IGNORED" not in (tmp_path / "agent.py").read_text()
    assert "$ROOT_REMOTE_IGNORED" not in (tmp_path / "pyproject.toml").read_text()
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


def test_render_agent_py_graph_two_python_tool_leaves_use_static_imports() -> None:
    """Each leaf imports its callable explicitly without runtime import dispatch."""
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
        assert "_load_python_tool" not in src
        assert "from apx_test_two_leaf_tools import tool_a" in src
        assert "from apx_test_two_leaf_tools import tool_b" in src
        ns = _exec_agent_py(src)
        assert _tool_names(ns["x"]) == ["tool_a"]
        assert _tool_names(ns["y"]) == ["tool_b"]
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

    memory = _effective(tmp_path).memory
    assert memory is not None, "memory missing from the generated project"
    assert memory.type == "lakebase"
    assert memory.table_name == "main.payroll.apx_memory"


def test_memory_section_absent_when_not_configured(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """[tool.apx.agent.memory] does NOT appear when config.memory is None."""
    generate_project(minimal_config, tmp_path)

    assert _effective(tmp_path).memory is None, "memory should not be declared"


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

    workflows = _effective(tmp_path).workflows
    assert workflows[0].id == "pricing-review"
    assert workflows[0].route == ["intelligence", "calibrate"]
    assert workflows[0].handoffs == []


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

    assert _effective(tmp_path).workflows[0].title == "First\rSecond\x01"


def test_workflows_section_absent_when_empty(tmp_path: Path, minimal_config: AgentConfig) -> None:
    generate_project(minimal_config, tmp_path)

    assert _effective(tmp_path).workflows == []


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
    assert ".build/apx_appkit_host" not in data["artifacts"]["default"]["build"]
    assert app["config"]["command"] == ["python", "-m", "agent_server.start_host"]
    assert app["user_api_scopes"] == ["ai-gateway", "sql"]
    resource_names = [entry.get("name") for entry in app["resources"]]
    assert "llm-endpoint" not in resource_names
    env = {item["name"]: item["value"] for item in app["config"]["env"]}
    assert env["APX_RUNTIME"] == "apps"
    assert "APX_APPS_HOST" not in env


# ---------------------------------------------------------------------------
# Test 6: module = "agent:agent" is always written (Python-canonical) — the
# generated agent.py is the single definition whether or not a template is set.
# ---------------------------------------------------------------------------


def test_no_agent_table_or_module_line_when_template_set(tmp_path: Path, coworker_config: AgentConfig) -> None:
    """3b-3b: the template codegens agent.py, so pyproject carries no [tool.apx.agent] or module line."""
    generate_project(coworker_config, tmp_path)
    content = (tmp_path / "pyproject.toml").read_text()
    assert "[tool.apx.agent" not in content and "module =" not in content


def test_no_agent_table_or_module_line_when_no_template(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """3b-3b: agent.py is the agent; pyproject carries no [tool.apx.agent] or module line."""
    generate_project(minimal_config, tmp_path)
    content = (tmp_path / "pyproject.toml").read_text()
    assert "[tool.apx.agent" not in content and "module =" not in content


# ---------------------------------------------------------------------------
# Test 7: [[tool.apx.tools]] entries from config.tools
# ---------------------------------------------------------------------------


def test_tools_emitted_as_python_factory_calls(tmp_path: Path) -> None:
    """Tools are Python declarations, not runtime TOML dispatch."""
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

    assert "tools" not in data["tool"]["apx"]
    assert (tmp_path / "tools" / "__init__.py").is_file()
    source = "\n".join(p.read_text() for p in (tmp_path / "tools").glob("*.py"))
    assert "genie_tool(" in source and "sql_tool(" in source
    assert "space_id='01ef'" in source and "warehouse_id='wh123'" in source
    assert "from tools." in (tmp_path / "agent.py").read_text()
    assert "load_config_tools" not in source


def test_no_tools_section_when_tools_empty(tmp_path: Path, minimal_config: AgentConfig) -> None:
    """No [[tool.apx.tools]] entries appear when config.tools is empty."""
    generate_project(minimal_config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    tables = (data.get("tool") or {}).get("apx", {}).get("tools")
    assert tables is None or tables == [], "[[tool.apx.tools]] should be absent when tools=[]"


# ---------------------------------------------------------------------------
# Test 8: skills copied and exposed as Python skill_tool declarations
# ---------------------------------------------------------------------------


def test_skill_file_copied_and_python_tool_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copied skill markdown is exposed by a real Python-authored callable."""
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

    with open(project_dir / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert "tools" not in data["tool"]["apx"]
    agent = _exec_generated_project(project_dir, monkeypatch)["agent"]
    assert _tool_names(agent) == ["sql_guide"]
    skill = agent._tool_fns[0]
    assert skill.__doc__ == "SQL best practices"
    monkeypatch.chdir(project_dir)
    assert skill() == "# SQL Guide\nUse explicit column names."


def test_skills_copy_step_in_databricks_yml(tmp_path: Path) -> None:
    """When skills are declared, databricks.yml includes the cp -r skills step."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "guide.md").write_text("guide content")

    config = AgentConfig(
        name="demo",
        target="responses_agent",
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

    knowledge = _effective(tmp_path).knowledge
    assert knowledge == "./.apx/okf", f"Explicit config.knowledge must be emitted, got: {knowledge!r}"


# ---------------------------------------------------------------------------
# PRD: Harden YAML graph-spec compilation — AC-1..AC-7 gate tests.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_generate_project_emits_declared_binding_without_transport_in_agent_py(
    tmp_path: Path,
) -> None:
    """A declared internal leaf is configured in TOML but stays logical in code."""
    config = AgentConfig(
        name="declared-graph",
        model="model",
        bindings={"pricing": "$PRICING_APP_URL"},
        agents={
            "review": {
                "type": "agent",
                "instructions": "Review the request before pricing.",
            },
            "pricing": {
                "type": "agent",
                "instructions": "Return the approved price.",
            },
        },
        root={"type": "sequential", "agents": ["review", "pricing"]},
    )

    generate_project(config, tmp_path)

    with open(tmp_path / "pyproject.toml", "rb") as f:
        generated = tomllib.load(f)
    assert generated["tool"]["apx"]["deploy"]["bindings"] == {
        "pricing": "$PRICING_APP_URL"
    }

    source = (tmp_path / "agent.py").read_text()
    assert "pricing = Agent(" in source
    assert "name='pricing'" in source
    assert "RemoteDatabricksAgent" not in source
    assert "$PRICING_APP_URL" not in source
    assert ".well-known/agent.json" not in source
    assert "http://" not in source
    assert "https://" not in source


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
    from apx_agent._yaml_spec import load_spec

    config = load_spec(_REPO_ROOT / "python" / "examples" / "graph_spec.yaml")
    assert (config.name, config.model) == ("graph-spec-example", "databricks-claude-sonnet-4-6")
    assert sorted(config.agents) == ["sales", "support"] and config.root is not None

    generate_project(config, tmp_path)
    ns = _exec_agent_py((tmp_path / "agent.py").read_text())
    assert isinstance(ns["agent"], RouterAgent)


def _exec_generated_project(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Import the generated package, not a cached tools module from another project."""
    with monkeypatch.context() as patch:
        patch.syspath_prepend(str(path))
        patch.chdir(path)
        for name in list(sys.modules):
            if name == "tools" or name.startswith("tools."):
                patch.delitem(sys.modules, name)
        try:
            return _exec_agent_py((path / "agent.py").read_text())
        finally:
            for name in list(sys.modules):
                if name == "tools" or name.startswith("tools."):
                    sys.modules.pop(name)


@pytest.mark.parametrize("mode", ["plain", "template", "graph"])
def test_native_tools_compose_once_with_resources_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from apx_agent._resources import get_resources
    from apx_agent._tool import get_tool_metadata
    from apx_agent._tool_scope import get_scope

    declaration = {
        "type": "uc_function", "function_name": "main.sales.lookup",
        "name": "lookup", "description": "Look up sales.",
        "identity": "obo", "scope": {"schemas": ["main.sales"]},
        "secret_scopes": ["sales"], "callers": ["analyst@example.com"],
    }
    fields: dict[str, Any] = {"tools": [declaration]}
    if mode == "template":
        fields["template"] = {"name": "data", "catalog": "main", "schema": "sales"}
    if mode == "graph":
        fields = {
            "agents": {"sales": {"type": "agent", "tools": [declaration]}},
            "root": {"type": "sequential", "agents": ["sales"]},
        }
    generate_project(AgentConfig(name="native-tools", **fields), tmp_path)
    source = (tmp_path / "agent.py").read_text()
    assert "from tools." in source
    assert "load_config_tools" not in source
    ns = _exec_generated_project(tmp_path, monkeypatch)
    leaf = ns["sales"] if mode == "graph" else ns["agent"]
    tools = [fn for fn in leaf._tool_fns if fn.__name__ == "lookup"]
    assert len(tools) == 1
    assert tools[0].__doc__ == "Look up sales."
    assert [resource.identifier for resource in get_resources(tools[0])] == ["main.sales.lookup"]
    assert get_scope(tools[0]).schemas == ("main.sales",)
    assert get_scope(tools[0]).callers == ("analyst@example.com",)
    assert get_scope(tools[0]).secret_scopes == ("sales",)
    assert get_tool_metadata(tools[0]).execution == "user"


def test_native_tools_env_is_resolved_at_import_not_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent._resources import get_resources

    monkeypatch.setenv("TOOL_WAREHOUSE", "generation-value-must-not-leak")
    generate_project(AgentConfig(name="env-tools", tools=[
        {"type": "sql", "warehouse_id": "${TOOL_WAREHOUSE}"},
    ]), tmp_path)
    sources = "\n".join(p.read_text() for p in tmp_path.rglob("*.py"))
    assert "generation-value-must-not-leak" not in sources
    monkeypatch.setenv("TOOL_WAREHOUSE", "runtime-warehouse")
    agent = _exec_generated_project(tmp_path, monkeypatch)["agent"]
    assert get_resources(agent._tool_fns[0])[0].identifier == "runtime-warehouse"
    monkeypatch.delenv("TOOL_WAREHOUSE")
    with pytest.raises((KeyError, ValueError), match="TOOL_WAREHOUSE"):
        _exec_generated_project(tmp_path, monkeypatch)


@pytest.mark.parametrize("tool, message", [
    ({"type": "genie"}, "space_id"),
    ({"type": "unknown"}, "unknown"),
    ({"type": "sql", "typo": 1}, "typo"),
    ({"type": "sql", "scope": {"schema": ["main.sales"]}}, "schema"),
    ({"type": "sql", "callers": []}, "callers"),
    ({"type": "python", "module": "not-an-import:fn"}, "module"),
])
def test_native_tools_invalid_configuration_fails_before_writes(
    tmp_path: Path, tool: dict[str, Any], message: str,
) -> None:
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=message):
        generate_project(AgentConfig(name="invalid-tools", tools=[tool]), output)
    assert not output.exists()


@pytest.mark.parametrize("collision", ["tools.py", "tools"])
def test_native_tools_collision_preserves_authored_files(tmp_path: Path, collision: str) -> None:
    path = tmp_path / collision
    if collision == "tools":
        path.mkdir()
        path = path / "__init__.py"
    path.write_text("# user authored\n")
    with pytest.raises(ValueError, match="tools"):
        generate_project(AgentConfig(name="collision", tools=[{"type": "sql"}]), tmp_path)
    assert path.read_text() == "# user authored\n"
    assert not (tmp_path / "agent.py").exists()


def test_native_tools_over_scope_fails_during_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generate_project(AgentConfig(name="scope", tools=[{
        "type": "uc_function", "function_name": "other.sales.lookup",
        "scope": {"catalogs": ["main"]},
    }]), tmp_path)
    with pytest.raises(ValueError, match="over-scoped"):
        _exec_generated_project(tmp_path, monkeypatch)


def test_native_tools_toolkit_and_static_python_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("apx_test_custom_tool")
    def custom(value: str) -> str:
        """Return the custom value."""
        return value
    module.custom = custom
    monkeypatch.setitem(sys.modules, module.__name__, module)
    generate_project(AgentConfig(name="toolkit", tools=[
        {"type": "python", "module": "apx_test_custom_tool:custom"},
        {"type": "jobs", "warehouse_id": "warehouse", "identity": "obo"},
    ]), tmp_path)
    sources = "\n".join(p.read_text() for p in (tmp_path / "tools").glob("*.py"))
    assert "from apx_test_custom_tool import custom" in sources
    assert "import_module" not in sources
    agent = _exec_generated_project(tmp_path, monkeypatch)["agent"]
    assert len(agent._tool_fns) == 5
    assert len({fn.__name__ for fn in agent._tool_fns}) == 5
    assert agent._tool_fns[0]("real result") == "real result"
    from apx_agent._tool_scope import get_scope
    assert all(get_scope(fn).identity == "obo" for fn in agent._tool_fns[1:])


def test_native_tools_duplicate_names_fail_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generate_project(AgentConfig(name="duplicates", tools=[
        {"type": "sql", "name": "same"}, {"type": "sql", "name": "same"},
    ]), tmp_path)
    with pytest.raises(ValueError, match="duplicate tool name"):
        _exec_generated_project(tmp_path, monkeypatch)


@pytest.mark.parametrize("field", ["tools", "skills"])
def test_native_tools_graph_top_level_is_rejected_not_dropped(tmp_path: Path, field: str) -> None:
    config = AgentConfig.model_validate({
        "name": "graph", "agents": {"leaf": {"type": "agent"}},
        "root": {"type": "sequential", "agents": ["leaf"]},
        field: ([{"type": "sql"}] if field == "tools" else
                [{"name": "guide", "description": "Read guide", "path": "guide.md"}]),
    })
    with pytest.raises(ValueError, match="leaf"):
        generate_project(config, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("mode", ["template", "graph"])
def test_native_tools_extra_tools_and_tools_are_both_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    spec = {"catalog": "main", "schema": "sales", "extra_tools": [
        {"type": "skill", "name": "extra", "description": "Extra guide", "path": "extra.md"},
    ]}
    tools = [{"type": "uc_function", "function": "main.sales.lookup"}]
    fields = ({"template": {"name": "data", **spec}, "tools": tools} if mode == "template" else {
        "agents": {"sales": {"type": "data", **spec, "tools": tools}},
        "root": {"type": "sequential", "agents": ["sales"]},
    })
    generate_project(AgentConfig(name="extras", **fields), tmp_path)
    ns = _exec_generated_project(tmp_path, monkeypatch)
    leaf = ns["agent"] if mode == "template" else ns["sales"]
    names = _tool_names(leaf)
    assert names.count("extra") == 1
    assert names.count("lookup") == 1


def test_native_tools_template_builtin_name_collision_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate_project(AgentConfig(name="duplicates", tools=[{"type": "sql"}],
        template={"name": "data", "catalog": "main", "schema": "sales"}), tmp_path)
    with pytest.raises(ValueError, match="duplicate tool name"):
        _exec_generated_project(tmp_path, monkeypatch)


def test_native_tools_broken_symlink_collision_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "tools").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="tools"):
        generate_project(AgentConfig(name="collision", tools=[{"type": "sql"}]), tmp_path)
    assert (tmp_path / "tools").is_symlink()
    assert not (tmp_path / "agent.py").exists()


_PARITY_TOOLS = [
    {"type": "uc_function", "function_name": "main.sales.lookup", "identity": "obo",
     "scope": {"schemas": ["main.sales"]}, "callers": ["analyst@example.com"]},
    {"type": "sql", "warehouse_id": "wh123"},
]


def test_python_and_legacy_tools_compile_equivalent_authorization_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration parity: generated Python tools govern exactly like the legacy loader."""
    from apx_agent import LlmAgent, load_config_tools
    from apx_agent._apps_authorization import compile_authorization_plan
    from apx_agent._durable_agent import build_native_manifest

    config = AgentConfig(name="parity", target="durable_agent_server", tools=_PARITY_TOOLS)
    generate_project(config, tmp_path)
    python_agent = _exec_generated_project(tmp_path, monkeypatch)["agent"]
    legacy_agent = LlmAgent(name="parity", tools=load_config_tools(_PARITY_TOOLS))

    plans = [compile_authorization_plan(a, model=config.model) for a in (python_agent, legacy_agent)]
    assert plans[0] == plans[1]
    assert len(plans[0].user_resources) == 2
    manifests = [
        build_native_manifest(config=config, authorization_plan=plan, agent=agent)
        for plan, agent in zip(plans, (python_agent, legacy_agent))
    ]
    assert manifests[0] == manifests[1]
    assert tomllib.loads(manifests[0])["auth"]["user"]["required"] is True


def test_python_and_legacy_tools_both_deny_over_scoped_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent import load_config_tools

    denied = [{"type": "uc_function", "function_name": "other.sales.lookup",
               "scope": {"catalogs": ["main"]}}]
    generate_project(AgentConfig(name="denied", tools=denied), tmp_path)
    with pytest.raises(ValueError, match="over-scoped"):
        _exec_generated_project(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="over-scoped"):
        load_config_tools(denied)


def test_generated_tools_import_only_public_apx_agent_names(tmp_path: Path) -> None:
    """Generated tools/ is user-owned source; it must not reach into private modules."""
    import apx_agent
    from apx_agent._tool_config import _registry

    assert {f.__name__ for f in _registry().values()} <= set(apx_agent.__all__)
    (tmp_path / "guide.md").write_text("# Guide\n")
    out = tmp_path / "out"
    generate_project(AgentConfig(name="public", tools=[{"type": "genie", "space_id": "s"}],
        skills=[SkillConfig(name="guide", description="Guide", path="guide.md")]), out,
        source_dir=tmp_path)
    source = (out / "tools" / "declared.py").read_text()
    assert "from apx_agent import genie_tool" in source
    assert "from apx_agent import skill_tool" in source
    assert "apx_agent._" not in source

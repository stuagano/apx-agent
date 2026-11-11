from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ctk import Artifact, verify

from apx_agent._agent_toml import load_agent_toml, write_agent_toml
from apx_agent._agents import LlmAgent, SequentialAgent
from apx_agent._apps_authorization import AuthorizationPlan
from apx_agent._durable_agent import build_native_manifest
from apx_agent._models import AgentConfig


def _plan(scopes: tuple[str, ...]) -> AuthorizationPlan:
    return AuthorizationPlan(operations=(), user_resources=(), service_resources=(),
                             user_api_scopes=scopes, app_dependencies=())


def test_migrate_round_trips_through_the_sdk(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    path = write_agent_toml(tmp_path, config, _plan(("sql",)), LlmAgent())
    verify(Artifact(str(path), min_bytes=60, must_contain="[memory_store]"))
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert project.memory_store == "mem"
    assert project.user_auth.additional_api_scopes == ("sql",)


def test_second_run_refuses_and_leaves_file_untouched(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({"name": "p", "model": "m", "target": "durable_agent_server"})
    path = write_agent_toml(tmp_path, config, _plan(()), LlmAgent())
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_agent_toml(tmp_path, config, _plan(("sql",)), LlmAgent())
    assert path.read_bytes() == before


def _tables(text: str) -> dict:
    doc = tomllib.loads(text)
    return {k: doc.get(k) for k in ("auth", "memory_store", "session_store")}


@pytest.mark.parametrize("composite", [False, True], ids=["llm", "composite"])
def test_migrated_stores_and_auth_match_the_generated_manifest(tmp_path: Path, composite: bool) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    agent = SequentialAgent([LlmAgent()], name="p") if composite else LlmAgent()
    plan = _plan(("sql", "ai-gateway"))
    generated = build_native_manifest(config=config, authorization_plan=plan, agent=agent)
    migrated = write_agent_toml(tmp_path, config, plan, agent)
    assert _tables(migrated.read_text()) == _tables(generated)
    assert (_tables(generated)["session_store"] is None) is composite


def test_staging_a_real_example_ships_the_authored_file(tmp_path: Path) -> None:
    import shutil

    from apx_agent._agent_toml import stage_agent_toml

    src = Path(__file__).parents[1] / "examples" / "data-triage-agent"
    project = tmp_path / "p"
    shutil.copytree(src, project, ignore=shutil.ignore_patterns(".venv", ".build", "__pycache__"))
    authored = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n\n[session_store]\nname = "s"\n'
    (project / "agent.toml").write_text(authored)
    (project / ".build").mkdir()
    staged = stage_agent_toml(project, project / ".build", print)
    verify(Artifact(str(staged), min_bytes=40, must_contain="[session_store]"))
    assert staged.read_text() == authored


# ADR 0001 decision 5: [[tools]] auth is authored in agent.toml and the platform
# enforces it; apx reads it back, never writes it, and keeps ToolScope for Python tools.
_AUTHORED_TOOLS = '''schema_version = 1

[agent]
framework = "langgraph"
server = "agentbricks"

[[tools]]
id = "lookup"
auth = "user"
source = { kind = "uc_function", function = "main.s.lookup" }

[[tools]]
id = "search"
auth = "app"
source = { kind = "mcp", service = "main.s.search" }
'''


def test_authored_tool_auth_survives_an_sdk_load(tmp_path: Path) -> None:
    path = tmp_path / "agent.toml"
    path.write_text(_AUTHORED_TOOLS)
    verify(Artifact(str(path), min_bytes=100, must_contain='auth = "user"'))
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert {t.id: t.auth for t in project.tools} == {"lookup": "user", "search": "app"}


def test_tool_scope_on_a_platform_tool_is_refused() -> None:
    from apx_agent._platform_tools import platform_tools
    from apx_agent._tool_scope import ToolScope, attach_scope

    with pytest.raises(ValueError, match="agent.toml downscope"):
        attach_scope(platform_tools("lookup")[0], ToolScope(catalogs=("main",)))


# The upstream agent.toml schema's top-level keys (databricks-agentbricks 0.9.0).
_UPSTREAM_KEYS = {"schema_version", "agent", "auth", "memory_store", "session_store", "tools"}



def test_generated_manifest_has_no_apx_only_keys(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    generated = build_native_manifest(config=config, authorization_plan=_plan(("sql",)), agent=LlmAgent())
    path = tmp_path / "agent.toml"
    path.write_text(generated)
    verify(Artifact(str(path), min_bytes=100, must_contain="[memory_store]"))
    doc = tomllib.loads(generated)
    assert set(doc) <= _UPSTREAM_KEYS
    assert "tools" not in doc  # apx never writes [[tools]]
    assert not [k for table in doc.values() if isinstance(table, dict) for k in table if "apx" in k.lower()]
    assert load_agent_toml(tmp_path) is not None

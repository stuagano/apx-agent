from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent import Agent, platform_tools
from apx_agent._agent_toml import check_agent_toml, load_agent_toml
from apx_agent._apps_authorization import compile_authorization_plan
from apx_agent._doctor import Check, Status
from apx_agent._models import AgentConfig

BARE = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
# compile_authorization_plan(model=...) always asks for the ai-gateway user scope.
BASE = BARE + '\n[auth.user]\nrequired = true\nadditional_api_scopes = ["ai-gateway"]\n'
GENIE = '\n[[tools]]\nid = "pricing"\nauth = "user"\n[tools.source]\nkind = "genie_agent"\nspace_id = "01ef0000000000000000000000000000"\n'


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "agent.toml").write_text(body)
    return tmp_path


def _noop(q: str) -> str:
    """Echo."""
    return q


def _findings(root: Path, agent: Agent, config: AgentConfig | None = None) -> list[Check]:
    plan = compile_authorization_plan(agent, model="m")
    return [c for c in check_agent_toml(root, agent, plan, config) if c.status is not Status.OK]


def _config(field: str, store_name: str) -> AgentConfig:
    # AgentConfig derives a managed session store by default; pin it off so only `field` is under test.
    raw: dict = {"name": "a", field: {"type": "managed", "store_name": store_name}}
    if field == "memory":
        raw |= {"target": "responses_agent", "session": {"type": "inmemory"}}
    else:
        raw["target"] = "durable_agent_server"
    return AgentConfig.model_validate(raw)


def test_absent_file_is_none_and_never_walks_up(tmp_path: Path) -> None:
    (tmp_path / "agent.toml").write_text(BASE)
    child = tmp_path / "project"
    child.mkdir()
    assert load_agent_toml(child) is None


def test_clean_project_has_no_findings(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    assert _findings(root, Agent(name="a", tools=[_noop, *platform_tools("pricing")])) == []


def test_unknown_platform_id_fails_with_fix(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[*platform_tools("pricing", "nope")]))
    assert finding.status is Status.FAIL
    assert "nope" in finding.detail and finding.fix is not None and "agent.toml" in finding.fix


def test_unreferenced_declared_tool_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert "pricing" in finding.detail and finding.fix is not None and "platform_tools" in finding.fix


def test_name_collision_with_python_tool_fails(tmp_path: Path) -> None:
    def pricing_ask(q: str) -> str:
        """Clashes with the Genie tool name."""
        return q

    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[pricing_ask, *platform_tools("pricing")]))
    assert "pricing_ask" in finding.detail


def test_missing_user_scope_fails(tmp_path: Path) -> None:
    from apx_agent import Dependencies

    def whoami(ws: Dependencies.UserClient) -> str:
        """Needs a user scope."""
        return ws.current_user.me().user_name

    root = _project(tmp_path, BARE)
    findings = _findings(root, Agent(name="a", tools=[whoami]))
    assert any(f.fix is not None and "additional_api_scopes" in f.fix for f in findings)


def test_wrong_framework_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE.replace("langgraph", "openai"))
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert finding.fix is not None and "langgraph" in finding.fix


def test_unparseable_file_is_one_fail_not_a_traceback(tmp_path: Path) -> None:
    root = _project(tmp_path, "schema_version = 99\n")
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert finding.status is Status.FAIL and finding.name == "agent.toml"


@pytest.mark.parametrize("runtime_only, expected", [(True, 0), (False, 1)])
def test_runtime_only_skips_deploy_level_checks(tmp_path: Path, runtime_only: bool, expected: int) -> None:
    root = _project(tmp_path, BASE + GENIE)
    agent = Agent(name="a", tools=[_noop])  # unreferenced tool: deploy-level, not runtime-breaking
    plan = compile_authorization_plan(agent, model="m")
    fails = [c for c in check_agent_toml(root, agent, plan, None, runtime_only=runtime_only)
             if c.status is Status.FAIL]
    assert len(fails) == expected


@pytest.mark.parametrize("field, command", [("memory", "agentbricks memory bind"),
                                            ("session", "agentbricks sessions bind")])
def test_managed_store_without_binding_fails(tmp_path: Path, field: str, command: str) -> None:
    config = _config(field, "aaa")
    [finding] = _findings(_project(tmp_path, BASE), Agent(name="a", tools=[_noop]), config)
    assert finding.fix is not None and command in finding.fix


@pytest.mark.parametrize("field, table", [("memory", "memory_store"), ("session", "session_store")])
def test_managed_store_name_mismatch_fails_and_match_passes(tmp_path: Path, field: str, table: str) -> None:
    root = _project(tmp_path, BASE + f'\n[{table}]\nname = "bbb"\n')
    agent = Agent(name="a", tools=[_noop])
    bad = _config(field, "aaa")
    [finding] = _findings(root, agent, bad)
    assert "mismatch" in finding.detail
    good = _config(field, "bbb")
    assert _findings(root, agent, good) == []

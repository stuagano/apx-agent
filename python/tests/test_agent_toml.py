from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent import Agent, SequentialAgent, platform_tools
from apx_agent._agent_toml import check_agent_toml, load_agent_toml, stage_agent_toml
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


GENERATED = "# Generated by APX from agent declarations; do not edit.\nschema_version = 1\n"


def test_authored_file_staged_byte_for_byte_replacing_stale_generated(tmp_path: Path) -> None:
    (tmp_path / "agent.toml").write_text(BASE + GENIE)
    build = tmp_path / ".build"
    build.mkdir()
    (build / "agent.toml").write_text(GENERATED)  # stale output from an older deploy
    logs: list[str] = []
    staged = stage_agent_toml(tmp_path, build, lambda: GENERATED, logs.append)
    assert staged.read_bytes() == (tmp_path / "agent.toml").read_bytes()
    assert not any("deprecated" in line for line in logs)


def test_no_authored_file_generates_and_warns(tmp_path: Path) -> None:
    build = tmp_path / ".build"
    build.mkdir()
    logs: list[str] = []
    staged = stage_agent_toml(tmp_path, build, lambda: GENERATED, logs.append)
    assert staged.read_text() == GENERATED
    assert any("migrate-agent-toml" in line for line in logs)


def test_symlinked_manifest_refused(tmp_path: Path) -> None:
    (tmp_path / "real.toml").write_text(BASE)
    (tmp_path / "agent.toml").symlink_to(tmp_path / "real.toml")
    (tmp_path / ".build").mkdir()
    with pytest.raises(ValueError, match="symlink"):
        stage_agent_toml(tmp_path, tmp_path / ".build", lambda: GENERATED, print)


def test_doctor_project_group_reports_agent_toml(tmp_path: Path, monkeypatch) -> None:
    from apx_agent._doctor import run_checks

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.agent]\nname = "p"\nmodel = "m"\ntarget = "durable_agent_server"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent, platform_tools\nagent = Agent(name='p', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    monkeypatch.chdir(tmp_path)
    groups = dict(run_checks(tmp_path, online=False))
    rows = [c for c in groups["Project"] if c.name == "agent.toml"]
    assert rows and rows[0].status is Status.FAIL and "nope" in rows[0].detail


def test_doctor_durable_reports_agent_toml_without_hiding_stages(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.agent]\nname = "p"\nmodel = "m"\nmodule = "agent:agent"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent, platform_tools\nagent = Agent(name='p', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    checks = check_durable_readiness(tmp_path)
    toml_rows = [c for c in checks if c.name == "agent.toml"]
    assert toml_rows and toml_rows[0].status is Status.FAIL and "nope" in toml_rows[0].detail
    startup = next(c for c in checks if c.name == "Startup")
    assert startup.status is not Status.SKIP, startup.detail


def test_composite_durable_project_without_session_store_has_no_session_finding(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({"name": "a", "target": "durable_agent_server"})
    agent = SequentialAgent([Agent(name="a", tools=[_noop])], name="seq")
    assert _findings(_project(tmp_path, BASE), agent, config) == []


# --- C1: migrate → check is clean; scopes are checked only when user auth is required ----
from apx_agent import Dependencies  # noqa: E402
from apx_agent._agent_toml import write_agent_toml  # noqa: E402


def _whoami(ws: Dependencies.UserClient) -> str:
    """Needs the request user."""
    return ws.current_user.me().user_name


def _durable(**extra: bool) -> AgentConfig:
    return AgentConfig.model_validate({"name": "a", "model": "m", "target": "durable_agent_server", **extra})


def _all_checks(root: Path, agent: Agent, config: AgentConfig | None) -> list[Check]:
    return check_agent_toml(root, agent, compile_authorization_plan(agent, model="m"), config)


@pytest.mark.parametrize("tools, extra", [
    ([_noop], {}),
    ([_whoami], {}),
    ([_noop], {"recovery": True}),
], ids=["plain", "user-client", "recovery"])
def test_migrated_agent_toml_checks_clean(tmp_path: Path, tools: list, extra: dict) -> None:
    agent = Agent(name="a", tools=tools)
    config = _durable(**extra)
    write_agent_toml(tmp_path, config, compile_authorization_plan(agent, model="m"), agent)
    assert _all_checks(tmp_path, agent, config) == [
        Check("agent.toml", Status.OK, "agent.toml matches the Python agent")]


def test_user_auth_platform_tool_needs_required_and_scopes(tmp_path: Path) -> None:
    """A referenced auth="user" agent.toml tool makes the request user required (I2)."""
    agent = Agent(name="a", tools=[*platform_tools("pricing")])
    assert [c.fix for c in _findings(_project(tmp_path, BARE + GENIE), agent)] == [
        "Set [auth.user] required = true in agent.toml.",
        "Add ['ai-gateway'] to [auth.user] additional_api_scopes in agent.toml."]
    assert _findings(_project(tmp_path, BASE + GENIE), agent) == []


def test_required_false_with_user_client_tool_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, BARE + '\n[auth.user]\nrequired = false\n')
    fixes = [c.fix for c in _findings(root, Agent(name="a", tools=[_whoami]))]
    assert "Set [auth.user] required = true in agent.toml." in fixes


def test_plain_agent_needs_no_declared_scopes(tmp_path: Path) -> None:
    assert _findings(_project(tmp_path, BARE), Agent(name="a", tools=[_noop])) == []


def test_missing_agent_toml_is_the_one_warning(tmp_path: Path) -> None:
    assert _all_checks(tmp_path, Agent(name="a", tools=[_noop]), None) == [
        Check("agent.toml", Status.WARN, "no agent.toml; deploy generates one (deprecated)",
              "Run `apx-agent agents migrate-agent-toml` to write an authored agent.toml.")]

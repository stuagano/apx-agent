from __future__ import annotations

from collections.abc import Iterator
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
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent, platform_tools\n"
        "agent = Agent(name='p', target='durable_agent_server', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    monkeypatch.chdir(tmp_path)
    groups = dict(run_checks(tmp_path, online=False))
    rows = [c for c in groups["Project"] if c.name == "agent.toml"]
    assert rows and rows[0].status is Status.FAIL and "nope" in rows[0].detail


def test_doctor_durable_reports_agent_toml_without_hiding_stages(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n')
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


# --- C2: both deploy backends check agent.toml before staging -------------------------
from typing import Any  # noqa: E402

import click  # noqa: E402


@pytest.mark.parametrize("deploy", ["", 'backend = "apps_api"\nentrypoint = "app"\n'],
                         ids=["agentbricks-cli", "apps-api"])
def test_mismatched_agent_toml_stops_deploy_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deploy: str,
) -> None:
    from apx_agent import cli

    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n{deploy}')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent\nagent = Agent(name='p', target='durable_agent_server', tools=[])\n")
    (tmp_path / "agent.toml").write_text(BARE.replace("langgraph", "openai"))

    def reached(*a: Any, **k: Any) -> Any:
        raise AssertionError("staging/deploy reached with a mismatched agent.toml")

    monkeypatch.setattr("apx_agent._agent_toml.stage_agent_toml", reached)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.stage_native_source", reached)
    monkeypatch.setattr("apx_agent._apps_api_deploy.deploy_apps_api", reached)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: None)
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(cli._doctor_mod, "check_agent_image", lambda *a, **k: None)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.validate_native_app", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="p", tools=[_noop]))
    with pytest.raises(click.ClickException, match=r"agent\.toml: framework = openai"):
        cli._deploy_apps_impl(cwd=tmp_path, module="agent:agent", profile=None, bundle_target="dev",
                              no_run=True, auto_update_yml=False, auto_build_wheel=False,
                              json_output=False, log=lambda message: None)


# --- I1 / I5: doctor and migrate load the agent like deploy; never crash ---------------
_DURABLE_PYPROJECT = '[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n'
_DURABLE_AGENT = "from apx_agent import Agent\nagent = Agent(name='p', target='durable_agent_server', tools=[])\n"


@pytest.fixture(autouse=True)
def _fresh_agent_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test project imports its own agent module, not one cached by an earlier test,
    and leaves neither its module nor its project dir on sys.path for later tests
    (a leaked tmp dir made doctor import another test's broken agent.py)."""
    import sys

    monkeypatch.setattr(sys, "path", list(sys.path))  # restored at teardown
    for name in ("agent", "app", "bot"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    for name in ("agent", "app", "bot"):
        sys.modules.pop(name, None)


def _agent_toml_doctor_rows(root: Path) -> list[Check]:
    from apx_agent._doctor import run_checks

    groups = dict(run_checks(root, online=False))
    assert {"Project"} <= groups.keys()
    return [c for c in groups["Project"] if c.name == "agent.toml"]


def test_doctor_without_agentbricks_extra_skips_agent_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    (tmp_path / "pyproject.toml").write_text(_DURABLE_PYPROJECT)
    (tmp_path / "agent.py").write_text(_DURABLE_AGENT)
    (tmp_path / "agent.toml").write_text(BARE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "databricks_agentbricks", None)
    monkeypatch.setitem(sys.modules, "databricks_agentbricks.errors", None)
    [row] = _agent_toml_doctor_rows(tmp_path)
    assert row.status is Status.SKIP and "agentbricks" in row.detail and "databricks_agentbricks" in row.detail


def test_doctor_agent_toml_value_error_is_one_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(_DURABLE_PYPROJECT)
    (tmp_path / "agent.py").write_text(_DURABLE_AGENT)
    monkeypatch.chdir(tmp_path)

    def bad_plan(*a: Any, **k: Any) -> Any:
        raise ValueError("bad plan")

    monkeypatch.setattr("apx_agent._apps_authorization.compile_authorization_plan", bad_plan)
    assert _agent_toml_doctor_rows(tmp_path) == [Check("agent.toml", Status.FAIL, "bad plan")]


def test_doctor_loads_a_non_default_agent_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(_DURABLE_PYPROJECT)
    (tmp_path / "app.py").write_text(
        "from apx_agent import Agent, platform_tools\n"
        "agent = Agent(name='p', target='durable_agent_server', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    monkeypatch.chdir(tmp_path)
    rows = _agent_toml_doctor_rows(tmp_path)
    assert any(r.status is Status.FAIL and "nope" in r.detail for r in rows), rows


def test_migrate_uses_the_deploy_loader_and_plan_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent import _apps_authorization
    from apx_agent.cli import main

    (tmp_path / "pyproject.toml").write_text(_DURABLE_PYPROJECT)
    (tmp_path / "bot.py").write_text(
        "from apx_agent import Agent\nassistant = Agent(name='p', target='durable_agent_server', tools=[])\n")
    monkeypatch.chdir(tmp_path)
    seen: list[dict[str, Any]] = []
    real = _apps_authorization.compile_authorization_plan

    def spy(agent: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(agent, **kwargs)

    monkeypatch.setattr(_apps_authorization, "compile_authorization_plan", spy)
    result = CliRunner().invoke(main, ["agents", "migrate-agent-toml", "--module", "bot:assistant"])
    assert result.exit_code == 0, result.output
    assert load_agent_toml(tmp_path) is not None
    assert seen and set(seen[0]) == {"model", "agent_identity"}


def test_migrate_load_failure_is_a_click_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent.cli import main

    (tmp_path / "pyproject.toml").write_text(_DURABLE_PYPROJECT)
    (tmp_path / "agent.py").write_text("import not_a_real_module_xyz\nagent = Agent(target='durable_agent_server')\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["agents", "migrate-agent-toml"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.exception
    assert "not_a_real_module_xyz" in result.output and not (tmp_path / "agent.toml").exists()


def test_generate_refuses_an_authored_file_in_the_staged_source(tmp_path: Path) -> None:
    build = tmp_path / ".build"
    build.mkdir()
    (build / "agent.toml").write_text(BASE)  # authored, not APX-generated
    with pytest.raises(ValueError, match="Refusing to overwrite an authored agent.toml"):
        stage_agent_toml(tmp_path, build, lambda: GENERATED, print)
    assert (build / "agent.toml").read_text() == BASE


def test_generate_replaces_a_stale_generated_file(tmp_path: Path) -> None:
    build = tmp_path / ".build"
    build.mkdir()
    (build / "agent.toml").write_text(GENERATED + "# stale\n")
    assert stage_agent_toml(tmp_path, build, lambda: GENERATED, print).read_text() == GENERATED


def test_python_only_managed_memory_needs_a_bound_store(tmp_path: Path) -> None:
    """#954: with no AgentConfig, memory declared on the agent is still checked."""
    agent = Agent(name="a", tools=[_noop], memory="managed")
    [finding] = _findings(_project(tmp_path, BASE), agent)
    assert "managed memory declared but no memory_store bound" in finding.detail
    store = agent.memory_config.store_name
    assert _findings(_project(tmp_path, BASE + f'\n[memory_store]\nname = "{store}"\n'), agent) == []


def test_python_only_memory_needs_a_request_user(tmp_path: Path) -> None:
    """#954: Python-only memory forces [auth.user] required, like config memory."""
    agent = Agent(name="a", tools=[_noop], memory="managed")
    body = BARE + '\n[auth.user]\nrequired = false\nadditional_api_scopes = []\n'
    body += f'\n[memory_store]\nname = "{agent.memory_config.store_name}"\n'
    details = [f.detail for f in _findings(_project(tmp_path, body), agent)]
    assert any("need a request user" in d for d in details), details


def test_native_manifest_binds_python_only_memory() -> None:
    """#954: generated agent.toml sees Agent(memory=) even when AgentConfig has none."""
    import tomllib

    from apx_agent._durable_agent import build_native_manifest

    agent = Agent(name="a", tools=[_noop], memory="managed")
    config = AgentConfig(name="a", target="durable_agent_server")
    assert config.memory is None
    plan = compile_authorization_plan(agent, model=config.model)
    manifest = tomllib.loads(build_native_manifest(config=config, authorization_plan=plan, agent=agent))
    assert manifest["auth"]["user"]["required"] is True
    assert manifest["memory_store"]["name"] == agent.memory_config.store_name


def test_mismatched_agent_toml_stops_deploy_without_agent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#954: require_agent_toml_ok still runs when pyproject has no apx settings."""
    from apx_agent import cli

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.toml").write_text(BARE.replace("langgraph", "openai"))
    (tmp_path / "databricks.yml").write_text(
        "bundle:\n  name: p\nresources:\n  apps:\n    p:\n      name: p\n")
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("# DurableAgentServer\n")

    def reached(*a: Any, **k: Any) -> Any:
        raise AssertionError("staging/deploy reached with a mismatched agent.toml")

    monkeypatch.setattr("apx_agent._agent_toml.stage_agent_toml", reached)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.stage_native_source", reached)
    monkeypatch.setattr("apx_agent._apps_api_deploy.deploy_apps_api", reached)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_validate_responses_agent_compiler", lambda: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: None)
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(cli._doctor_mod, "check_agent_image", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="p", tools=[_noop]))
    with pytest.raises(click.ClickException, match=r"agent\.toml: framework = openai"):
        cli._deploy_apps_impl(cwd=tmp_path, module="agent:agent", profile=None, bundle_target="dev",
                              no_run=True, auto_update_yml=False, auto_build_wheel=False,
                              json_output=False, log=lambda message: None)


def test_doctor_python_only_reports_agent_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#954: doctor still checks agent.toml when there is no apx settings."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent, platform_tools\nagent = Agent(name='p', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    monkeypatch.chdir(tmp_path)
    rows = _agent_toml_doctor_rows(tmp_path)
    assert any(r.status is Status.FAIL and "nope" in r.detail for r in rows), rows

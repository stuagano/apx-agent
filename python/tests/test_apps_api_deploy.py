"""Apps API deploy: mocked workspace, seven get-or-create steps, four-row routing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
import yaml
from click.testing import CliRunner

from apx_agent import AgentConfig, KeywordRouter, LlmAgent
from apx_agent._apps_authorization import AuthorizationPlan, compile_authorization_plan
from apx_agent._durable_agent import build_native_manifest
from apx_agent.cli import main

PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[tool.apx.deploy]
name = "{name}"
{deploy}
"""


def _config(**deploy: Any) -> AgentConfig:
    return AgentConfig(
        name="proof", target="durable_agent_server",
        deploy={"backend": "apps_api", "entrypoint": "app", **deploy},
        session={"type": "managed", "store_name": "proof-sessions"},
        memory={"type": "managed", "store_name": "proof-memory"},
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "proof"\n')
    (tmp_path / "agent.py").write_text("agent = None\n")
    (tmp_path / "prompts.py").write_text("PROMPT = 'hi'\n")
    return tmp_path


def test_explicit_deploy_resources_preserve_attachments(harness: dict[str, Any]) -> None:
    resources = [
        {"kind": "experiment", "identifier": "1694681152367532", "permission": "CAN_MANAGE"},
        {"kind": "sql_warehouse", "identifier": "e60471096d7311eb", "permission": "CAN_USE"},
        {"kind": "serving_endpoint", "identifier": "databricks-claude-opus-4-8", "permission": "CAN_QUERY"},
    ]
    config = _config(resources=resources)
    agent = LlmAgent(name="proof")
    before = compile_authorization_plan(agent, model=config.model)
    assert build_native_manifest(config=config, authorization_plan=before, agent=agent) == build_native_manifest(
        config=_config(), authorization_plan=before, agent=agent,
    )
    _deploy(harness, config, agent)
    app = harness["ws"].apps.create_update.call_args.kwargs["app"]
    assert [r.as_dict() for r in app.resources] == [
        {"name": r.name, **{key: body}} for r, key, body in zip(app.resources,
            ["experiment", "sql_warehouse", "serving_endpoint"], [
                {"experiment_id": "1694681152367532", "permission": "CAN_MANAGE"},
                {"id": "e60471096d7311eb", "permission": "CAN_USE"},
                {"name": "databricks-claude-opus-4-8", "permission": "CAN_QUERY"},
            ], strict=True)
    ]
    assert app.user_api_scopes == ["sql"]
    assert compile_authorization_plan(agent, model=config.model) == before


@pytest.mark.parametrize("resource", [
    {"kind": "job", "identifier": "123", "permission": "CAN_MANAGE"},
    {"kind": "experiment", "identifier": "123", "permission": "CAN_QUERY"},
    {"kind": "experiment", "identifier": "${EXPERIMENT}", "permission": "CAN_MANAGE"},
    {"kind": "sql_warehouse", "identifier": "../warehouse", "permission": "CAN_USE"},
    {"kind": "serving_endpoint", "identifier": " endpoint ", "permission": "CAN_QUERY"},
    {"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE", "scope": "all-apis"},
    {"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE", "secret": "secret-ref"},
])
def test_invalid_deploy_resources_are_rejected(resource: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _config(resources=[resource])


def test_deploy_resources_roundtrip_and_placement_guards() -> None:
    import tomllib
    from apx_agent._project_gen import _build_pyproject

    resource = {"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE"}
    config = _config(resources=[resource])
    from apx_agent._inspection import _fold_deploy_table

    loaded = AgentConfig.model_validate(_fold_deploy_table(tomllib.loads(_build_pyproject(config)), {}))
    assert loaded.deploy.resources == config.deploy.resources
    for changes in ({"space": "space"}, {"backend": "agentbricks", "entrypoint": None}):
        with pytest.raises(ValueError, match="dedicated"):
            _config(resources=[resource], **changes)
    with pytest.raises(ValueError, match="Duplicate"):
        _config(resources=[resource, resource])
    with pytest.raises(ValueError, match="durable_agent_server"):
        AgentConfig(name="bad", target="langgraph", deploy=config.deploy)


def test_deploy_resources_reject_derived_collision_before_mutation(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._apps_api_deploy import deployment_resources
    from apx_agent._resources import ResourceSpec

    resource = {"kind": "sql_warehouse", "identifier": "abc123", "permission": "CAN_USE"}
    plan = AuthorizationPlan((), (), (ResourceSpec("sql_warehouse", "abc123"),), (), ())
    with pytest.raises(click.ClickException, match="collides"):
        deployment_resources(_config(resources=[resource]), plan)
    monkeypatch.setattr("apx_agent._resources._bounded_name", lambda *args: "collision")
    resources = [resource, {"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE"}]
    with pytest.raises(click.ClickException, match="collides"):
        _deploy(harness, _config(resources=resources), LlmAgent(name="proof"))
    assert harness["state"]["calls"] == []
    assert not (harness["project"] / ".build").exists()


def test_deploy_resources_combine_with_service_resources() -> None:
    from apx_agent._apps_api_deploy import deployment_resources
    from apx_agent._resources import ResourceSpec

    config = _config(resources=[{"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE"}])
    plan = AuthorizationPlan((), (), (ResourceSpec("sql_warehouse", "abc123"),), (), ())
    result = deployment_resources(config, plan)
    assert result[0]["sql_warehouse"] == {"id": "abc123", "permission": "CAN_USE"}
    assert result[1]["experiment"] == {"experiment_id": "123", "permission": "CAN_MANAGE"}
    assert result == deployment_resources(config, plan)


class _Wait:
    def __init__(self, value: Any) -> None:
        self.value = value

    def result(self) -> Any:
        return self.value


def _app(space: str | None, *, compute: str = "ACTIVE") -> SimpleNamespace:
    return SimpleNamespace(
        name="proof", space=space, description="proof", service_principal_client_id="sp-1",
        compute_status=SimpleNamespace(state=compute),
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service.workspace import ObjectType

    project = _project(tmp_path)
    state: dict[str, Any] = {"app": None, "memory": False, "session": False, "calls": []}
    ws = MagicMock()
    ws.workspace.get_status.return_value = SimpleNamespace(object_type=ObjectType.FILE)

    def get_app(name: str) -> Any:
        state["calls"].append("apps.get")
        if state["app"] is None:
            raise NotFound(name)
        return state["app"]

    def create_app(app: Any, **kwargs: Any) -> _Wait:
        state["calls"].append(("apps.create", kwargs, app.space))
        state["app"] = _app(app.space)
        return _Wait(state["app"])

    def update_app(*args: Any, **kwargs: Any) -> _Wait:
        state["calls"].append(("apps.update", args, kwargs))
        return _Wait(SimpleNamespace())

    ws.apps.get.side_effect = get_app
    ws.apps.create.side_effect = create_app
    ws.apps.create_update.side_effect = update_app
    ws.apps.get_space.return_value = SimpleNamespace(name="app-space")
    ws.current_user.me.return_value = SimpleNamespace(user_name="me@example.com")
    ws.api_client.do.return_value = {
        "space": "app-space", "compute_size": "LIQUID", "service_principal_client_id": "sp-1",
    }
    bricks = MagicMock()
    bricks.workspace_client = ws

    def get_session(name: str) -> None:
        state["calls"].append(("session.get", name))
        if not state["session"]:
            raise NotFound(name)

    bricks.get_session_store.side_effect = get_session
    bricks.create_session_store.side_effect = lambda name, **k: state["calls"].append(("session.create", name)) or state.update(session=True)
    bricks.grant_session_store_permission.side_effect = lambda name, sp: state["calls"].append(("session.grant", name, sp))
    monkeypatch.setattr("databricks_agentkit._api_client._AgentBricksApiClient", lambda *a, **k: bricks)
    monkeypatch.setattr(
        "databricks_agentbricks.clients.managed_runtime_store.get_or_create_backend",
        lambda *a, **k: state["calls"].append("runtime") or SimpleNamespace(branch="b", database_id="db", username="sp-1"),
    )
    monkeypatch.setattr("apx_agent.cli._ensure_experiment_id", lambda *a, **k: "exp-1")
    monkeypatch.setattr("apx_agent.cli._grant_experiment_to_sp", lambda *a, **k: state["calls"].append("grant") or True)
    monkeypatch.setattr("apx_agent.cli._grant_trace_uc_tables_to_sp", lambda *a, **k: True)
    monkeypatch.setattr("apx_agent.cli._ensure_apx_wheel", lambda cwd: None)
    monkeypatch.setattr("apx_agent.cli._stage_build_manifest", lambda build, wheel: (build / "pyproject.toml").write_text("ok\n"))
    monkeypatch.setattr("apx_agent.cli._poll_app_ready", lambda *a, **k: state["calls"].append("poll") or {"url": "https://proof"})
    monkeypatch.setattr("apx_agent.cli._check_readyz", lambda *a, **k: state["calls"].append("readyz") or (True, {"durable": True}))
    monkeypatch.setattr("apx_agent.cli._check_native_execution", lambda *a, **k: state["calls"].append("smoke") or SimpleNamespace(completed=True, detail="ok"))
    monkeypatch.setattr("apx_agent.cli._register_apps_manifest_step", lambda **k: "1")
    monkeypatch.setattr("apx_agent.cli._maybe_write_deploy_state", lambda **k: True)
    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", lambda args, profile=None: state["calls"].append(tuple(args)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(
        "apx_agent._memory_managed.provision_managed_memory",
        lambda ws, name, **k: state["calls"].append("memory.create" if not state["memory"] else "memory.get")
        or state.update(memory=True) or f"memory {name}",
    )
    monkeypatch.setattr("apx_agent._app_space.validate_space_deployment", lambda *a, **k: state["calls"].append("space-check"))
    return {"project": project, "ws": ws, "state": state}


def _deploy(box: dict[str, Any], config: AgentConfig, agent: Any, *, profile: str | None = "chosen", smoke_input: Path | None = None,
            registry_tables: Any = None, register_uc: bool = False, json_output: bool = False) -> None:
    from apx_agent._apps_api_deploy import deploy_apps_api

    deploy_apps_api(
        cwd=box["project"], agent=agent, config=config, plan=AuthorizationPlan((), (), (), ("sql",), ()),
        family_permissions=SimpleNamespace(can_use_groups=(), can_manage_groups=()), workspace=box["ws"],
        profile=profile, bundle_target="dev", app_name="proof", auto_experiment=True, auto_build_wheel=False,
        readyz_gate=True, register_uc=register_uc, uc_name=None, module="agent:agent", no_run=False, vars=(),
        env_pairs=(), secret_env_pairs=(), app_name_override=None, json_output=json_output, extra_version_tags={},
        poll_timeout_seconds=1, readyz_attempts=1, pin=SimpleNamespace(), log=lambda message: None,
        smoke_input=smoke_input, registry_tables=registry_tables,
    )


def test_apps_api_validates_and_forwards_typed_smoke(harness, monkeypatch, tmp_path):
    from tests.test_native_deploy_smoke import Domain

    agent = LlmAgent(name="proof", input_schema=Domain, input_key="domain", output_schema=Domain)
    with pytest.raises(click.ClickException, match="--smoke-input"):
        _deploy(harness, _config(), agent)
    assert harness["state"]["calls"] == []
    assert not (harness["project"] / ".build").exists()
    path = tmp_path / "smoke.json"
    path.write_text('{"value": 1}')
    captured = {}
    def smoke(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completed=True, detail="ok")
    monkeypatch.setattr("apx_agent.cli._check_native_execution", smoke)
    _deploy(harness, _config(), agent, smoke_input=path)
    assert captured["smoke_input"]["apx_data_parts"][0]["data"] == {"value": 1}
    assert captured["smoke_agent"] is agent
    assert not (harness["project"] / ".build" / "smoke.json").exists()


def test_deploy_resources_still_require_explicit_profile(harness: dict[str, Any]) -> None:
    config = _config(resources=[{"kind": "experiment", "identifier": "123", "permission": "CAN_MANAGE"}])
    with pytest.raises(click.ClickException, match="explicit --profile"):
        _deploy(harness, config, LlmAgent(name="proof"), profile=None)
    assert harness["state"]["calls"] == []


def test_sync_uploads_the_gitignored_build_tree(harness: dict[str, Any]) -> None:
    """``.build/`` is gitignored and ``databricks sync`` honors .gitignore, so
    without an include override the source folder ends up empty (seen live)."""
    _deploy(harness, _config(), LlmAgent(name="proof"))
    sync = next(call for call in harness["state"]["calls"] if isinstance(call, tuple) and call[0] == "sync")
    assert "--include" in sync and sync[sync.index("--include") + 1] == "**"
    assert sync[sync.index("--exclude") + 1] == "uv.lock"


def test_steps_run_in_order_and_rerun_does_not_recreate(harness: dict[str, Any]) -> None:
    config = _config(space="app-space", include=["prompts.py"], env={"DATA_INSPECTOR_URL": "https://inspector.example"})
    _deploy(harness, config, LlmAgent(name="proof"))
    names = [item if isinstance(item, str) else item[0] for item in harness["state"]["calls"]]
    assert names.index("memory.create") < names.index("apps.create")
    assert names.index("apps.create") < names.index("space-check") < names.index("runtime")
    # #910: a missing session store is created after the app exists, then granted to its principal.
    assert names.index("runtime") < names.index("session.create") < names.index("session.grant") < names.index("sync")
    assert ("session.grant", "proof-sessions", "sp-1") in harness["state"]["calls"]
    assert names.index("runtime") < names.index("sync") < names.index("apps") < names.index("poll")
    assert names.index("poll") < names.index("readyz") < names.index("smoke")
    assert "apps.update" not in names
    sync = next(item for item in harness["state"]["calls"] if isinstance(item, tuple) and item[0] == "sync")
    assert sync[3:] == ("--include", "**", "--exclude", "uv.lock", "--exclude", ".databricks/**")
    app_yaml = yaml.safe_load((harness["project"] / ".build" / "app.yaml").read_text().split("\n", 1)[1])
    # The upload omits uv.lock and Apps installs nothing from a bare pyproject.toml (#956).
    assert app_yaml["command"] == ["uv", "run", "python", "-m", "app"]
    env = {item["name"]: item["value"] for item in app_yaml["env"]}
    assert env["DATA_INSPECTOR_URL"] == "https://inspector.example"
    assert env["AGENT_SESSION_STORE"] == "proof-sessions"
    assert (harness["project"] / ".build" / "prompts.py").is_file()
    created = [item for item in harness["state"]["calls"] if isinstance(item, tuple) and item[0] == "apps.create"]
    assert created[0][1] == {} and created[0][2] == "app-space"
    harness["state"]["calls"].clear()
    _deploy(harness, config, LlmAgent(name="proof"))
    again = [item if isinstance(item, str) else item[0] for item in harness["state"]["calls"]]
    assert "apps.create" not in again and "memory.create" not in again and "session.create" not in again
    assert "session.grant" in again


@pytest.mark.parametrize("missing", ["app.yaml", "agent.toml", "pyproject.toml"])
def test_empty_upload_does_not_start_rollout(harness, missing):
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service.workspace import ObjectType

    def status(path):
        if path.endswith("/" + missing):
            raise NotFound("missing staged manifest")
        return SimpleNamespace(object_type=ObjectType.FILE)
    harness["ws"].workspace.get_status.side_effect = status
    with pytest.raises(click.ClickException, match="source verification failed"):
        _deploy(harness, _config(), LlmAgent(name="proof"))
    assert not any(isinstance(item, tuple) and item[:2] == ("apps", "deploy") for item in harness["state"]["calls"])


def test_rollout_failure_does_not_claim_previous_app_is_active(harness, monkeypatch):
    def run(args, profile=None):
        return SimpleNamespace(returncode=1 if args[:2] == ["apps", "deploy"] else 0, stdout="", stderr="")
    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", run)
    with pytest.raises(click.ClickException) as exc:
        _deploy(harness, _config(), LlmAgent(name="proof"))
    assert "may be unavailable" in str(exc.value)
    assert "previous deployment was left in place" not in str(exc.value)


def test_failed_sync_does_not_claim_atomic_upload_or_start_rollout(harness, monkeypatch):
    commands = []
    def run(args, profile=None):
        commands.append(args)
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", run)
    with pytest.raises(click.ClickException, match="App rollout was not attempted") as exc:
        _deploy(harness, _config(), LlmAgent(name="proof"))
    assert "source was not replaced" not in str(exc.value)
    assert not any(args[:2] == ["apps", "deploy"] for args in commands)
    harness["ws"].workspace.get_status.assert_not_called()


def test_dedicated_update_mask_and_no_liquid_read(harness: dict[str, Any]) -> None:
    _deploy(harness, _config(), LlmAgent(name="proof"))
    update = next(item for item in harness["state"]["calls"] if isinstance(item, tuple) and item[0] == "apps.update")
    assert update[1][1] == "user_api_scopes,resources"
    sent = update[2]["app"]
    assert sent.name == "proof"
    assert sent.user_api_scopes == ["sql"]
    assert sent.resources == []
    assert sent.space is None and sent.description is None
    assert not any(item == "space-check" for item in harness["state"]["calls"])
    # Dedicated still creates the runtime store and does not read the raw app payload.
    assert "runtime" in harness["state"]["calls"]
    harness["ws"].api_client.do.assert_not_called()


def test_space_runs_scope_check_and_liquid_read(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def check(doc: dict[str, Any], **kwargs: Any) -> None:
        seen["doc"] = doc
        seen["space"] = kwargs
        harness["state"]["calls"].append("space-check")

    monkeypatch.setattr("apx_agent._app_space.validate_space_deployment", check)
    _deploy(harness, _config(space="app-space"), LlmAgent(name="proof"))
    assert seen["doc"]["resources"]["apps"]["proof"]["space"] == "app-space"
    assert not any(isinstance(item, tuple) and item[0] == "apps.update" for item in harness["state"]["calls"])
    assert harness["ws"].api_client.do.call_args.args[:2] == ("GET", "/api/2.0/apps/proof")


@pytest.mark.parametrize(("existing_space", "declared"), [
    ("other", "app-space"),
    (None, "app-space"),
    ("app-space", None),
])
def test_placement_mismatch_is_refused(harness: dict[str, Any], existing_space: str | None, declared: str | None) -> None:
    harness["state"]["app"] = _app(existing_space)
    with pytest.raises(click.ClickException, match="Refusing"):
        _deploy(harness, _config(**({} if declared is None else {"space": declared})), LlmAgent(name="proof"))
    assert not any(isinstance(item, tuple) and item[0] in {"apps.create", "apps.update"} for item in harness["state"]["calls"])


def test_stopped_enum_starts_compute_once(harness: dict[str, Any]) -> None:
    from enum import Enum

    from apx_agent._apps_api_deploy import _wait_compute_active

    class ComputeState(Enum):
        STOPPED = "STOPPED"
        ACTIVE = "ACTIVE"

    states = iter((ComputeState.STOPPED, ComputeState.STOPPED, ComputeState.ACTIVE))

    def get_app(name: str) -> SimpleNamespace:
        harness["state"]["calls"].append("apps.get")
        return SimpleNamespace(compute_status=SimpleNamespace(state=next(states)))

    harness["ws"].apps.get.side_effect = get_app
    harness["ws"].apps.start.side_effect = lambda name: harness["state"]["calls"].append(("apps.start", name))
    _wait_compute_active(harness["ws"], "proof", lambda message: None, timeout_seconds=5)
    starts = [item for item in harness["state"]["calls"] if isinstance(item, tuple) and item[0] == "apps.start"]
    assert starts == [("apps.start", "proof")]
    assert harness["state"]["calls"].count("apps.get") == 3


def test_compute_poll_retries_a_transient_get(harness: dict[str, Any]) -> None:
    from apx_agent._apps_api_deploy import _wait_compute_active

    answers = iter((TimeoutError("Timed out after 0:00:01"), _app(None)))

    def get_app(name: str) -> SimpleNamespace:
        harness["state"]["calls"].append("apps.get")
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    harness["ws"].apps.get.side_effect = get_app
    _wait_compute_active(harness["ws"], "proof", lambda message: None, timeout_seconds=5)
    assert harness["state"]["calls"].count("apps.get") == 2
    harness["ws"].apps.start.assert_not_called()


def test_composite_skips_session_even_when_declared(harness: dict[str, Any]) -> None:
    leaf = LlmAgent(name="leaf")
    router = KeywordRouter(branches=[("investigate", leaf, ["missing"])], default=leaf)
    config = _config()
    assert config.session is not None and config.session.type == "managed"
    _deploy(harness, config, router)
    assert not any(isinstance(item, tuple) and item[0] == "session.get" for item in harness["state"]["calls"])
    manifest = (harness["project"] / ".build" / "agent.toml").read_text()
    assert "[session_store]" not in manifest
    plan = compile_authorization_plan(leaf, model=config.model)
    written = build_native_manifest(config=config, authorization_plan=plan, agent=LlmAgent(name="proof"))
    assert "[session_store]" in written


def test_deploy_config_requires_entrypoint_only_for_apps_api() -> None:
    with pytest.raises(ValueError, match="entrypoint"):
        AgentConfig(name="x", target="durable_agent_server", deploy={"backend": "apps_api"})
    with pytest.raises(ValueError, match="apps_api"):
        AgentConfig(name="x", target="durable_agent_server", deploy={"entrypoint": "app"})
    with pytest.raises(ValueError, match="closed|apps_api|agentbricks"):
        AgentConfig(name="x", target="durable_agent_server", deploy={"backend": "bundle"})


def _doctor_project(tmp_path: Path, name: str, *, deploy: str, bundle: str | None, agent: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "pyproject.toml").write_text(PYPROJECT.format(name=name, deploy=deploy))
    (root / "agent.py").write_text(agent)
    if bundle is not None:
        (root / "databricks.yml").write_text(bundle)
    return root


LLM = (
    "from apx_agent import LlmAgent\n"
    "agent = LlmAgent(name='{name}', tools=[], model='databricks-claude-sonnet-4-6', target='durable_agent_server')\n"
)
BUNDLE = """\
bundle:
  name: {app}
resources:
  apps:
    {app}:
      name: {app}
{extra}      source_code_path: ./.build
"""
SPACE = """\
      space: app-space
      config:
        command: [python, -m, app]
        env:
          - name: APX_APPS_HOST
            value: agentbricks
"""


@pytest.mark.parametrize(("deploy", "bundle", "needle"), [
    ("", BUNDLE.format(app="agent-bricks-row1", extra=""), "native deploy preflight"),
    ('space = "app-space"\n', BUNDLE.format(app="mcp-row2", extra=SPACE), "App Space deploy preflight"),
    ('backend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n', None, "Apps API space app-space"),
    ('backend = "apps_api"\nentrypoint = "app"\n', None, "Apps API dedicated"),
])
def test_doctor_routes_all_four_rows(tmp_path: Path, deploy: str, bundle: str | None, needle: str) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = _doctor_project(tmp_path, "row", deploy=deploy, bundle=bundle, agent=LLM.format(name="row"))
    deploy_check = next(check for check in check_durable_readiness(root) if check.name == "Deploy")
    assert deploy_check.status is Status.OK, deploy_check.detail
    assert needle in deploy_check.detail


def test_uv_lock_unsets_frozen_only_for_that_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _stage_build_manifest

    build = tmp_path / ".build"
    build.mkdir()
    wheel = tmp_path / "apx_agent-0.whl"
    wheel.write_bytes(b"wheel")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "proof"\nversion = "0"\ndependencies = []\n'
        '[tool.uv.sources]\napx-agent = { path = "../.." }\n'
    )
    seen: dict[str, str | None] = {}

    def run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        assert cmd == ["uv", "lock"]
        seen["frozen"] = kwargs["env"].get("UV_FROZEN")
        (build / "uv.lock").write_text("version = 1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("UV_FROZEN", "1")
    monkeypatch.setattr("apx_agent.cli.subprocess.run", run)
    monkeypatch.setattr("apx_agent.cli._sanitize_uv_lock", lambda path: False)
    monkeypatch.setattr("apx_agent.cli._warn_unknown_lock_mirrors", lambda path: None)
    _stage_build_manifest(build, wheel)
    assert seen["frozen"] is None
    assert os.environ["UV_FROZEN"] == "1"


@pytest.mark.parametrize(("name", "deploy", "bundle", "backend", "app_name"), [
    ("row1", "", BUNDLE.format(app="agent-bricks-row1", extra=""), None, "agent-bricks-row1"),
    ("row2", 'space = "app-space"\n', BUNDLE.format(app="mcp-row2", extra=SPACE), None, "mcp-row2"),
    ("row3", 'backend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n', None, "apps_api", "row3"),
    ("row4", 'backend = "apps_api"\nentrypoint = "app"\n', None, "apps_api", "row4"),
    # #909: an mcp- name keeps MCP discovery without an App Space.
    ("mcp-row4", 'backend = "apps_api"\nentrypoint = "app"\n', None, "apps_api", "mcp-row4"),
])
def test_agents_deploy_routes_all_four_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, deploy: str, bundle: str | None,
    backend: str | None, app_name: str,
) -> None:
    root = _doctor_project(tmp_path, name, deploy=deploy, bundle=bundle, agent=LLM.format(name=name))
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps", "--dry-run", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["app_name"] == app_name
    if backend == "apps_api":
        assert payload["deployment_backend"] == "apps_api"
        assert "agent-bricks-" not in payload["app_name"]
    else:
        assert payload.get("deployment_backend") != "apps_api"


def test_apps_api_honors_declared_deploy_app_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.apx.deploy] app_name wins over the agent name (the agent name is a tool name)."""
    from apx_agent.cli import _resolve_project_app_name

    root = _doctor_project(
        tmp_path, "inspector", deploy='backend = "apps_api"\nentrypoint = "app"\napp_name = "mcp-inspector"\n',
        bundle=None, agent=LLM.format(name="inspector"),
    )
    assert _resolve_project_app_name(root) == "mcp-inspector"
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps", "--dry-run", "--json-output"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["app_name"] == "mcp-inspector"


def test_apps_api_name_is_not_agent_bricks_prefixed(tmp_path: Path) -> None:
    from apx_agent.cli import _resolve_project_app_name

    root = _doctor_project(
        tmp_path, "named", deploy='backend = "apps_api"\nentrypoint = "app"\n',
        bundle=None, agent=LLM.format(name="named"),
    )
    assert _resolve_project_app_name(root) == "named"


def test_plan_routes_apps_api_without_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _doctor_project(
        tmp_path, "planned",
        deploy='backend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n',
        bundle=None, agent=LLM.format(name="planned"),
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps", "--dry-run", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.stdout) if result.stdout.startswith("target") else __import__("json").loads(result.stdout)
    assert payload["deployment_backend"] == "apps_api"
    assert payload["app_name"] == "planned"
    assert "agent-bricks-" not in payload["app_name"]


def test_apps_api_records_registry_after_ledger_and_reports_json(harness, monkeypatch, capsys):
    from apx_agent._registry import RegistryTables

    steps: list[Any] = []
    monkeypatch.setattr("apx_agent.cli._register_apps_manifest_step", lambda **k: steps.append("ledger") or "1")
    monkeypatch.setattr("apx_agent.cli._deploy_registry_step",
                        lambda **k: steps.append(("registry", k["app_name"], k["tables"].agents)))
    tables = RegistryTables("c.apx.agent_registry", "c.apx.agent_tools")
    _deploy(harness, _config(), LlmAgent(name="proof"), registry_tables=tables, register_uc=True, json_output=True)
    assert steps == ["ledger", ("registry", "proof", "c.apx.agent_registry")]
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["registry_recorded"] is True and doc["registry_table"] == "c.apx.agent_registry"


def test_apps_api_without_registry_reports_not_recorded(harness, capsys):
    _deploy(harness, _config(), LlmAgent(name="proof"), registry_tables=None, json_output=True)
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["registry_recorded"] is False and doc["registry_table"] is None

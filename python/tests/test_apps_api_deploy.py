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

[tool.apx.agent]
name = "{name}"
model = "databricks-claude-sonnet-4-6"
module = "agent:agent"
target = "durable_agent_server"
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

    project = _project(tmp_path)
    state: dict[str, Any] = {"app": None, "memory": False, "calls": []}
    ws = MagicMock()

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
    kit = MagicMock()
    kit.session_stores.get.side_effect = lambda name: state["calls"].append(("session.get", name))
    kit.session_stores.create.side_effect = AssertionError("session create is not part of this path")
    bricks = MagicMock()
    bricks.workspace_client = ws
    monkeypatch.setattr("databricks_agentkit.AgentKitClient", lambda **k: kit)
    monkeypatch.setattr("databricks_agentkit._api_client._AgentBricksApiClient", lambda *a, **k: bricks)
    monkeypatch.setattr(
        "databricks_agentbricks.lakebase_runtime_store.get_or_create_backend",
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
    return {"project": project, "ws": ws, "state": state, "kit": kit}


def _deploy(box: dict[str, Any], config: AgentConfig, agent: Any) -> None:
    from apx_agent._apps_api_deploy import deploy_apps_api

    deploy_apps_api(
        cwd=box["project"], agent=agent, config=config, plan=AuthorizationPlan((), (), (), ("sql",), ()),
        family_permissions=SimpleNamespace(can_use_groups=(), can_manage_groups=()), workspace=box["ws"],
        profile="chosen", bundle_target="dev", app_name="proof", auto_experiment=True, auto_build_wheel=False,
        readyz_gate=True, register_uc=False, uc_name=None, module="agent:agent", no_run=False, vars=(),
        env_pairs=(), secret_env_pairs=(), app_name_override=None, json_output=False, extra_version_tags={},
        poll_timeout_seconds=1, readyz_attempts=1, pin=SimpleNamespace(), log=lambda message: None,
    )


def test_steps_run_in_order_and_rerun_does_not_recreate(harness: dict[str, Any]) -> None:
    config = _config(space="app-space", include=["prompts.py"], env={"DATA_INSPECTOR_URL": "https://inspector.example"})
    _deploy(harness, config, LlmAgent(name="proof"))
    names = [item if isinstance(item, str) else item[0] for item in harness["state"]["calls"]]
    assert names.index("memory.create") < names.index("session.get") < names.index("apps.create")
    assert names.index("apps.create") < names.index("space-check") < names.index("runtime")
    assert names.index("runtime") < names.index("sync") < names.index("apps") < names.index("poll")
    assert names.index("poll") < names.index("readyz") < names.index("smoke")
    assert "apps.update" not in names
    app_yaml = yaml.safe_load((harness["project"] / ".build" / "app.yaml").read_text().split("\n", 1)[1])
    assert app_yaml["command"] == ["python", "-m", "app"]
    env = {item["name"]: item["value"] for item in app_yaml["env"]}
    assert env["DATA_INSPECTOR_URL"] == "https://inspector.example"
    assert env["AGENT_SESSION_STORE"] == "proof-sessions"
    assert (harness["project"] / ".build" / "prompts.py").is_file()
    created = [item for item in harness["state"]["calls"] if isinstance(item, tuple) and item[0] == "apps.create"]
    assert created[0][1] == {} and created[0][2] == "app-space"
    harness["state"]["calls"].clear()
    _deploy(harness, config, LlmAgent(name="proof"))
    again = [item if isinstance(item, str) else item[0] for item in harness["state"]["calls"]]
    assert "apps.create" not in again and "memory.create" not in again


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


LLM = "from apx_agent import LlmAgent\nagent = LlmAgent(name='{name}', tools=[])\n"
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
    ('\n[tool.apx.agent.deploy]\nspace = "app-space"\n', BUNDLE.format(app="mcp-row2", extra=SPACE), "App Space deploy preflight"),
    ('\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n', None, "Apps API space app-space"),
    ('\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\n', None, "Apps API dedicated"),
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
    ("row2", '\n[tool.apx.agent.deploy]\nspace = "app-space"\n', BUNDLE.format(app="mcp-row2", extra=SPACE), None, "mcp-row2"),
    ("row3", '\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n', None, "apps_api", "row3"),
    ("row4", '\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\n', None, "apps_api", "row4"),
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


def test_apps_api_name_is_not_agent_bricks_prefixed(tmp_path: Path) -> None:
    from apx_agent.cli import _resolve_project_app_name

    root = _doctor_project(
        tmp_path, "named", deploy='\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\n',
        bundle=None, agent=LLM.format(name="named"),
    )
    assert _resolve_project_app_name(root) == "named"


def test_plan_routes_apps_api_without_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _doctor_project(
        tmp_path, "planned",
        deploy='\n[tool.apx.agent.deploy]\nbackend = "apps_api"\nentrypoint = "app"\nspace = "app-space"\n',
        bundle=None, agent=LLM.format(name="planned"),
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps", "--dry-run", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.stdout) if result.stdout.startswith("target") else __import__("json").loads(result.stdout)
    assert payload["deployment_backend"] == "apps_api"
    assert payload["app_name"] == "planned"
    assert "agent-bricks-" not in payload["app_name"]

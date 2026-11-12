"""Native Agent Bricks deploy of a front door that binds other apps (named bindings).

Three gaps found deploying a SequentialAgent with ``bindings=`` on the native path:
app dependencies were refused even when every hop runs as the request user,
``[tool.apx.deploy] include`` was ignored by native staging, and ``--env`` values
didn't reach the local agent load that resolves ``$VAR`` bindings.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest

from apx_agent import Agent
from apx_agent._apps_authorization import AppDependency, AuthorizationPlan

_PEER = "https://agent-bricks-peer-1.aws.databricksapps.com"
_TOML = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
_USER = '\n[auth.user]\nrequired = {required}\nadditional_api_scopes = []\n'


class _ReachedStaging(Exception):
    """Raised by the stubbed native staging: the deploy got past every guard."""


def _noop() -> str:
    """No-op tool."""
    return ""


@pytest.fixture(autouse=True)
def _isolated_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    yield
    sys.modules.pop("agent", None)


def _project(tmp_path: Path, *, required: bool, deploy: tuple[str, ...] = ()) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n{"".join(deploy)}')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent\nagent = Agent(name='p', target='durable_agent_server', tools=[])\n")
    (tmp_path / "agent.toml").write_text(_TOML + _USER.format(required=str(required).lower()))
    (tmp_path / "app.yaml").write_text("command: [uv, run, python, server.py]\n")
    return tmp_path


def _stub_deploy(monkeypatch: pytest.MonkeyPatch, staged: dict[str, Any]) -> None:
    from apx_agent import cli

    def stage(cwd: Path, **kwargs: Any) -> None:
        staged.update(kwargs)
        raise _ReachedStaging

    plan = AuthorizationPlan(
        operations=(), user_resources=(), service_resources=(),
        user_api_scopes=(), app_dependencies=(AppDependency(_PEER),),
    )
    monkeypatch.setattr("apx_agent._agentbricks_deploy.stage_native_source", stage)
    monkeypatch.setattr("apx_agent._apps_authorization.compile_authorization_plan", lambda *a, **k: plan)
    monkeypatch.setattr("apx_agent._agent_toml.require_agent_toml_ok", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: None)
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(cli._doctor_mod, "check_agent_image", lambda *a, **k: None)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.validate_native_app", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="p", tools=[_noop]))
    # Peers still resolve to exact workspace Apps on the native path; stub the workspace lookup.
    monkeypatch.setattr(cli, "_resolve_app_dependencies", lambda deps, profile: [])
    monkeypatch.setattr(cli, "_validate_resolved_app_dependencies", lambda *a, **k: None)


def _deploy(cwd: Path, **kwargs: Any) -> None:
    from apx_agent import cli

    cli._deploy_apps_impl(cwd=cwd, module="agent:agent", profile="sandbox", bundle_target="dev",
                          no_run=True, auto_update_yml=False, auto_build_wheel=True,
                          json_output=False, record_registry=False, log=lambda message: None, **kwargs)


def test_app_dependencies_deploy_natively_when_every_hop_runs_as_the_request_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deploy(monkeypatch, {})
    with pytest.raises(_ReachedStaging):
        _deploy(_project(tmp_path, required=True))


def test_app_dependencies_still_refused_without_a_required_request_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deploy(monkeypatch, {})
    with pytest.raises(click.ClickException, match=r"\[auth\.user\] required = true"):
        _deploy(_project(tmp_path, required=False))


def test_native_staging_receives_declared_deploy_include(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: dict[str, Any] = {}
    _stub_deploy(monkeypatch, staged)
    project = _project(tmp_path, required=True, deploy=('include = ["domain", "handoff.py"]\n',))
    with pytest.raises(_ReachedStaging):
        _deploy(project)
    assert staged["include"] == ("domain", "handoff.py")


def test_env_flags_resolve_bindings_during_the_local_agent_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent import cli

    seen: dict[str, str | None] = {}

    def load(module: str) -> Agent:
        seen["PEER_APP_URL"] = os.environ.get("PEER_APP_URL")
        return Agent(name="p", tools=[_noop])

    _stub_deploy(monkeypatch, {})
    monkeypatch.setattr(cli, "_load_finalized_agent", load)
    monkeypatch.setenv("PEER_APP_URL", "https://stale.invalid")
    with pytest.raises(_ReachedStaging):
        _deploy(_project(tmp_path, required=True), env_pairs=(f"PEER_APP_URL={_PEER}",))
    assert seen["PEER_APP_URL"] == _PEER  # the value the App receives, not the shell's
    assert os.environ["PEER_APP_URL"] == "https://stale.invalid"  # restored afterwards

"""Native bindings to typed peers: the public NativeBinding, typed input over a
string binding, and the user scopes bound peers need (#1182, #1183).

Found deploying a three-app SequentialAgent root on the native Agent Bricks path:
NativeBinding had to be imported from a private module, a string binding sent a
typed peer no artifact (422 at runtime), and the root's forwarded token lacked the
peers' ``sql`` scope (401 MCP_USER_AUTH_REQUIRED at runtime).
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from pydantic import BaseModel, ConfigDict

from apx_agent import Agent, SequentialAgent
from apx_agent._apps_authorization import AppDependency, AuthorizationPlan, ResolvedAppDependency

_PEER = "https://agent-bricks-peer-1.aws.databricksapps.com"
_TOML = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'


class _ReachedStaging(Exception):
    """Raised by the stubbed native staging: the deploy got past every guard."""


class _Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str


def _noop() -> str:
    """No-op tool."""
    return ""


@pytest.fixture(autouse=True)
def _isolated_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    yield
    sys.modules.pop("agent", None)


def test_native_binding_is_public_api() -> None:
    import apx_agent
    from apx_agent._models import NativeBinding as Private

    assert apx_agent.NativeBinding is Private
    assert "NativeBinding" in apx_agent.__all__


def test_app_dependency_resolution_records_the_peer_user_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import cli

    peer = SimpleNamespace(id="app-1", name="agent-bricks-peer", url=_PEER, user_api_scopes=["ai-gateway", "sql"])
    workspace = SimpleNamespace(apps=SimpleNamespace(list=lambda: [peer]))
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda profile: workspace)
    resolved = cli._resolve_app_dependencies((AppDependency(_PEER),), profile="sandbox")
    assert resolved[0].user_api_scopes == ("ai-gateway", "sql")


def _bound_root(*, native: bool) -> Agent:
    from apx_agent._models import NativeBinding
    from apx_agent._wiring import _apply_remote_leaf_bindings

    peer = Agent(name="peer", input_schema=_Decision, input_key="pricing",
                 output_schema=_Decision, output_key="opportunity")
    root = SequentialAgent([Agent(name="local", tools=[_noop]), peer], name="root")
    reference: Any = NativeBinding(transport="native", url=_PEER) if native else _PEER
    _apply_remote_leaf_bindings(root, {"peer": reference})
    return root


def _project(tmp_path: Path, scopes: tuple[str, ...]) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.deploy]\nname = "p"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent\nagent = Agent(name='p', target='durable_agent_server', tools=[])\n")
    listed = ", ".join(f'"{scope}"' for scope in scopes)
    (tmp_path / "agent.toml").write_text(
        _TOML + f"\n[auth.user]\nrequired = true\nadditional_api_scopes = [{listed}]\n")
    (tmp_path / "app.yaml").write_text("command: [uv, run, python, server.py]\n")
    return tmp_path


def _deploy(monkeypatch: pytest.MonkeyPatch, project: Path, *, agent: Agent,
            peer_scopes: tuple[str, ...]) -> None:
    from apx_agent import cli

    def stage(cwd: Path, **kwargs: Any) -> None:
        raise _ReachedStaging

    plan = AuthorizationPlan(operations=(), user_resources=(), service_resources=(),
                             user_api_scopes=(), app_dependencies=(AppDependency(_PEER),))
    resolved = [ResolvedAppDependency(id="app-1", name="agent-bricks-peer", url=_PEER,
                                      user_api_scopes=peer_scopes)]
    monkeypatch.setattr("apx_agent._agentbricks_deploy.stage_native_source", stage)
    monkeypatch.setattr("apx_agent._apps_authorization.compile_authorization_plan", lambda *a, **k: plan)
    monkeypatch.setattr("apx_agent._agent_toml.require_agent_toml_ok", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: None)
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(cli._doctor_mod, "check_agent_image", lambda *a, **k: None)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.validate_native_app", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: agent)
    monkeypatch.setattr(cli, "_resolve_app_dependencies", lambda deps, profile: resolved)
    monkeypatch.setattr(cli, "_validate_resolved_app_dependencies", lambda *a, **k: None)
    cli._deploy_apps_impl(cwd=project, module="agent:agent", profile="sandbox", bundle_target="dev",
                          no_run=True, auto_update_yml=False, auto_build_wheel=True,
                          json_output=False, record_registry=False, log=lambda message: None)


def test_native_deploy_refuses_when_bound_peers_need_scopes_the_root_does_not_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(click.ClickException, match=r"need user API scopes .*: sql"):
        _deploy(monkeypatch, _project(tmp_path, ("ai-gateway",)), agent=_bound_root(native=True),
                peer_scopes=("ai-gateway", "sql"))


def test_native_deploy_proceeds_when_the_root_requests_its_peers_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(_ReachedStaging):
        _deploy(monkeypatch, _project(tmp_path, ("ai-gateway", "sql")), agent=_bound_root(native=True),
                peer_scopes=("ai-gateway", "sql"))


def test_native_deploy_refuses_a_string_binding_to_a_typed_input_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(click.ClickException, match=r"peer.*NativeBinding"):
        _deploy(monkeypatch, _project(tmp_path, ("ai-gateway", "sql")), agent=_bound_root(native=False),
                peer_scopes=("ai-gateway", "sql"))

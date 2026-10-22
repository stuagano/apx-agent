"""Sub-project 2: apx settings live on the agent object (#954 follow-on)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest

from apx_agent import Agent
from apx_agent._inspection import config_from_agent


def _noop(q: str) -> str:
    """Echo."""
    return q


def test_agent_constructor_keeps_apx_settings() -> None:
    agent = Agent(
        name="triage",
        tools=[_noop],
        model="databricks-claude-sonnet-4-6",
        target="durable_agent_server",
        identity="sp",
        recovery=True,
    )
    assert agent.model == "databricks-claude-sonnet-4-6"
    assert agent.target == "durable_agent_server"
    assert agent.identity == "sp"
    assert agent.recovery is True


def test_agent_rejects_invalid_identity() -> None:
    with pytest.raises(ValueError, match="named-sp"):
        Agent(name="a", tools=[_noop], identity="not-a-mode")


def test_config_from_agent_copies_constructor_settings() -> None:
    agent = Agent(
        name="triage",
        tools=[_noop],
        model="databricks-claude-sonnet-4-6",
        identity="obo",
        recovery=True,
        memory="managed",
    )
    config = config_from_agent(agent)
    assert config.name == "triage"
    assert config.model == "databricks-claude-sonnet-4-6"
    assert config.target == "durable_agent_server"
    assert config.identity == "obo"
    assert config.recovery is True
    assert config.memory is not None and config.memory.store_name == agent.memory_config.store_name


def test_python_only_native_deploy_does_not_need_pyproject_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent import cli

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', tools=[])\n")
    (tmp_path / "agent.toml").write_text(
        'schema_version = 1\n\n[agent]\nframework = "openai"\nserver = "agentbricks"\n')
    seen: list[Any] = []

    def fake_compile(config: Any) -> dict[str, str]:
        seen.append(config)
        return {"name": f"agent-bricks-{config.name}", "config": {"env": []}}

    def reached(*a: Any, **k: Any) -> Any:
        raise AssertionError("bundle/staging reached; python-only deploy should stay native_direct")

    monkeypatch.setattr("apx_agent._agentbricks_deploy.compile_native_app", fake_compile)
    monkeypatch.setattr("apx_agent._agent_toml.stage_agent_toml", reached)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.stage_native_source", reached)
    monkeypatch.setattr(cli, "_read_databricks_yml", reached)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: None)
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(cli._doctor_mod, "check_agent_image", lambda *a, **k: None)
    monkeypatch.setattr("apx_agent._agentbricks_deploy.validate_native_app", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="from-python", tools=[_noop]))
    with pytest.raises(click.ClickException, match=r"agent\.toml"):
        cli._deploy_apps_impl(cwd=tmp_path, module="agent:agent", profile=None, bundle_target="dev",
                              no_run=True, auto_update_yml=False, auto_build_wheel=False,
                              json_output=False, log=lambda message: None)
    assert seen and seen[0].name == "from-python"
    assert seen[0].target == "durable_agent_server"


def test_python_only_durable_doctor_loads_the_agent(tmp_path: Path) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n')
    (root / "agent.py").write_text(
        "from apx_agent import LlmAgent\nagent = LlmAgent(name='p', tools=[])\n")
    checks = {c.name: c for c in check_durable_readiness(root)}
    assert checks["Config"].status is Status.OK
    assert "LlmAgent" in checks["Config"].detail


def test_launcher_compiles_from_agent_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import sys
    from apx_agent._serve import create_app

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    agent = Agent(name="proof", tools=[_noop], model="agent-model")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=agent))
    monkeypatch.setenv("APX_PYPROJECT", str(tmp_path / "pyproject.toml"))
    monkeypatch.delenv("APX_MODEL", raising=False)
    monkeypatch.delenv("AGENT_SESSION_STORE", raising=False)
    ws = MagicMock()
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: ws)
    native_app = SimpleNamespace(state=SimpleNamespace())
    compiler = MagicMock(return_value=native_app)
    monkeypatch.setattr("apx_agent._runtime_targets.compile_agent", compiler)
    assert create_app() is native_app
    kwargs = compiler.call_args.kwargs
    assert kwargs["model"] == "agent-model"
    assert kwargs["config"].name == "proof"
    assert kwargs["config"].target == "durable_agent_server"
    assert kwargs["service_ws"] is ws


from apx_agent import HandoffAgent, KeywordRouter, LoopAgent, ParallelAgent, RouterAgent, SequentialAgent


def _leaf(name: str = "leaf", **settings: Any) -> Agent:
    return Agent(name=name, tools=[_noop], **settings)


@pytest.mark.parametrize("build", [
    lambda **s: SequentialAgent([_leaf()], **s),
    lambda **s: ParallelAgent([_leaf()], **s),
    lambda **s: RouterAgent([_leaf()], **s),
    lambda **s: LoopAgent(_leaf(), **s),
    lambda **s: HandoffAgent([_leaf()], **s),
    lambda **s: KeywordRouter([("a", _leaf(), ["a"])], default=_leaf("d"), **s),
])
def test_every_agent_class_takes_settings(build: Any) -> None:
    agent = build(model="m", target="durable_agent_server", knowledge="./okf")
    assert (agent.model, agent.target, agent.knowledge) == ("m", "durable_agent_server", "./okf")


def test_unset_settings_are_none() -> None:
    agent = SequentialAgent([_leaf()])
    assert agent.model is None and agent.recovery is None and agent.identity is None


def test_unknown_setting_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="modle"):
        SequentialAgent([_leaf()], modle="m")  # type: ignore[call-arg]


def test_composite_rejects_invalid_identity_and_recovery_on_responses() -> None:
    with pytest.raises(ValueError, match="identity"):
        SequentialAgent([_leaf()], identity="not-a-mode")
    with pytest.raises(ValueError, match="recovery requires"):
        SequentialAgent([_leaf()], recovery=True, target="responses_agent")

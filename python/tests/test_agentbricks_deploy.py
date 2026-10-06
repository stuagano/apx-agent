"""Exercise the installed CLI against controlled cloud boundaries."""

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
import yaml
from click.testing import CliRunner

from apx_agent import AgentConfig, LlmAgent
from apx_agent._agentbricks_deploy import deploy_native_project, validate_cli_deployment
from apx_agent._apps_authorization import compile_authorization_plan
from apx_agent._durable_agent import build_native_manifest
from apx_agent._project_gen import _build_databricks_yml, generate_project


@pytest.mark.parametrize("failure", [None, "grant", "rollout"])
def test_installed_cli_rollout_preserves_native_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    from ctk import Artifact, verify
    from databricks_agentbricks.agent_project import AgentProject

    # Production runs the CLI in a child process. Contain its global Click
    # customization while exercising the real command in this test process.
    monkeypatch.setattr(click.UsageError, "show", click.UsageError.show)
    from databricks_agentbricks.cli.app import CliContext, agentbricks
    from databricks_agentbricks import errors

    monkeypatch.setattr(errors, "_OUTPUT_MODE", errors._OUTPUT_MODE)

    native = importlib.import_module("databricks_agentbricks.cli.deploy")
    config = AgentConfig(name="proof", target="durable_agent_server",
                         memory={"type": "managed", "store_name": "memory"},
                         session={"type": "managed", "store_name": "sessions"})
    plan = compile_authorization_plan(LlmAgent(name="proof"), model=config.model)
    (tmp_path / "agent.toml").write_text(build_native_manifest(config=config, authorization_plan=plan))
    sdk = MagicMock()
    sdk.current_user = "owner@example.com"
    sdk.host = "https://workspace.example.com"
    sdk.create_memory_store.return_value = {"name": "memory-stores/memory-id", "display_name": "memory"}
    sdk.create_session_store.return_value = {"name": "session-stores/sessions"}
    sdk.create_runtime_store.return_value = {
        "name": "runtime-stores/agent-bricks-proof",
        "owner": {"app": {"name": "agent-bricks-proof", "service_principal_id": "app-sp"}},
        "storage_backend": {"lakebase": {"branch": "projects/p/branches/b", "database_id": "runtime"}},
    }
    monkeypatch.setattr(CliContext, "client", lambda self: sdk)
    monkeypatch.setattr(native, "plan_app_user_scope_update", lambda *a, **k: SimpleNamespace(existing_scopes=[]))
    auth = MagicMock()
    monkeypatch.setattr(native, "apply_app_user_scope_update", auth)
    monkeypatch.setattr(native, "_app_service_principal", lambda *a: "app-sp")
    monkeypatch.setattr(native, "_app_compute_state", lambda *a: "ACTIVE")
    monkeypatch.setattr(native, "_app_url", lambda *a: "https://proof.databricksapps.com")
    monkeypatch.setattr(native, "create_experiment_idempotent", lambda *a: SimpleNamespace(
        experiment_id="experiment", tables=native.MLflowTraceTables()))
    monkeypatch.setattr(native, "reconcile_tool_access", lambda *a: None)
    monkeypatch.setattr(native, "finalize_tool_access", lambda *a: None)
    monkeypatch.setattr(native, "apply_trace_resources", lambda *a: None)
    monkeypatch.setattr(native, "_grant_store_access", lambda *a: "synthetic-secret" if failure == "grant" else None)
    cloud_calls = []

    def cloud(args, profile, **kwargs):
        assert profile == "chosen"
        cloud_calls.append(args)
        if failure == "rollout" and args[:2] == ["apps", "deploy"]:
            raise click.ClickException("synthetic-secret")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(native, "_databricks", cloud)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[1:3] == ["-m", "databricks_agentbricks.cli.app"]
        assert "--allow-user-scope-update" in command
        assert kwargs["cwd"] == tmp_path
        result = CliRunner().invoke(agentbricks, command[3:])
        return subprocess.CompletedProcess(command, result.exit_code, result.output, "")

    monkeypatch.setattr("apx_agent._agentbricks_deploy.subprocess.run", run)
    app = {"config": {"command": ["python", "-m", "agent_server.start_host"], "env": [
        {"name": "APX_APPS_HOST", "value": "agentbricks"},
        {"name": "TRANSIENT", "value": "synthetic-secret"},
    ]}}
    kwargs = dict(app=app, app_name="agent-bricks-proof", profile="chosen",
                  experiment_name="/Users/owner/proof", transient_env_keys=["TRANSIENT"])
    if failure:
        with pytest.raises(click.ClickException) as error:
            deploy_native_project(tmp_path, **kwargs)
        assert "synthetic-secret" not in str(error.value)
    else:
        result = deploy_native_project(tmp_path, **kwargs)
        assert result["store_grant"] == "granted"
        assert result["trace_experiment_id"] == "experiment"
        # Redeploy must accept the files rewritten by the real CLI.
        deploy_native_project(tmp_path, **kwargs)
        verify(Artifact(str(tmp_path / "agent.toml"), must_contain="[auth.user]"))
        sdk.create_runtime_store.assert_called_with("agent-bricks-proof", "app-sp", app_name="agent-bricks-proof", retry_transient=True)
    project = AgentProject.load(tmp_path)
    assert project.memory_store == "memory" and project.session_store == "sessions"
    assert project.user_auth.required
    assert project.deployment_name == "agent-bricks-proof"
    assert project.trace_experiment_name == "/Users/owner/proof"
    saved = yaml.safe_load((tmp_path / "app.yaml").read_text())
    env = {entry["name"]: entry["value"] for entry in saved["env"]}
    assert "TRANSIENT" not in env
    assert env["AGENT_MEMORY_STORE"] == "memory-id"
    assert env["AGENT_SESSION_STORE"] == "sessions"
    assert env["DATABRICKS_AGENTBRICKS_RUNTIME_STORE_USERNAME"] == "app-sp"
    assert any(args[:2] == ["apps", "deploy"] for args in cloud_calls)
    assert auth.call_count == len(commands)


@pytest.mark.parametrize("problem", ["profile", "name", "no_run", "resources", "scaling", "target", "env", "index"])
def test_native_preflight_rejects_configuration_loss(tmp_path: Path, problem: str) -> None:
    config = AgentConfig(name="proof", target="durable_agent_server")
    generate_project(config, tmp_path)
    doc = yaml.safe_load(_build_databricks_yml(config))
    app = doc["resources"]["apps"]["proof"]
    assert app["name"] == "agent-bricks-proof"
    original_error_formatter = click.UsageError.show
    validate_cli_deployment(doc, bundle_key="proof", app_name=app["name"], profile="chosen", no_run=False)
    assert click.UsageError.show is original_error_formatter
    if problem == "resources":
        app["resources"] = [{"name": "existing-grant"}]
    elif problem == "scaling":
        app.update(compute_min_instances=1, compute_max_instances=2)
    elif problem == "target":
        doc["targets"]["prod"]["resources"]["jobs"] = {"job": {}}
    elif problem == "env":
        app["config"]["env"].append({"name": "SECRET", "value_from": "resource"})
    elif problem == "index":
        app["config"]["env"].append({"name": "UV_INDEX_URL", "value": "https://custom.example"})
    with pytest.raises(click.ClickException):
        validate_cli_deployment(doc, bundle_key="proof", app_name="old-app" if problem == "name" else app["name"],
                                profile=None if problem == "profile" else "chosen", no_run=problem == "no_run")


@pytest.mark.parametrize("symlink", [False, True])
def test_native_staging_preserves_authored_files(tmp_path: Path, symlink: bool) -> None:
    authored = tmp_path / "authored.yaml"
    authored.write_text("command: [user-command]\n")
    staged = tmp_path / "app.yaml"
    if symlink:
        staged.symlink_to(authored)
    else:
        staged.write_text(authored.read_text())
    with pytest.raises(click.ClickException, match="symlinked|authored"):
        deploy_native_project(tmp_path, app={}, app_name="agent-bricks-proof", profile="chosen",
                              experiment_name=None, transient_env_keys=[])
    assert authored.read_text() == "command: [user-command]\n"
    assert staged.read_text() == authored.read_text()


def test_native_source_rebuild_removes_stale_files_and_preserves_sources(tmp_path: Path) -> None:
    from apx_agent._agentbricks_deploy import stage_native_source
    from ctk import Artifact, verify

    generate_project(AgentConfig(name="direct", target="durable_agent_server"), tmp_path)
    assert not (tmp_path / "databricks.yml").exists()
    assert not (tmp_path / "app.yml").exists()
    tools = tmp_path / "tools.py"
    tools.write_text("TOOL_VERSION = 1\n")
    stage_native_source(tmp_path)
    verify(Artifact(str(tmp_path / ".build" / "tools.py"), must_contain="TOOL_VERSION = 1"))
    tools.unlink()
    stage_native_source(tmp_path)
    assert not (tmp_path / ".build" / "tools.py").exists()
    verify(Artifact(str(tmp_path / ".build" / "agent.py"), must_contain="LlmAgent"))
    assert (tmp_path / "agent.py").read_text() == (tmp_path / ".build" / "agent.py").read_text()


def test_optional_yaml_native_input_can_be_rematerialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import cli as cli_mod
    from apx_agent._inspection import _load_agent_config

    monkeypatch.setattr(cli_mod, "_bake_schema_into_project", lambda *args: False)
    config = AgentConfig(name="direct", target="durable_agent_server", model="first-model")
    spec = tmp_path / "direct.yaml"
    spec.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)))
    project = cli_mod._materialize_yaml_project(spec, config, None)
    updated = config.model_copy(update={"model": "updated-model"})
    assert cli_mod._materialize_yaml_project(spec, updated, None) == project
    assert _load_agent_config(pyproject_path=project / "pyproject.toml") == updated
    assert not (project / "databricks.yml").exists()


def test_native_generated_guidance_has_no_bundle_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import cli as cli_mod
    from ctk import Artifact, verify

    monkeypatch.setattr(cli_mod, "_bake_schema_into_project", lambda *args: False)
    monkeypatch.setattr(cli_mod, "_emit_governance_receipt", lambda *args, **kwargs: None)
    cli_mod._materialize_agent(AgentConfig(name="direct", target="durable_agent_server"), tmp_path, force=False)
    verify(Artifact(str(tmp_path / "README.md"), must_contain="--profile <profile>"))
    assert "No customer-authored YAML" in (tmp_path / "README.md").read_text()
    assert not (tmp_path / "scripts" / "quickstart.py").exists()
    assert not (tmp_path / ".env.example").exists()
    assert not (tmp_path / "agent_server").exists()


def test_native_rematerialization_preserves_unrelated_python_project(tmp_path: Path) -> None:
    from apx_agent import cli as cli_mod

    project = tmp_path / "existing"
    project.mkdir()
    (project / "agent.py").write_text("# authored agent\n")
    (project / "pyproject.toml").write_text('[project]\nname = "unrelated"\n')
    with pytest.raises(click.ClickException, match="non-apx directory"):
        cli_mod._materialize_yaml_project(tmp_path / "existing.yaml",
                                         AgentConfig(name="native", target="durable_agent_server"), None)
    assert (project / "agent.py").read_text() == "# authored agent\n"


@pytest.mark.parametrize("hazard", ["unowned", "source-link", "destination-link", "lock-link"])
def test_native_source_staging_fails_closed(tmp_path: Path, hazard: str) -> None:
    from apx_agent._agentbricks_deploy import stage_native_source

    protected = tmp_path / "protected"
    protected.write_text("preserve me")
    if hazard == "unowned":
        (tmp_path / ".build").mkdir()
        (tmp_path / ".build" / "authored.py").write_text("preserve me")
    elif hazard == "source-link":
        (tmp_path / "agent_server").mkdir()
        (tmp_path / "agent_server" / "nested.py").symlink_to(protected)
    elif hazard == "lock-link":
        (tmp_path / "uv.lock").symlink_to(protected)
    else:
        (tmp_path / ".build").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(click.ClickException, match="symlinked|unowned"):
        stage_native_source(tmp_path)
    assert protected.read_text() == "preserve me"
    if hazard == "unowned":
        assert (tmp_path / ".build" / "authored.py").read_text() == "preserve me"

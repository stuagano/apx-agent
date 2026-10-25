"""Model Serving is removed: --target model-serving names Apps (sub-project 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apx_agent import cli

_COMMANDS = [
    ("agents deploy", ["agents", "deploy", "--target", "model-serving"]),
    ("agents scaffold", ["agents", "scaffold", "x", "--target", "model-serving", "--no-interactive"]),
    ("agents hot-swap", ["agents", "hot-swap", "--target", "model-serving"]),
    ("canary status", ["canary", "status", "--target", "model-serving"]),
    ("canary deploy", ["canary", "deploy", "--target", "model-serving"]),
    ("canary promote", ["canary", "promote", "--target", "model-serving"]),
    ("canary rollback", ["canary", "rollback", "--target", "model-serving"]),
    ("canary analyze", ["canary", "analyze", "--target", "model-serving"]),
]


@pytest.mark.parametrize(("command", "args"), _COMMANDS, ids=[c for c, _ in _COMMANDS])
def test_model_serving_target_names_apps(command: str, args: list[str], tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    touched = MagicMock(side_effect=AssertionError("must fail before any workspace access"))
    for name in ("_make_scaffold_workspace_client", "_make_ws_for_scaffold", "_run_databricks_cmd"):
        monkeypatch.setattr(cli, name, touched, raising=False)
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 2, result.output  # a usage error
    assert (f"Model Serving was removed: apx deploys agents to Databricks Apps. "
            f"Run apx-agent {command} --target apps.") in result.output, result.output
    touched.assert_not_called()


def test_hot_swap_defaults_to_apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "_hot_swap_apps_cli", lambda **kw: calls.append(kw))
    result = CliRunner().invoke(cli.main, ["agents", "hot-swap", "--llm-endpoint", "databricks-claude-sonnet-4-6"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0]["new_value"] == "databricks-claude-sonnet-4-6"


@pytest.mark.parametrize("sub", ["status", "deploy", "promote", "rollback", "analyze"])
def test_canary_defaults_to_apps_without_serving_options(sub: str) -> None:
    command = cli.canary.commands[sub]
    names = {opt for p in command.params for opt in p.opts}
    assert not names & {"--endpoint", "--workload-size", "--no-scale-to-zero", "--version"}, names
    assert next(p for p in command.params if p.name == "deploy_target").default == "apps"


def test_hot_swap_has_no_serving_options() -> None:
    names = {opt for p in cli.agents.commands["hot-swap"].params for opt in p.opts}
    assert not names & {"--endpoint", "--no-wait"}, names


_REMOVED_DEPLOY_FLAGS = ["--no-deploy", "--no-publish-tools", "--no-set-uc-tags", "--agent-name", "--capture-env-vars",
                         "--allow-env-var", "--scale-to-zero", "--workload-size", "--env-suffix", "--version", "--hot-swap",
                         "--model", "--name", "--experiment"]


@pytest.mark.parametrize("flag", _REMOVED_DEPLOY_FLAGS)
def test_removed_deploy_flag_is_an_unknown_option(flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["agents", "deploy", flag, "x"])
    assert result.exit_code == 2 and "No such option" in result.output, result.output


def test_an_old_serving_layout_detects_apps(tmp_path: Path) -> None:
    # The flat agent.py + app.py layout without databricks.yml used to auto-detect Model Serving.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', tools=[])\n")
    (tmp_path / "app.py").write_text("from apx_agent import create_app\nfrom agent import agent\napp = create_app(agent)\n")
    assert cli._detect_target(tmp_path).target == "apps"


def test_scaffold_target_choice_is_apps_only_in_help() -> None:
    target = next(p for p in cli.agents.commands["scaffold"].params if p.name == "scaffold_target")
    assert target.help is not None and "model-serving" not in target.help


def test_run_serves_a_flat_app_py_project_from_app_py(tmp_path: Path) -> None:
    # `agents run` picks the module by layout; dropping Model Serving detection must not change it.
    (tmp_path / "app.py").write_text("app = None\n")
    assert cli._run_module_for_layout(tmp_path) == "app:app"
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("app = None\n")
    assert cli._run_module_for_layout(tmp_path) == "agent_server.start_server:app"

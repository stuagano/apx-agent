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


def test_serving_only_commands_are_gone() -> None:
    assert "cost" not in cli.agents.commands
    assert "endpoints" not in cli.main.commands


@pytest.mark.parametrize(("sub", "gone"), [("logs", {"--endpoint", "--served-model", "--build"}),
                                           ("delete", {"--endpoint"})])
def test_serving_only_options_are_gone(sub: str, gone: set[str]) -> None:
    names = {opt for p in cli.agents.commands[sub].params for opt in p.opts}
    assert not names & gone, names
    assert "--app" in names


def test_advertise_still_registers_a_serving_endpoint_peer() -> None:
    # Kept by the spec: advertising an existing serving endpoint is not hosting one.
    opt = next(p for p in cli.agents.commands["advertise"].params if p.name == "endpoint_type")
    assert "model-serving" in opt.type.choices


def test_the_scaffold_wizard_never_offers_model_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default first-run path in a TTY: it must not prompt for, or return, model-serving.
    prompts: list[str] = []
    monkeypatch.setattr(cli.click, "prompt", lambda text, **kw: prompts.append(text) or kw["default"])
    target, *_ = cli._scaffold_wizard(None, None, "base", None, None)
    assert target == "apps"
    assert "Target" not in prompts


def test_apps_preflight_on_a_flat_layout_does_not_point_at_model_serving(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', tools=[], target='responses_agent')\n")
    (tmp_path / "app.py").write_text("app = None\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)  # CI has no databricks binary
    result = CliRunner().invoke(cli.main, ["agents", "deploy", "--profile", "x"])
    assert result.exit_code != 0
    assert "--target model-serving" not in result.output
    assert "agents scaffold" in result.output and "--target apps" in result.output, result.output


def test_doctor_accepts_a_flat_app_py_project(tmp_path: Path) -> None:
    from apx_agent import _doctor

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nname = "p"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', tools=[])\n")
    (tmp_path / "app.py").write_text("from apx_agent import create_app\nfrom agent import agent\napp = create_app(agent)\n")
    assert _doctor.check_project_layout(tmp_path).status is _doctor.Status.OK


def test_generated_and_help_text_never_point_at_model_serving() -> None:
    src = Path(cli.__file__).read_text()
    for template in ("_SCAFFOLD_APPS_AGENT",):
        start = src.index(f"{template} = ")
        assert "--target model-serving" not in src[start:src.index('"""', src.index('"""', start) + 3)], template
    helps = [p.help for c in (cli.agents.commands["deploy"], cli.agents.commands["scaffold"], cli.agents.commands["refresh-schema"])
             for p in c.params if isinstance(p, cli.click.Option) and p.help]
    assert not [h for h in helps if "model-serving" in h or "Model Serving" in h]
    deploy_help = cli.agents.commands["deploy"].help
    assert deploy_help is not None and "Model Serving" not in deploy_help.replace("Model Serving was removed", "")


_REMOVED_EXPORTS = ("log_agent", "hot_swap_model", "HotSwapResult", "get_active_override", "APX_MODEL_OVERRIDE_ENV",
                    "cost_for_endpoint", "cost_for_agent", "CostBreakdown", "CanaryConfig", "CanaryReport",
                    "VersionMetrics", "analyze_canary", "deploy_canary", "get_canary_config", "promote_canary",
                    "rollback_canary")
_REMOVED_CLI_SYMBOLS = ("_emit_serving_deploy_plan", "_serving_health_gate", "_scaffold_model_serving",
                        "_hot_swap_model_serving", "_resolve_served_model_name", "mlflow_resources_for",
                        "_SCAFFOLD_PYPROJECT ", "_SCAFFOLD_AGENT ", "_SCAFFOLD_APP ")


def test_no_public_model_serving_hosting_api() -> None:
    import apx_agent

    assert [n for n in _REMOVED_EXPORTS if hasattr(apx_agent, n) or n in apx_agent.__all__] == []
    src = Path(cli.__file__).read_text()
    assert [s for s in _REMOVED_CLI_SYMBOLS if s in src] == []


def test_the_serving_modules_are_gone() -> None:
    import importlib.util

    assert [m for m in ("apx_agent._canary", "apx_agent._hot_swap") if importlib.util.find_spec(m)] == []


@pytest.mark.parametrize(("override", "expected"), [("databricks-claude-opus-4-7", "databricks-claude-opus-4-7"),
                                                    (None, "databricks-claude-sonnet-4-6"),
                                                    ("", "databricks-claude-sonnet-4-6")])
def test_the_runtime_model_override_still_applies(override: str | None, expected: str,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    # Kept by the spec: both runtimes honor APX_AGENT_MODEL_OVERRIDE; unset or empty falls back.
    from apx_agent._chat_agent import chat_agent_for
    from apx_agent._responses_agent import _resolve_model
    from apx_agent import Agent

    if override is None:
        monkeypatch.delenv("APX_AGENT_MODEL_OVERRIDE", raising=False)
    else:
        monkeypatch.setenv("APX_AGENT_MODEL_OVERRIDE", override)
    assert _resolve_model("databricks-claude-sonnet-4-6") == expected
    chat = chat_agent_for(Agent(name="p", tools=[]), model="databricks-claude-sonnet-4-6")
    assert chat._resolve_model() == expected


def test_host_model_serving_is_a_clear_validation_error() -> None:
    from pydantic import ValidationError

    from apx_agent._models import AgentConfig

    with pytest.raises(ValidationError, match="Model Serving was removed"):
        AgentConfig.model_validate({"name": "p", "host": "model_serving"})


def test_user_facing_help_and_samples_do_not_name_removed_apis() -> None:
    # CLI help and the package's own code samples must not point at removed functions.
    src = Path(cli.__file__).parent
    helps = [p.help for c in cli.agents.commands.values() for p in c.params
             if isinstance(p, cli.click.Option) and p.help]
    assert [h for h in helps if "log_agent" in h] == []
    assert "log_agent" not in (src / "_tool_publish.py").read_text()

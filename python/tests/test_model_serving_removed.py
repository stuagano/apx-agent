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

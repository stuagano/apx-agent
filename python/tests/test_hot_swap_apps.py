"""Tests for the Apps-target hot-swap flow.

Covers ``_hot_swap_apps.py`` (bundle var read + redeploy orchestration)
and the ``apx hot-swap --target apps`` CLI dispatch. Mocks the
``databricks`` CLI subprocess seam exactly like ``test_canary_apps.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apx_agent import (
    AppsHotSwapResult,
    DEFAULT_LLM_VAR_NAME,
    hot_swap_apps,
    read_var_default,
)
from apx_agent.cli import main


_DATABRICKS_YML = """\
bundle:
  name: test-app

variables:
  llm_endpoint_name:
    description: Foundation model endpoint.
    default: databricks-claude-sonnet-4-6

resources:
  apps:
    my-app:
      name: my-app
      description: test
      source_code_path: ./.build

targets:
  prod:
    default: true
    mode: production
"""


def _write_scaffold(tmp_path: Path, *, yml: str = _DATABRICKS_YML) -> Path:
    (tmp_path / "databricks.yml").write_text(yml)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-app"\n')
    server = tmp_path / "agent_server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    (server / "agent.py").write_text("agent = object()\n")
    return tmp_path


@pytest.fixture
def scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_scaffold(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent_server", None)
    sys.modules.pop("agent_server.agent", None)
    return tmp_path


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, *, deploy_rc: int = 0,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(args: list[str], profile: str | None = None) -> _FakeProc:
        calls.append(list(args))
        if args[:2] == ["bundle", "deploy"]:
            return _FakeProc(deploy_rc, stdout="ok\n", stderr="")
        return _FakeProc(0, stdout="", stderr="")

    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", fake)
    return calls


# ---------------------------------------------------------------------------
# read_var_default — long form, short form, missing
# ---------------------------------------------------------------------------


def test_read_var_default_long_form() -> None:
    doc = yaml.safe_load(_DATABRICKS_YML)
    assert read_var_default(doc, "llm_endpoint_name") == "databricks-claude-sonnet-4-6"


def test_read_var_default_short_form() -> None:
    doc = {"variables": {"my_var": "literal-value"}}
    assert read_var_default(doc, "my_var") == "literal-value"


def test_read_var_default_missing() -> None:
    doc = {"variables": {}}
    assert read_var_default(doc, "absent") is None


def test_read_var_default_no_variables_block() -> None:
    assert read_var_default({}, "any") is None


# ---------------------------------------------------------------------------
# hot_swap_apps — orchestration
# ---------------------------------------------------------------------------


def test_hot_swap_apps_invokes_bundle_deploy_with_var(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    result = hot_swap_apps(
        cwd=scaffold,
        app_name="my-app",
        new_value="databricks-claude-opus-4-7",
        run_cmd=_run_databricks_cmd,
        bundle_target="prod",
    )
    assert isinstance(result, AppsHotSwapResult)
    assert result.previous_value == "databricks-claude-sonnet-4-6"
    assert result.new_value == "databricks-claude-opus-4-7"
    assert result.var_name == "llm_endpoint_name"
    assert result.bundle_target == "prod"
    # The deploy CLI call carried the right --var.
    deploy_call = next(
        c for c in calls if c[:2] == ["bundle", "deploy"]
    )
    assert "--var" in deploy_call
    assert "llm_endpoint_name=databricks-claude-opus-4-7" in deploy_call


def test_hot_swap_apps_custom_var_name(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bundle declares a non-default variable.
    custom_yml = _DATABRICKS_YML.replace(
        "llm_endpoint_name:", "model_endpoint:",
    )
    (scaffold / "databricks.yml").write_text(custom_yml)
    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    result = hot_swap_apps(
        cwd=scaffold,
        app_name="my-app",
        new_value="new-endpoint",
        run_cmd=_run_databricks_cmd,
        var_name="model_endpoint",
    )
    assert result.var_name == "model_endpoint"
    deploy_call = next(c for c in calls if c[:2] == ["bundle", "deploy"])
    assert "model_endpoint=new-endpoint" in deploy_call


def test_hot_swap_apps_raises_when_var_absent(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bundle with no `llm_endpoint_name` variable at all.
    yml = """\
bundle:
  name: test-app
variables:
  other_var:
    default: x
resources:
  apps:
    my-app:
      name: my-app
      source_code_path: ./.build
targets:
  prod:
    default: true
"""
    (scaffold / "databricks.yml").write_text(yml)
    _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    with pytest.raises(RuntimeError, match="variables.llm_endpoint_name"):
        hot_swap_apps(
            cwd=scaffold,
            app_name="my-app",
            new_value="anything",
            run_cmd=_run_databricks_cmd,
        )


def test_hot_swap_apps_raises_when_deploy_fails(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, deploy_rc=1)
    from apx_agent.cli import _run_databricks_cmd

    with pytest.raises(RuntimeError, match="bundle deploy"):
        hot_swap_apps(
            cwd=scaffold,
            app_name="my-app",
            new_value="x",
            run_cmd=_run_databricks_cmd,
        )


def test_hot_swap_apps_raises_when_yml_missing(tmp_path: Path) -> None:
    def _noop(*a: Any, **k: Any) -> Any:
        return _FakeProc(0)

    with pytest.raises(RuntimeError, match="No databricks.yml"):
        hot_swap_apps(
            cwd=tmp_path,
            app_name="x",
            new_value="y",
            run_cmd=_noop,
        )


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_cli_hot_swap_apps_target_invokes_redeploy(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "hot-swap", "--target", "apps",
         "--llm-endpoint", "databricks-claude-opus-4-7"],
    )
    assert result.exit_code == 0, result.output
    deploy_call = next(c for c in calls if c[:2] == ["bundle", "deploy"])
    assert "llm_endpoint_name=databricks-claude-opus-4-7" in deploy_call
    # The CLI output names the previous value so the operator can roll back.
    assert "databricks-claude-sonnet-4-6" in result.output


def test_cli_hot_swap_apps_requires_llm_endpoint(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "hot-swap", "--target", "apps"])
    assert result.exit_code != 0
    assert "--llm-endpoint" in result.output


def test_cli_hot_swap_apps_json_success(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json emits EXACTLY one JSON object on stdout naming the swap: app,
    var, new + previous value (issue #417)."""
    _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "hot-swap", "--target", "apps",
         "--llm-endpoint", "databricks-claude-opus-4-7", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "target": "apps",
        "app_name": "my-app",
        "bundle_target": "prod",
        "var_name": DEFAULT_LLM_VAR_NAME,
        "new_value": "databricks-claude-opus-4-7",
        "previous_value": "databricks-claude-sonnet-4-6",
    }


def test_cli_hot_swap_apps_json_failure(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json on a failed swap (bundle deploy non-zero) emits EXACTLY one
    {ok: false, error: ...} object on stdout and exits non-zero."""
    _install_mock(monkeypatch, deploy_rc=1)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "hot-swap", "--target", "apps",
         "--llm-endpoint", "databricks-claude-opus-4-7", "--json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "hot-swap --target apps failed" in payload["error"]


def test_cli_hot_swap_default_target_still_model_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --target flag → model-serving path unchanged."""
    captured: dict[str, Any] = {}

    def fake_hot_swap(endpoint: str, model: str, wait: bool = True) -> Any:
        captured["endpoint"] = endpoint
        captured["model"] = model
        return SimpleNamespace(
            endpoint_name=endpoint,
            new_model=model,
            previous_model="prev-model",
            served_entities_updated=1,
        )

    monkeypatch.setattr("apx_agent._hot_swap.hot_swap_model", fake_hot_swap)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "hot-swap", "--endpoint", "ep", "--model", "new-model"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"endpoint": "ep", "model": "new-model"}


def test_cli_hot_swap_model_serving_requires_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "hot-swap", "--target", "model-serving", "--model", "x"],
    )
    assert result.exit_code != 0
    assert "--endpoint" in result.output

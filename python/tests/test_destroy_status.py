"""Tests for ``apx destroy`` and ``apx status``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apx_agent._deploy_state import ApxAppDeployState, utc_now_iso
from apx_agent.cli import main


_DATABRICKS_YML = """\
bundle:
  name: test-app

resources:
  apps:
    my-app:
      name: my-app
      description: test
      source_code_path: ./.build

targets:
  staging:
    mode: production
  dev:
    default: true
    mode: development
"""


_AGENT_PY = """\
class _StubAgent:
    def __init__(self) -> None:
        self._tool_fns = []
        self._sub_agent_urls = []

agent = _StubAgent()
"""


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "databricks.yml").write_text(_DATABRICKS_YML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-app"\n')
    (tmp_path / "agent.py").write_text(_AGENT_PY)
    server = tmp_path / "agent_server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent", None)
    return tmp_path


def test_destroy_runs_bundle_destroy_and_clears_state(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli_mod

    calls: list[list[str]] = []

    def fake(args: list[str], profile: str | None = None) -> _FakeProc:
        calls.append(list(args))
        return _FakeProc(0, stdout="ok\n")

    monkeypatch.setattr(cli_mod, "_run_databricks_cmd", fake)

    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_mod,
        "load_deploy_state",
        lambda ws, app, target: ApxAppDeployState(
            app_name=app,
            bundle_target=target,
            app_url="https://my-app.example.databricksapps.com",
            deployed_at=utc_now_iso(),
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "delete_deploy_state",
        lambda ws, app, target: deleted.append((app, target)),
    )
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda profile=None: MagicMock(),
    )

    runner = CliRunner()
    result = runner.invoke(main, [
        "destroy", "--bundle-target", "staging", "--yes", "--json-output",
    ])
    assert result.exit_code == 0, result.output
    assert any(c[:2] == ["bundle", "destroy"] for c in calls)
    assert ("my-app", "staging") in deleted
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["destroyed"] is True


def test_status_prints_state_or_empty(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli_mod

    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda profile=None: MagicMock(),
    )
    monkeypatch.setattr(cli_mod, "load_deploy_state", lambda *_a, **_k: None)

    runner = CliRunner()
    result = runner.invoke(main, ["status", "--bundle-target", "dev"])
    assert result.exit_code == 0, result.output
    assert "no deploy state" in result.output

    monkeypatch.setattr(
        cli_mod,
        "load_deploy_state",
        lambda ws, app, target: ApxAppDeployState(
            app_name=app,
            bundle_target=target,
            app_url="https://example.databricksapps.com",
            deployed_at="2026-07-28T00:00:00Z",
        ),
    )
    result2 = runner.invoke(main, ["status", "--json-output"])
    assert result2.exit_code == 0, result2.output
    payload = json.loads(result2.output.strip())
    assert payload["ok"] is True
    assert payload["state"]["app_url"] == "https://example.databricksapps.com"

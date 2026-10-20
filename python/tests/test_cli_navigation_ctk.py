"""CTK: local navigation commands do what their output claims, without network.

Each test drives the real Click group through CliRunner. Workspace-touching
commands are patched at the CLI boundary (``_require_sdk`` / ``_connect_workspace``)
so a missing Databricks profile cannot leak a network call into the assertion.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from click.testing import CliRunner

from apx_agent.cli import agents as agents_group
from apx_agent.cli import cli


def test_agents_init_scaffolds_yaml(tmp_path) -> None:
    runner = CliRunner()
    first = runner.invoke(cli, ["agents", "init", "--dir", str(tmp_path)])
    assert first.exit_code == 0, first.output
    text = (tmp_path / "agent.yaml").read_text()
    assert "name:" in text

    second = runner.invoke(cli, ["agents", "init", "--dir", str(tmp_path)])
    assert second.exit_code != 0
    assert "already exists" in second.output


def test_agents_doctor_warns(tmp_path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("name: bare\ninstructions: hello\ntools: []\n")
    result = CliRunner().invoke(cli, ["agents", "doctor", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "warn" in result.output
    assert "model" in result.output


def test_agents_doctor_empty_is_ok(tmp_path) -> None:
    result = CliRunner().invoke(cli, ["agents", "doctor", "--file", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_agents_models_lists_default() -> None:
    from apx_agent import cli as cli_mod

    result = CliRunner().invoke(cli, ["agents", "models"])
    assert result.exit_code == 0, result.output
    assert f"{cli_mod._DEFAULT_MODEL_CANDIDATES[0]} (default)" in result.output


def test_agents_models_profile_reports_what_the_workspace_serves(monkeypatch) -> None:
    """#955: with --profile, each shortlisted name shows the gateway's real answer."""
    from apx_agent import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_make_scaffold_workspace_client", lambda profile: object())
    monkeypatch.setattr(cli_mod, "_probe_model",
                        lambda ws, name: "served" if name.startswith("system.ai.") else "403 PERMISSION_DENIED")
    result = CliRunner().invoke(cli, ["agents", "models", "--profile", "p"])
    assert result.exit_code == 0, result.output
    assert "system.ai.claude-sonnet-4-6  served" in result.output
    assert "databricks-claude-sonnet-4-6  403 PERMISSION_DENIED" in result.output


def test_agents_playground_noninteractive(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: ping_agent\nmodel: databricks-claude-sonnet-4-5\n"
        "instructions: say ping\ntools: []\n"
    )
    monkeypatch.setattr("apx_agent.cli._is_interactive", lambda: False)
    result = CliRunner().invoke(cli, ["agents", "playground", str(path)])
    assert result.exit_code == 0, result.output
    assert "non-interactive" in result.output


def test_agents_test_flags_bad_yaml(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: broken\nmodel: databricks-claude-sonnet-4-5\ntools: []\n")
    failed = CliRunner().invoke(cli, ["agents", "test", str(bad)])
    assert failed.exit_code != 0
    assert "fail" in failed.output

    good = tmp_path / "good.yaml"
    good.write_text(
        "name: whole\nmodel: databricks-claude-sonnet-4-5\n"
        "instructions: do the thing\ntools: []\n"
    )
    passed = CliRunner().invoke(cli, ["agents", "test", str(good)])
    assert passed.exit_code == 0, passed.output
    assert "ok" in passed.output


def test_mcp_doctor_runs() -> None:
    result = CliRunner().invoke(cli, ["mcp", "doctor"])
    assert result.exit_code == 0, result.output
    assert "uvx" in result.output


def test_endpoints_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "apx_agent.cli._connect_workspace",
        lambda *_a, **_k: (MagicMock(), SimpleNamespace(host="https://h.example")),
    )
    result = CliRunner().invoke(cli, ["endpoints", "url", "my-ep"])
    assert result.exit_code == 0, result.output
    assert "https://h.example/serving-endpoints/my-ep/invocations" in result.output


def test_resources_list(tmp_path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: with_tool\nmodel: databricks-claude-sonnet-4-5\n"
        "instructions: look it up\n"
        "tools:\n  - {name: t, uc_function: cat.sch.fn}\n"
    )
    result = CliRunner().invoke(cli, ["resources", "list", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "cat.sch.fn" in result.output


def test_dashboard_open(monkeypatch) -> None:
    monkeypatch.setattr(
        "apx_agent.cli._connect_workspace",
        lambda *_a, **_k: (MagicMock(), SimpleNamespace(host="https://h.example")),
    )
    result = CliRunner().invoke(cli, ["dashboard-open", "abc123"])
    assert result.exit_code == 0, result.output
    assert "https://h.example/dashboardsv3/abc123" in result.output


def test_agents_why_hidden() -> None:
    assert agents_group.commands["why"].hidden is True
    known = CliRunner().invoke(cli, ["agents", "why", "create-team"])
    assert known.exit_code == 0, known.output
    assert "--name" in known.output
    unknown = CliRunner().invoke(cli, ["agents", "why", "nope"])
    assert unknown.exit_code != 0


def test_agents_show_missing(monkeypatch) -> None:
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = []
    ws.serving_endpoints.get.side_effect = Exception("not found")
    ws.registered_models.list.return_value = []
    monkeypatch.setattr("apx_agent.cli._require_sdk", lambda *_a, **_k: ws)
    result = CliRunner().invoke(cli, ["agents", "show", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output

"""Tests for cli.py — the apx command-line interface.

Covers:
  1. apx --help and `version` print without errors.
  2. apx scaffold creates the expected file tree.
  3. _parse_module_spec validates the MODULE:VARIABLE syntax.
  4. _load_agent imports a real module and returns the named attribute.
  5. apx mcp-config emits a JSON config snippet for a sample agent.
  6. apx publish-tools --dry-run lists annotated tools without writing.

Uses click.testing.CliRunner so we don't shell out.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apx_agent.cli import _load_agent, _parse_module_spec, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_agent_module(tmp_path: Path) -> None:
    """Write a minimal agent module that can be imported by _load_agent."""
    (tmp_path / "tmp_test_agent.py").write_text(textwrap.dedent("""
        from apx_agent import Agent, tool, genie_tool

        @tool(uc="main.tools.classify_intent", grant=["agent_consumers"])
        def classify_intent(query: str) -> str:
            \"\"\"Classify a customer query.\"\"\"
            return "billing" if "bill" in query.lower() else "other"

        agent = Agent(
            instructions="Triage customer queries.",
            tools=[classify_intent, genie_tool("space-abc-123")],
        )
    """))


# ---------------------------------------------------------------------------
# _parse_module_spec
# ---------------------------------------------------------------------------


def test_parse_module_spec_ok() -> None:
    assert _parse_module_spec("agent:agent") == ("agent", "agent")
    assert _parse_module_spec("pkg.mod:my_agent") == ("pkg.mod", "my_agent")


def test_parse_module_spec_rejects_missing_colon() -> None:
    import click

    with pytest.raises(click.BadParameter, match="MODULE:VARIABLE"):
        _parse_module_spec("agent")


def test_parse_module_spec_rejects_empty_segments() -> None:
    import click

    with pytest.raises(click.BadParameter, match="non-empty"):
        _parse_module_spec(":agent")
    with pytest.raises(click.BadParameter, match="non-empty"):
        _parse_module_spec("module:")


# ---------------------------------------------------------------------------
# _load_agent
# ---------------------------------------------------------------------------


def test_load_agent_imports_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    # _load_agent prepends cwd to sys.path itself
    agent = _load_agent("tmp_test_agent:agent")
    assert agent is not None
    # Cleanup: remove from sys.modules so subsequent tests get a fresh import
    sys.modules.pop("tmp_test_agent", None)


def test_load_agent_friendly_error_for_missing_module() -> None:
    import click

    with pytest.raises(click.ClickException, match="Failed to import"):
        _load_agent("definitely_does_not_exist_xyz:agent")


def test_load_agent_friendly_error_for_missing_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import click

    (tmp_path / "tmp_no_agent.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(click.ClickException, match="no attribute"):
        _load_agent("tmp_no_agent:agent")
    sys.modules.pop("tmp_no_agent", None)


# ---------------------------------------------------------------------------
# `apx --help` and `apx version`
# ---------------------------------------------------------------------------


def test_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "scaffold" in result.output
    assert "deploy" in result.output
    assert "publish" in result.output
    assert "mcp-config" in result.output


def test_version_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()  # some version string


# ---------------------------------------------------------------------------
# `apx scaffold`
# ---------------------------------------------------------------------------


def test_scaffold_creates_expected_files(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"
    for filename in ("pyproject.toml", "agent.py", "app.py", ".gitignore", "README.md"):
        assert (base / filename).exists(), f"missing {filename}"
    pyproject = (base / "pyproject.toml").read_text()
    assert 'name = "my_agent"' in pyproject
    assert "[tool.apx.agent]" in pyproject


def test_scaffold_refuses_overwrite_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "existing"
    target.mkdir()
    (target / "junk.txt").write_text("hi")

    result = runner.invoke(
        main,
        ["scaffold", "existing", "--dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_scaffold_overwrites_with_force(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "existing"
    target.mkdir()
    (target / "agent.py").write_text("# old content")

    result = runner.invoke(
        main,
        ["scaffold", "existing", "--dir", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0
    assert "# old content" not in (target / "agent.py").read_text()
    assert "from apx_agent import" in (target / "agent.py").read_text()


# ---------------------------------------------------------------------------
# `apx mcp-config`
# ---------------------------------------------------------------------------


def test_mcp_config_outputs_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mcp-config",
            "--module", "tmp_test_agent:agent",
            "--host", "https://workspace.cloud.databricks.com",
            "--name", "triage",
        ],
    )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert "mcpServers" in parsed
    # The uc_function and genie_space from the fixture agent show up
    keys = list(parsed["mcpServers"].keys())
    assert any("uc_function" in k for k in keys)
    assert any("genie_space" in k for k in keys)
    assert all(k.startswith("triage.") for k in keys)


# ---------------------------------------------------------------------------
# `apx publish-tools --dry-run`
# ---------------------------------------------------------------------------


def test_publish_tools_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["publish-tools", "--module", "tmp_test_agent:agent", "--dry-run"],
    )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "main.tools.classify_intent" in result.output
    assert "agent_consumers" in result.output  # the grant is printed


# ---------------------------------------------------------------------------
# `apx info`
# ---------------------------------------------------------------------------


def test_info_text_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["info", "--module", "tmp_test_agent:agent"])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert "Agent loaded from tmp_test_agent:agent" in result.output
    assert "classify_intent" in result.output
    assert "UC: main.tools.classify_intent" in result.output
    assert "agent_consumers" in result.output  # grant surfaced
    assert "Declared resources" in result.output
    assert "uc_function" in result.output
    assert "genie_space" in result.output


def test_info_json_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["info", "--module", "tmp_test_agent:agent", "--format", "json"],
    )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["module"] == "tmp_test_agent:agent"
    assert any(t["name"] == "classify_intent" for t in parsed["tools"])
    classify = next(t for t in parsed["tools"] if t["name"] == "classify_intent")
    assert classify["uc_name"] == "main.tools.classify_intent"
    assert classify["grants"] == ["agent_consumers"]
    kinds = {r["kind"] for r in parsed["resources"]}
    assert {"uc_function", "genie_space"}.issubset(kinds)


# ---------------------------------------------------------------------------
# `apx deploy --experiment` and pyproject fallback
# ---------------------------------------------------------------------------


def test_deploy_forwards_experiment_flag_to_log_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--experiment", "/Users/me/agents/x",
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert fake_log_agent.call_args.kwargs["experiment"] == "/Users/me/agents/x"


def test_deploy_falls_back_to_pyproject_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\n'
        'name = "x"\n'
        'experiment = "/Users/me/agents/x"\n'
    )
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                # no --experiment flag
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert fake_log_agent.call_args.kwargs["experiment"] == "/Users/me/agents/x"


def test_deploy_cli_flag_wins_over_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\n'
        'experiment = "/Users/me/agents/from-pyproject"\n'
    )
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--experiment", "/Users/me/agents/from-cli",
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0
    assert fake_log_agent.call_args.kwargs["experiment"] == "/Users/me/agents/from-cli"


def test_deploy_chains_publish_tools_and_set_uc_tags_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))
    fake_publish = MagicMock(return_value=[])
    fake_set_tags = MagicMock(return_value={})

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("apx_agent.publish_tools_to_uc", fake_publish), \
         patch("apx_agent.set_uc_tags_for_agent", fake_set_tags), \
         patch("mlflow.start_run"):
        result = runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    fake_publish.assert_called_once()
    fake_set_tags.assert_called_once()
    # agent_name defaults to short part of registered_model_name
    assert fake_set_tags.call_args.kwargs["name"] == "x"


def test_deploy_no_publish_tools_flag_skips_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))
    fake_publish = MagicMock(return_value=[])
    fake_set_tags = MagicMock(return_value={})

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("apx_agent.publish_tools_to_uc", fake_publish), \
         patch("apx_agent.set_uc_tags_for_agent", fake_set_tags), \
         patch("mlflow.start_run"):
        runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--no-deploy",
                "--no-publish-tools",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    fake_publish.assert_not_called()


def test_deploy_no_set_uc_tags_flag_skips_tag_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))
    fake_set_tags = MagicMock(return_value={})

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("apx_agent.publish_tools_to_uc", return_value=[]), \
         patch("apx_agent.set_uc_tags_for_agent", fake_set_tags), \
         patch("mlflow.start_run"):
        runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--no-deploy",
                "--no-set-uc-tags",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    fake_set_tags.assert_not_called()


def test_deploy_agent_name_flag_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_set_tags = MagicMock(return_value={})

    runner = CliRunner()
    with patch("apx_agent.log_agent",
               return_value=SimpleNamespace(registered_model_version="1")), \
         patch("apx_agent.publish_tools_to_uc", return_value=[]), \
         patch("apx_agent.set_uc_tags_for_agent", fake_set_tags), \
         patch("mlflow.start_run"):
        runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--agent-name", "explicit_name",
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert fake_set_tags.call_args.kwargs["name"] == "explicit_name"


def test_deploy_no_experiment_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--no-deploy",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert fake_log_agent.call_args.kwargs["experiment"] is None


# ---------------------------------------------------------------------------
# `apx watchdog violations` / `apx watchdog status`
# ---------------------------------------------------------------------------


def test_watchdog_violations_requires_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APX_WATCHDOG_VIOLATIONS_TABLE", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["watchdog", "violations"])
    assert result.exit_code != 0
    assert "table" in result.output.lower() or "table" in (result.stderr or "").lower()


def test_watchdog_violations_table_must_be_three_part() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["watchdog", "violations", "--table", "not_three_parts"],
    )
    assert result.exit_code != 0
    assert "three-part" in result.output


def test_watchdog_violations_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_WATCHDOG_VIOLATIONS_TABLE", "main.watchdog.violations")
    rows = [
        {
            "ts": "2026-05-19 08:00:00",
            "agent_name": "triage",
            "operation": "tool_call",
            "action": "reject",
            "reason": "PII access denied",
            "policy_id": "pii-001",
            "domain": "security",
            "context": "{}",
            "metadata": "{}",
        },
    ]

    runner = CliRunner()
    fake_ws_cls = MagicMock()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.run_sql", return_value=rows) as mock_sql:
        result = runner.invoke(main, ["watchdog", "violations"])

    assert result.exit_code == 0, result.output
    assert "main.watchdog.violations" in mock_sql.call_args.args[1]


def test_watchdog_violations_filters_by_agent_and_hours() -> None:
    runner = CliRunner()
    fake_ws_cls = MagicMock()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.run_sql", return_value=[]) as mock_sql:
        runner.invoke(main, [
            "watchdog", "violations",
            "--table", "main.watchdog.violations",
            "--agent", "triage",
            "--hours", "12",
            "--limit", "5",
        ])

    sql = mock_sql.call_args.args[1]
    assert "agent_name = 'triage'" in sql
    assert "INTERVAL 12 HOUR" in sql
    assert "LIMIT 5" in sql


def test_watchdog_violations_escapes_single_quotes_in_agent_name() -> None:
    runner = CliRunner()
    fake_ws_cls = MagicMock()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.run_sql", return_value=[]) as mock_sql:
        runner.invoke(main, [
            "watchdog", "violations",
            "--table", "main.watchdog.violations",
            "--agent", "user's-agent",
        ])

    sql = mock_sql.call_args.args[1]
    assert "user''s-agent" in sql


def test_watchdog_violations_json_output() -> None:
    rows = [
        {
            "ts": "2026-05-19 08:00:00",
            "agent_name": "triage",
            "action": "reject",
            "policy_id": "p-1",
        },
    ]

    runner = CliRunner()
    fake_ws_cls = MagicMock()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.run_sql", return_value=rows):
        result = runner.invoke(main, [
            "watchdog", "violations",
            "--table", "main.watchdog.violations",
            "--format", "json",
        ])

    parsed = json.loads(result.output)
    assert parsed[0]["agent_name"] == "triage"
    assert parsed[0]["policy_id"] == "p-1"


def test_watchdog_violations_no_rows_prints_helpful_message() -> None:
    runner = CliRunner()
    fake_ws_cls = MagicMock()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.run_sql", return_value=[]):
        result = runner.invoke(main, [
            "watchdog", "violations",
            "--table", "main.watchdog.violations",
        ])

    assert result.exit_code == 0
    assert "No violations matched" in result.output


def test_watchdog_status_requires_mcp_url_and_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APX_WATCHDOG_MCP_URL", raising=False)
    monkeypatch.delenv("APX_WATCHDOG_MCP_TOOL_NAME", raising=False)

    runner = CliRunner()
    result = runner.invoke(main, ["watchdog", "status"])
    assert result.exit_code != 0
    assert "mcp-url" in result.output.lower() or "APX_WATCHDOG_MCP_URL" in result.output


def test_watchdog_status_falls_back_to_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_WATCHDOG_MCP_URL", "https://watchdog.example.com/mcp")
    monkeypatch.setenv("APX_WATCHDOG_MCP_TOOL_NAME", "posture_status")

    fake_transport = MagicMock(return_value={
        "action": "allow",
        "reason": "no open violations",
        "policy_id": None,
        "domain": "security",
    })

    runner = CliRunner()
    with patch("apx_agent.make_mcp_transport", return_value=fake_transport):
        result = runner.invoke(main, ["watchdog", "status", "--agent", "triage"])

    assert result.exit_code == 0, result.output
    assert "allow" in result.output
    assert "no open violations" in result.output
    # The transport was invoked with our operation + context
    request = fake_transport.call_args.args[0]
    assert request["operation"] == "status"
    assert request["context"]["agent_name"] == "triage"


def test_watchdog_status_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_transport = MagicMock(return_value={
        "action": "reject",
        "reason": "PII tag missing",
        "policy_id": "p-7",
        "domain": "security",
        "metadata": {"owner": "data-team@x.com"},
    })

    runner = CliRunner()
    with patch("apx_agent.make_mcp_transport", return_value=fake_transport):
        result = runner.invoke(main, [
            "watchdog", "status",
            "--mcp-url", "https://watchdog.example.com/mcp",
            "--mcp-tool", "posture_status",
            "--agent", "triage",
            "--format", "json",
        ])

    parsed = json.loads(result.output)
    assert parsed["action"] == "reject"
    assert parsed["policy_id"] == "p-7"
    assert parsed["metadata"] == {"owner": "data-team@x.com"}


# ---------------------------------------------------------------------------
# `apx cost`
# ---------------------------------------------------------------------------


def test_cost_requires_agent_or_endpoint() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["cost"])
    assert result.exit_code != 0
    assert "Pass --agent" in result.output or "Pass --agent" in (result.stderr or "")


def test_cost_agent_and_endpoint_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--agent", "x", "--endpoint", "y"])
    assert result.exit_code != 0


def test_cost_text_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._cost import CostBreakdown

    fake_breakdown = CostBreakdown.from_rows(
        endpoint="customer_triage",
        lookback_hours=24,
        rows=[
            {"sku_name": "MODEL_SERVING", "usage_unit": "DBU", "dbus": 100.0, "usd": 7.5, "unit_price": 0.075},
            {"sku_name": "WAREHOUSE",     "usage_unit": "DBU", "dbus": 50.0,  "usd": 3.0, "unit_price": 0.06},
        ],
    )

    fake_ws_cls = MagicMock()
    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.cost_for_agent", return_value=fake_breakdown):
        result = runner.invoke(main, ["cost", "--agent", "customer_triage"])

    assert result.exit_code == 0, result.output
    assert "customer_triage" in result.output
    assert "MODEL_SERVING" in result.output
    assert "150.00" in result.output  # total DBUs
    assert "10.50" in result.output  # total USD


def test_cost_json_output() -> None:
    from apx_agent._cost import CostBreakdown

    fake_breakdown = CostBreakdown.from_rows(
        endpoint="triage",
        lookback_hours=12,
        rows=[{"sku_name": "X", "usage_unit": "DBU", "dbus": 5.0, "usd": 1.0, "unit_price": 0.2}],
    )

    fake_ws_cls = MagicMock()
    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.cost_for_agent", return_value=fake_breakdown):
        result = runner.invoke(main, [
            "cost", "--endpoint", "triage", "--hours", "12", "--format", "json",
        ])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["endpoint"] == "triage"
    assert parsed["lookback_hours"] == 12
    assert parsed["total_dbus"] == 5.0
    assert parsed["total_usd"] == 1.0


def test_cost_no_usage_rows_prints_helpful_message() -> None:
    from apx_agent._cost import CostBreakdown

    empty = CostBreakdown(endpoint="triage", lookback_hours=24)
    fake_ws_cls = MagicMock()
    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", fake_ws_cls), \
         patch("apx_agent.cost_for_agent", return_value=empty):
        result = runner.invoke(main, ["cost", "--agent", "triage"])

    assert result.exit_code == 0
    assert "No usage rows" in result.output


# ---------------------------------------------------------------------------
# `apx trace`
# ---------------------------------------------------------------------------


def test_trace_requires_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no pyproject -> no experiment fallback
    runner = CliRunner()
    result = runner.invoke(main, ["trace"])
    assert result.exit_code != 0
    assert "experiment" in result.output.lower()


def test_trace_falls_back_to_pyproject_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\nexperiment = "/Users/me/agents/triage"\n'
    )
    monkeypatch.chdir(tmp_path)

    fake_search = MagicMock(return_value=[])
    runner = CliRunner()
    with patch("mlflow.search_traces", fake_search):
        result = runner.invoke(main, ["trace"])

    assert result.exit_code == 0, result.output
    assert fake_search.call_args.kwargs["experiment_names"] == [
        "/Users/me/agents/triage"
    ]


def test_trace_filters_by_agent_and_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_search = MagicMock(return_value=[])

    runner = CliRunner()
    with patch("mlflow.search_traces", fake_search):
        runner.invoke(main, [
            "trace",
            "--experiment", "/Users/me/agents/triage",
            "--agent", "triage",
            "--operation", "tool_call",
            "--limit", "5",
        ])

    fs = fake_search.call_args.kwargs["filter_string"]
    assert "apx.agent.name" in fs
    assert "triage" in fs
    assert "apx.operation" in fs
    assert "tool_call" in fs
    assert fake_search.call_args.kwargs["max_results"] == 5


def test_trace_prints_rows_from_dataframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    # Simulate the DataFrame return path
    fake_df = SimpleNamespace(
        to_dict=lambda orient: [
            {
                "trace_id": "trace-1",
                "tags": {"apx.agent.name": "triage", "apx.operation": "predict"},
                "status": "OK",
                "execution_time_ms": 123,
            },
            {
                "trace_id": "trace-2",
                "tags": {"apx.agent.name": "billing", "apx.operation": "tool_call"},
                "status": "OK",
                "execution_time_ms": 45,
            },
        ],
    )
    runner = CliRunner()
    with patch("mlflow.search_traces", return_value=fake_df):
        result = runner.invoke(main, [
            "trace",
            "--experiment", "/Users/me/agents/triage",
        ])

    assert result.exit_code == 0
    assert "trace-1" in result.output
    assert "trace-2" in result.output
    assert "triage" in result.output


def test_trace_json_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    fake_df = SimpleNamespace(
        to_dict=lambda orient: [
            {
                "trace_id": "trace-1",
                "tags": {"apx.agent.name": "triage"},
                "status": "OK",
                "execution_time_ms": 100,
            },
        ],
    )
    runner = CliRunner()
    with patch("mlflow.search_traces", return_value=fake_df):
        result = runner.invoke(main, [
            "trace",
            "--experiment", "/Users/me/agents/x",
            "--format", "json",
        ])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed[0]["trace_id"] == "trace-1"
    assert parsed[0]["agent_name"] == "triage"


# ---------------------------------------------------------------------------
# `apx test`
# ---------------------------------------------------------------------------


def test_test_requires_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["test", "--module", "tmp_test_agent:agent"])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    assert "model" in result.output.lower()


def test_test_runs_prompts_and_reports_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Fake compiled agent that returns a stub response
    from types import SimpleNamespace as NS

    fake_chat = MagicMock()
    fake_chat.predict.return_value = NS(messages=[
        NS(role="assistant", content="ok response"),
    ])

    runner = CliRunner()
    with patch("apx_agent.compile_to_chat_agent", return_value=fake_chat):
        result = runner.invoke(main, [
            "test",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--prompt", "hello there",
            "--prompt", "what is the lineage of main.sales.orders?",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert "hello there" in result.output
    assert "ok response" in result.output
    assert "2/2 prompts passed" in (result.output + result.stderr)
    assert fake_chat.predict.call_count == 2


def test_test_exits_non_zero_on_predict_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_chat = MagicMock()
    fake_chat.predict.side_effect = RuntimeError("compile blew up")

    runner = CliRunner()
    with patch("apx_agent.compile_to_chat_agent", return_value=fake_chat):
        result = runner.invoke(main, [
            "test",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--prompt", "hi",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    assert "FAIL" in (result.output + result.stderr)


def test_test_default_prompt_when_none_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_agent_module(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\nmodel = "databricks-claude-sonnet-4-6"\n'
    )
    monkeypatch.chdir(tmp_path)

    from types import SimpleNamespace as NS
    fake_chat = MagicMock()
    fake_chat.predict.return_value = NS(messages=[NS(role="assistant", content="ok")])

    runner = CliRunner()
    with patch("apx_agent.compile_to_chat_agent", return_value=fake_chat):
        result = runner.invoke(main, ["test", "--module", "tmp_test_agent:agent"])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0
    assert fake_chat.predict.call_count == 1


# ---------------------------------------------------------------------------
# `apx list`
# ---------------------------------------------------------------------------


def _make_tagged_model(
    *,
    name: str,
    catalog: str = "main",
    schema: str = "agents",
    tags: dict[str, str] | None = None,
) -> Any:
    tag_objs = [SimpleNamespace(key=k, value=v) for k, v in (tags or {}).items()]
    return SimpleNamespace(
        name=name,
        catalog_name=catalog,
        schema_name=schema,
        full_name=f"{catalog}.{schema}.{name}",
        tags=tag_objs,
    )


def test_list_schema_requires_catalog() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["list", "--schema", "agents"])
    assert result.exit_code != 0
    assert "--catalog" in result.output


def test_list_prints_apx_tagged_models_only() -> None:
    fake_ws = MagicMock()
    fake_ws.registered_models.list.return_value = [
        _make_tagged_model(
            name="triage",
            tags={
                "apx.agent.name": "customer_triage",
                "apx.agent.model": "databricks-claude-sonnet-4-6",
                "apx.agent.tool_count": "5",
                "apx.agent.metadata": json.dumps({"resources": [{"kind": "uc_function", "identifier": "main.tools.x"}]}),
            },
        ),
        _make_tagged_model(name="not_apx", tags={"other.tag": "value"}),
        _make_tagged_model(
            name="billing",
            tags={"apx.agent.name": "billing", "apx.agent.model": "claude"},
        ),
    ]

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["list"])

    assert result.exit_code == 0, result.output
    assert "customer_triage" in result.output
    assert "billing" in result.output
    assert "not_apx" not in result.output


def test_list_json_format() -> None:
    fake_ws = MagicMock()
    fake_ws.registered_models.list.return_value = [
        _make_tagged_model(
            name="triage",
            tags={
                "apx.agent.name": "customer_triage",
                "apx.agent.model": "databricks-claude-sonnet-4-6",
                "apx.agent.metadata": json.dumps({"resources": [{"kind": "uc_function", "identifier": "main.tools.x"}, {"kind": "genie_space", "identifier": "abc"}]}),
            },
        ),
    ]

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["list", "--format", "json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed[0]["agent_name"] == "customer_triage"
    assert parsed[0]["resource_count"] == 2


def test_list_no_tagged_models_prints_helpful_message() -> None:
    fake_ws = MagicMock()
    fake_ws.registered_models.list.return_value = [
        _make_tagged_model(name="not_apx", tags={"other": "x"}),
    ]

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["list"])

    assert result.exit_code == 0
    assert "No apx-tagged" in result.output


# ---------------------------------------------------------------------------
# `apx logs`
# ---------------------------------------------------------------------------


def _fake_endpoint_with_served_model(name: str) -> object:
    return SimpleNamespace(
        config=SimpleNamespace(
            served_entities=[SimpleNamespace(name=name)],
        )
    )


def test_logs_requires_endpoint_or_app() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert result.exit_code != 0
    assert "either --endpoint" in result.output or "either --endpoint" in (result.stderr or "")


def test_logs_endpoint_and_app_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--endpoint", "x", "--app", "y"])
    assert result.exit_code != 0


def test_logs_endpoint_auto_discovers_served_model_and_prints_runtime_logs() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.get.return_value = _fake_endpoint_with_served_model(
        "customer_triage-1"
    )
    fake_ws.serving_endpoints.logs.return_value = SimpleNamespace(
        logs="2026-05-18 12:00:00 [INFO] healthy\n"
    )

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["logs", "--endpoint", "customer_triage"])

    assert result.exit_code == 0, result.output
    assert "healthy" in result.output
    fake_ws.serving_endpoints.get.assert_called_once_with("customer_triage")
    fake_ws.serving_endpoints.logs.assert_called_once_with(
        name="customer_triage", served_model_name="customer_triage-1",
    )


def test_logs_build_flag_calls_build_logs() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.get.return_value = _fake_endpoint_with_served_model("triage-1")
    fake_ws.serving_endpoints.build_logs.return_value = SimpleNamespace(
        logs="Building model...\n"
    )

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["logs", "--endpoint", "triage", "--build"])

    assert result.exit_code == 0, result.output
    assert "Building" in result.output
    fake_ws.serving_endpoints.build_logs.assert_called_once()
    fake_ws.serving_endpoints.logs.assert_not_called()


def test_logs_explicit_served_model_skips_discovery() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.logs.return_value = SimpleNamespace(logs="ok\n")

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(
            main,
            ["logs", "--endpoint", "triage", "--served-model", "triage-2"],
        )

    assert result.exit_code == 0
    fake_ws.serving_endpoints.get.assert_not_called()
    fake_ws.serving_endpoints.logs.assert_called_once_with(
        name="triage", served_model_name="triage-2",
    )


def test_logs_endpoint_without_served_models_errors() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.get.return_value = SimpleNamespace(
        config=SimpleNamespace(served_entities=[])
    )

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(main, ["logs", "--endpoint", "triage"])

    assert result.exit_code != 0
    assert "no served models" in result.output


def test_logs_app_shells_out_to_databricks_cli() -> None:
    runner = CliRunner()
    fake_completed = SimpleNamespace(
        returncode=0,
        stdout="app log line 1\napp log line 2\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as mock_run:
        result = runner.invoke(main, ["logs", "--app", "my-app", "--profile", "prod"])

    assert result.exit_code == 0, result.output
    assert "app log line 1" in result.output
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["databricks", "apps", "logs"]
    assert "my-app" in cmd
    assert "--profile" in cmd
    assert "prod" in cmd


def test_logs_app_surfaces_databricks_cli_failure() -> None:
    runner = CliRunner()
    fake_completed = SimpleNamespace(returncode=2, stdout="", stderr="app not found\n")
    with patch("subprocess.run", return_value=fake_completed):
        result = runner.invoke(main, ["logs", "--app", "missing-app"])

    assert result.exit_code != 0
    assert "app not found" in result.output


def test_logs_app_friendly_error_when_databricks_cli_missing() -> None:
    runner = CliRunner()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = runner.invoke(main, ["logs", "--app", "x"])

    assert result.exit_code != 0
    assert "databricks" in result.output.lower()


def test_publish_tools_no_uc_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tmp_empty.py").write_text(textwrap.dedent("""
        from apx_agent import Agent

        def plain(x: str) -> str:
            \"\"\"plain\"\"\"
            return x

        agent = Agent(instructions="...", tools=[plain])
    """))
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["publish-tools", "--module", "tmp_empty:agent", "--dry-run"],
    )
    sys.modules.pop("tmp_empty", None)

    assert result.exit_code == 0
    assert "No @tool(uc=...) decorated tools found." in result.output


# ---------------------------------------------------------------------------
# `apx memory ...` and `apx examples ...`
# ---------------------------------------------------------------------------


_STORE_MODULE_NAME = "tmp_store_fixture"


def _write_store_module(tmp_path: Path) -> None:
    """Drop a module exporting seeded MemoryStore + ExampleStore fixtures.

    The module exposes ``mem_store`` and ``ex_store`` — both InMemory
    backends pre-seeded with rows scoped to principal/agent
    ``"alice"``/``"triage"`` respectively. Tests point ``--store-module``
    at ``tmp_store_fixture:mem_store`` or ``...:ex_store``.
    """
    (tmp_path / f"{_STORE_MODULE_NAME}.py").write_text(textwrap.dedent("""
        from apx_agent import InMemoryMemoryStore, InMemoryExampleStore

        mem_store = InMemoryMemoryStore()
        for content, tags in [
            ("alice likes window seats", ("preference",)),
            ("alice is vegetarian", ("preference", "diet")),
            ("alice flew BOS->SFO on 2026-01-12", ("episodic",)),
        ]:
            mem_store.add({
                "principal_id": "alice",
                "content": content,
                "tags": list(tags),
                "namespace": "profile",
            })

        ex_store = InMemoryExampleStore()
        for query, answer, intent in [
            ("what's my next flight?", "Checking your itinerary...", "lookup"),
            ("change my seat to a window", "Updated to 14A.", "modify"),
            ("cancel my booking", "Booking cancelled, refund issued.", "modify"),
        ]:
            ex_store.add({
                "agent_id": "triage",
                "input": query,
                "output": answer,
                "intent": intent,
            })
    """))


def _cleanup_store_module() -> None:
    sys.modules.pop(_STORE_MODULE_NAME, None)


# --- memory ---------------------------------------------------------------


def test_memory_remember_then_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "remember",
            "--principal-id", "alice",
            "--content", "alice prefers aisle on red-eyes",
            "--namespace", "profile",
            "--tags", "preference,seating",
            "--importance", "0.8",
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert result.exit_code == 0, result.output
        new = json.loads(result.output)
        assert new["principal_id"] == "alice"
        assert new["importance"] == 0.8
        assert set(new["tags"]) == {"preference", "seating"}
        assert new["id"].startswith("mem_")
        new_id = new["id"]

        # list should include the new row plus the 3 seeded
        result2 = runner.invoke(main, [
            "memory", "list",
            "--principal-id", "alice",
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert result2.exit_code == 0, result2.output
        rows = json.loads(result2.output)
        ids = {r["id"] for r in rows}
        assert new_id in ids
        assert len(rows) == 4
    finally:
        _cleanup_store_module()


def test_memory_recall_returns_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "recall",
            "--principal-id", "alice",
            "--query", "what does alice like to eat?",
            "-k", "2",
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) <= 2
        assert all("memory" in r and "score" in r for r in rows)
        assert all(r["memory"]["principal_id"] == "alice" for r in rows)
    finally:
        _cleanup_store_module()


def test_memory_recall_text_format_is_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "recall",
            "--principal-id", "alice",
            "--query", "alice",
            "--format", "text",
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert result.exit_code == 0, result.output
        # markdown bullets per task spec
        assert "# memory recall" in result.output
        assert "- [" in result.output
    finally:
        _cleanup_store_module()


def test_memory_forget_existing_then_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        # First, get an id from the seeded list
        list_res = runner.invoke(main, [
            "memory", "list",
            "--principal-id", "alice",
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        rows = json.loads(list_res.output)
        target_id = rows[0]["id"]

        ok_res = runner.invoke(main, [
            "memory", "forget",
            "--id", target_id,
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert ok_res.exit_code == 0, ok_res.output
        assert json.loads(ok_res.output) == {"deleted": target_id}

        # Forgetting again should fail with non-zero exit
        miss_res = runner.invoke(main, [
            "memory", "forget",
            "--id", target_id,
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert miss_res.exit_code != 0
    finally:
        _cleanup_store_module()


def test_memory_missing_required_flag_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "recall",
            # missing --principal-id and --query
            "--store-module", f"{_STORE_MODULE_NAME}:mem_store",
        ])
        assert result.exit_code != 0
        # click prints "Missing option" for required flags
        assert "Missing option" in (result.output + (result.stderr or ""))
    finally:
        _cleanup_store_module()


def test_memory_store_module_load_failure_friendly_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, [
        "memory", "list",
        "--principal-id", "x",
        "--store-module", "nonexistent_module_xyz:store",
    ])
    assert result.exit_code != 0
    assert "Failed to import" in result.output


def test_memory_pyproject_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\n'
        f'memory_store = "{_STORE_MODULE_NAME}:mem_store"\n'
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        # No --store-module flag — should pick up from pyproject
        result = runner.invoke(main, [
            "memory", "list",
            "--principal-id", "alice",
        ])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 3
    finally:
        _cleanup_store_module()


def test_memory_no_store_configured_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, [
        "memory", "list",
        "--principal-id", "alice",
    ])
    assert result.exit_code != 0
    assert "memory_store" in result.output or "--store-module" in result.output


# --- examples -------------------------------------------------------------


def test_examples_save_then_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        save_res = runner.invoke(main, [
            "examples", "save",
            "--agent-id", "triage",
            "--input", "how do I rebook a missed flight?",
            "--output", "I can rebook you onto the next available departure.",
            "--intent", "lookup",
            "--score", "0.9",
            "--tags", "support,vip",
            "--store-module", f"{_STORE_MODULE_NAME}:ex_store",
        ])
        assert save_res.exit_code == 0, save_res.output
        saved = json.loads(save_res.output)
        assert saved["agent_id"] == "triage"
        assert saved["score"] == 0.9

        find_res = runner.invoke(main, [
            "examples", "find",
            "--agent-id", "triage",
            "--query", "modify a booking",
            "-k", "5",
            "--store-module", f"{_STORE_MODULE_NAME}:ex_store",
        ])
        assert find_res.exit_code == 0, find_res.output
        rows = json.loads(find_res.output)
        assert all("example" in r and "score" in r for r in rows)
        # at least the seeded 3 + the new 1 are accessible
        assert len(rows) >= 1
    finally:
        _cleanup_store_module()


def test_examples_list_text_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "list",
            "--agent-id", "triage",
            "--format", "text",
            "--store-module", f"{_STORE_MODULE_NAME}:ex_store",
        ])
        assert result.exit_code == 0, result.output
        assert "# examples list" in result.output
        # markdown-style bullets
        assert "- (" in result.output
    finally:
        _cleanup_store_module()


def test_examples_remove_missing_id_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "remove",
            "--id", "ex_does_not_exist",
            "--store-module", f"{_STORE_MODULE_NAME}:ex_store",
        ])
        assert result.exit_code != 0
    finally:
        _cleanup_store_module()


def test_examples_list_filter_by_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_store_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "list",
            "--agent-id", "triage",
            "--intent", "modify",
            "--store-module", f"{_STORE_MODULE_NAME}:ex_store",
        ])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        # seeded: 2 with intent=modify
        assert len(rows) == 2
        assert all(r["intent"] == "modify" for r in rows)
    finally:
        _cleanup_store_module()


# ---------------------------------------------------------------------------
# `apx examples mine` and `apx memory consolidate`
# ---------------------------------------------------------------------------
#
# These subcommands wrap mine_examples / consolidate_memories with file-
# injectable stores and summarizer callables. We write a per-test fixture
# module to ``tmp_path`` exposing seeded stores + callables; tests point
# the relevant ``--*-fn`` / ``--*-store`` flags at it.

_MINE_FIXTURE_NAME = "tmp_mine_fixture"


def _write_mine_fixture(tmp_path: Path) -> None:
    """Drop a module exporting seeded SessionStore + ExampleStore + callables.

    The session store carries three sessions:
        s1: 2 user→assistant turns (one short, one longer)
        s2: 1 turn, with a leading tool message that should be skipped
        s3: 1 turn whose assistant content is "skip" — used by filter_fn tests
    """
    (tmp_path / f"{_MINE_FIXTURE_NAME}.py").write_text(textwrap.dedent("""
        from apx_agent import InMemorySessionStore, InMemoryExampleStore
        from apx_agent._session import Session

        sess_store = InMemorySessionStore()
        sess_store.put(Session(
            session_id="s1",
            history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "how are you?"},
                {"role": "assistant", "content": "doing well, thanks"},
            ],
        ))
        sess_store.put(Session(
            session_id="s2",
            history=[
                {"role": "user", "content": "compute pi"},
                {"role": "assistant", "content": ""},  # tool-call only
                {"role": "tool", "content": "3.14"},
                {"role": "assistant", "content": "pi is about 3.14"},
            ],
        ))
        sess_store.put(Session(
            session_id="s3",
            history=[
                {"role": "user", "content": "noise"},
                {"role": "assistant", "content": "skip"},
            ],
        ))

        ex_store = InMemoryExampleStore()

        def intent_fn(turn):
            content = turn.user_message.get("content", "")
            return "greet" if "hello" in content else "other"

        def score_fn(turn):
            return 0.9

        def filter_fn(turn):
            # Drop the "skip" turn from s3.
            return turn.assistant_message.get("content") != "skip"

        def tags_fn(turn):
            return ("mined",)

        def metadata_fn(turn):
            return {"source": "test"}
    """))


def _cleanup_mine_fixture() -> None:
    sys.modules.pop(_MINE_FIXTURE_NAME, None)


# --- examples mine -------------------------------------------------------


def test_examples_mine_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "mine",
            "--session-store", f"{_MINE_FIXTURE_NAME}:sess_store",
            "--example-store", f"{_MINE_FIXTURE_NAME}:ex_store",
            "--agent-id", "triage",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # s1 has 2 turns, s2 has 1 real turn (assistant-with-content), s3 has 1
        # default filter drops s2's empty assistant and the s3 "skip" passes
        # the default filter (it's a non-empty string).
        assert payload["sessions_scanned"] == 3
        assert payload["examples_added"] >= 3
        assert payload["dry_run"] is False
        assert all(e["agent_id"] == "triage" for e in payload["examples"])
    finally:
        _cleanup_mine_fixture()


def test_examples_mine_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "mine",
            "--session-store", f"{_MINE_FIXTURE_NAME}:sess_store",
            "--example-store", f"{_MINE_FIXTURE_NAME}:ex_store",
            "--agent-id", "triage",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["examples_added"] == 0
        assert len(payload["examples"]) >= 1  # materialized client-side

        # Re-list the store: nothing should have been persisted.
        list_res = runner.invoke(main, [
            "examples", "list",
            "--agent-id", "triage",
            "--store-module", f"{_MINE_FIXTURE_NAME}:ex_store",
        ])
        assert list_res.exit_code == 0
        rows = json.loads(list_res.output)
        assert rows == []
    finally:
        _cleanup_mine_fixture()


def test_examples_mine_resolves_callables_from_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "mine",
            "--session-store", f"{_MINE_FIXTURE_NAME}:sess_store",
            "--example-store", f"{_MINE_FIXTURE_NAME}:ex_store",
            "--agent-id", "triage",
            "--intent-fn", f"{_MINE_FIXTURE_NAME}:intent_fn",
            "--score-fn", f"{_MINE_FIXTURE_NAME}:score_fn",
            "--filter-fn", f"{_MINE_FIXTURE_NAME}:filter_fn",
            "--tags-fn", f"{_MINE_FIXTURE_NAME}:tags_fn",
            "--metadata-fn", f"{_MINE_FIXTURE_NAME}:metadata_fn",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # filter_fn drops s3 "skip"; intent_fn flips "hello" → "greet"
        intents = {e["intent"] for e in payload["examples"]}
        assert "greet" in intents
        assert all(e["score"] == 0.9 for e in payload["examples"])
        assert all("mined" in e["tags"] for e in payload["examples"])
        assert all(e["metadata"].get("source") == "test"
                   for e in payload["examples"])
        # filter_fn dropped the "skip" turn
        assert all(e["output"] != "skip" for e in payload["examples"])
    finally:
        _cleanup_mine_fixture()


def test_examples_mine_resolves_callables_from_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\n'
        f'session_store = "{_MINE_FIXTURE_NAME}:sess_store"\n'
        f'example_store = "{_MINE_FIXTURE_NAME}:ex_store"\n'
        f'intent_fn = "{_MINE_FIXTURE_NAME}:intent_fn"\n'
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        # No --session-store, --example-store, or --intent-fn — all from pyproject.
        result = runner.invoke(main, [
            "examples", "mine",
            "--agent-id", "triage",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        intents = {e["intent"] for e in payload["examples"]}
        # intent_fn from pyproject is the same one as the flag test
        assert "greet" in intents
    finally:
        _cleanup_mine_fixture()


def test_examples_mine_session_ids_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "mine",
            "--session-store", f"{_MINE_FIXTURE_NAME}:sess_store",
            "--example-store", f"{_MINE_FIXTURE_NAME}:ex_store",
            "--agent-id", "triage",
            "--session-ids", "s1",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["sessions_scanned"] == 1
        # s1 has 2 pairs
        assert len(payload["examples"]) == 2
    finally:
        _cleanup_mine_fixture()


def test_examples_mine_text_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mine_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "examples", "mine",
            "--session-store", f"{_MINE_FIXTURE_NAME}:sess_store",
            "--example-store", f"{_MINE_FIXTURE_NAME}:ex_store",
            "--agent-id", "triage",
            "--format", "text",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "Mined" in result.output
        assert "examples from" in result.output
        assert "sessions" in result.output
        assert "turns considered" in result.output
    finally:
        _cleanup_mine_fixture()


# --- memory consolidate ---------------------------------------------------


_CONS_FIXTURE_NAME = "tmp_consolidate_fixture"


def _write_consolidate_fixture(tmp_path: Path, n: int = 6) -> None:
    """Drop a module exporting a seeded MemoryStore + summarize callable."""
    (tmp_path / f"{_CONS_FIXTURE_NAME}.py").write_text(textwrap.dedent(f"""
        from apx_agent import InMemoryMemoryStore

        mem_store = InMemoryMemoryStore()
        for i in range({n}):
            mem_store.add({{
                "principal_id": "alice",
                "content": f"memory-{{i}}",
                "namespace": "profile",
            }})

        def summarize_fn(mems):
            return "alice has " + str(len(mems)) + " memories"
    """))


def _cleanup_consolidate_fixture() -> None:
    sys.modules.pop(_CONS_FIXTURE_NAME, None)


def test_memory_consolidate_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            "--summarize-fn", f"{_CONS_FIXTURE_NAME}:summarize_fn",
            "--min-memories-for-consolidation", "5",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["candidates_found"] == 6
        assert payload["consolidated_memory"]["content"] == "alice has 6 memories"
        assert payload["consolidated_memory"]["namespace"] == "consolidated"
        assert len(payload["deleted_ids"]) == 6
        assert payload["dry_run"] is False
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            "--summarize-fn", f"{_CONS_FIXTURE_NAME}:summarize_fn",
            "--min-memories-for-consolidation", "5",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["consolidated_memory"] is not None
        assert payload["consolidated_memory"]["content"] == "alice has 6 memories"
        assert payload["deleted_ids"] == []

        # Originals must still be in the store.
        list_res = runner.invoke(main, [
            "memory", "list",
            "--principal-id", "alice",
            "--store-module", f"{_CONS_FIXTURE_NAME}:mem_store",
        ])
        assert list_res.exit_code == 0
        rows = json.loads(list_res.output)
        assert len(rows) == 6
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_keep_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            "--summarize-fn", f"{_CONS_FIXTURE_NAME}:summarize_fn",
            "--min-memories-for-consolidation", "5",
            "--keep-originals",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["deleted_ids"] == []

        list_res = runner.invoke(main, [
            "memory", "list",
            "--principal-id", "alice",
            "--store-module", f"{_CONS_FIXTURE_NAME}:mem_store",
        ])
        rows = json.loads(list_res.output)
        # 6 originals + 1 consolidated row
        assert len(rows) == 7
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_below_threshold_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=3)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            "--summarize-fn", f"{_CONS_FIXTURE_NAME}:summarize_fn",
            "--min-memories-for-consolidation", "5",
        ])
        assert result.exit_code != 0
        # The JSON / text body for below-threshold lands on stderr per
        # the spec (caller wants a non-zero exit). click.testing combines
        # stdout/stderr into `result.output` by default.
        combined = result.output
        assert "below" in combined or "3" in combined
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_text_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            "--summarize-fn", f"{_CONS_FIXTURE_NAME}:summarize_fn",
            "--min-memories-for-consolidation", "5",
            "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert "Consolidated 6 memories" in result.output
        assert "mem_" in result.output
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_pyproject_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\n'
        f'memory_store = "{_CONS_FIXTURE_NAME}:mem_store"\n'
        f'summarize_fn = "{_CONS_FIXTURE_NAME}:summarize_fn"\n'
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        # No --store, no --summarize-fn — both from pyproject.
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--principal-id", "alice",
            "--min-memories-for-consolidation", "5",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["consolidated_memory"]["content"] == "alice has 6 memories"
    finally:
        _cleanup_consolidate_fixture()


def test_memory_consolidate_requires_summarize_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_consolidate_fixture(tmp_path, n=6)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(main, [
            "memory", "consolidate",
            "--store", f"{_CONS_FIXTURE_NAME}:mem_store",
            "--principal-id", "alice",
            # No --summarize-fn flag, no pyproject — must error.
        ])
        assert result.exit_code != 0
        assert "summarize-fn" in result.output or "summarize_fn" in result.output
    finally:
        _cleanup_consolidate_fixture()


# ---------------------------------------------------------------------------
# `apx deploy` — env-var capture + secret-scan
# ---------------------------------------------------------------------------


_ENV_KEY = "MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING"


def _make_env_capturing_log_agent(captured: dict) -> MagicMock:
    """Build a log_agent mock that snapshots os.environ at call time."""
    def _capture(*args, **kwargs):  # noqa: ANN001 — match log_agent signature loosely
        captured["mlflow_env"] = os.environ.get(_ENV_KEY)
        return SimpleNamespace(registered_model_version="1")
    return MagicMock(side_effect=_capture)


def test_deploy_no_capture_env_vars_default_sets_mlflow_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (no flag) must set MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    captured: dict = {}
    fake_log_agent = _make_env_capturing_log_agent(captured)

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    # During the log_agent call, the kill switch was on.
    assert captured["mlflow_env"] == "false"
    # After the deploy returns, the caller's env is unchanged (var absent).
    assert _ENV_KEY not in os.environ
    # And the honest-output status line printed.
    assert "env-var-capture=off" in result.output
    assert "secrets-scan=on" in result.output


def test_deploy_capture_env_vars_flag_lets_mlflow_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--capture-env-vars must NOT set the kill switch."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    captured: dict = {}
    fake_log_agent = _make_env_capturing_log_agent(captured)

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--capture-env-vars",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    # The guard did NOT touch the env, so during the call the var is still absent.
    assert captured["mlflow_env"] is None
    assert _ENV_KEY not in os.environ
    assert "env-var-capture=on" in result.output


def test_deploy_env_var_guard_restores_preexisting_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING was already set, restore it."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Pre-existing value the user had in their shell.
    monkeypatch.setenv(_ENV_KEY, "true")

    captured: dict = {}
    fake_log_agent = _make_env_capturing_log_agent(captured)

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert captured["mlflow_env"] == "false"
    # Caller's "true" must be restored exactly.
    assert os.environ.get(_ENV_KEY) == "true"


def test_deploy_allow_env_var_requires_capture_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--allow-env-var X without --capture-env-vars must error loudly."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--allow-env-var", "DATABRICKS_HOST",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    # Click renders BadParameter via stderr-merged output.
    assert "allow-env-var" in result.output.lower()
    fake_log_agent.assert_not_called()


def test_deploy_allow_env_var_with_capture_flag_combines_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--capture-env-vars + repeated --allow-env-var prints the all-or-nothing warning."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--capture-env-vars",
            "--allow-env-var", "DATABRICKS_HOST",
            "--allow-env-var", "DATABRICKS_TOKEN",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    # All-or-nothing warning fired and both vars are listed.
    assert "all-or-nothing" in result.output
    assert "DATABRICKS_HOST" in result.output
    assert "DATABRICKS_TOKEN" in result.output


def test_deploy_secret_scan_warns_on_secret_referenced_in_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *_KEY referenced via os.environ in the project must appear in the warning."""
    _write_agent_module(tmp_path)
    # Plant a source file that references a clearly-secret-looking env var.
    (tmp_path / "leaky.py").write_text(
        'import os\n'
        'token = os.environ["ATLASSIAN_API_KEY"]\n'
        'gem = os.getenv("GEMINI_API_KEY")\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    # Capture is off, so we just surface the note (no prompt).
    assert "ATLASSIAN_API_KEY" in result.output
    assert "GEMINI_API_KEY" in result.output


def test_deploy_secret_scan_prompts_when_capture_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With capture ON and secret-looking refs, deploy must prompt unless --yes."""
    _write_agent_module(tmp_path)
    (tmp_path / "leaky.py").write_text(
        'import os\n'
        'tok = os.environ["ATLASSIAN_API_KEY"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        # Respond "n" → abort.
        result_n = runner.invoke(
            main,
            [
                "deploy",
                "--module", "tmp_test_agent:agent",
                "--model", "databricks-claude-sonnet-4-6",
                "--name", "main.agents.x",
                "--capture-env-vars",
                "--no-deploy",
            ],
            input="n\n",
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result_n.exit_code != 0  # aborted
    fake_log_agent.assert_not_called()
    assert "ATLASSIAN_API_KEY" in result_n.output


def test_deploy_yes_flag_skips_secret_scan_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--yes must bypass the secret-scan confirm prompt."""
    _write_agent_module(tmp_path)
    (tmp_path / "leaky.py").write_text(
        'import os\n'
        'tok = os.environ["ATLASSIAN_API_KEY"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--capture-env-vars",
            "--yes",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    fake_log_agent.assert_called_once()
    # Even with --yes, the warning text is still emitted.
    assert "ATLASSIAN_API_KEY" in result.output


def test_deploy_secret_scan_picks_up_dotenv_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KEY in .env.local that's also referenced in source must surface."""
    _write_agent_module(tmp_path)
    (tmp_path / ".env.local").write_text(
        "ATLASSIAN_API_KEY=sk-xxx\n"
        "BENIGN_FLAG=true\n"
    )
    (tmp_path / "uses_env.py").write_text(
        'import os\n'
        'k = os.environ.get("ATLASSIAN_API_KEY")\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "deploy",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert "ATLASSIAN_API_KEY" in result.output
    # BENIGN_FLAG should not appear — it doesn't match the secret pattern.
    assert "BENIGN_FLAG" not in result.output

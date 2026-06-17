"""Tests for cli.py — the apx command-line interface.

Covers:
  1. apx --help and `version` print without errors.
  2. apx-agent scaffold creates the expected file tree.
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
    assert "agents" in result.output
    assert "uc" in result.output
    assert "eval" in result.output
    assert "traces" in result.output


def test_version_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()  # some version string


# ---------------------------------------------------------------------------
# `apx-agent scaffold`
# ---------------------------------------------------------------------------


def test_scaffold_creates_expected_files(tmp_path: Path) -> None:
    runner = CliRunner()
    # Pin model-serving (flat agent.py + app.py); apps is the default and is
    # covered by test_scaffold_apps.py.
    result = runner.invoke(
        main,
        ["agents", "scaffold", "my_agent", "--target", "model-serving", "--dir", str(tmp_path), "--no-yaml"],
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
        ["agents", "scaffold", "existing", "--dir", str(tmp_path), "--no-yaml"],
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
        ["agents", "scaffold", "existing", "--dir", str(tmp_path), "--force", "--no-yaml"],
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
            "uc", "mcp-config",
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
        ["uc", "publish", "--module", "tmp_test_agent:agent", "--dry-run"],
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
    result = runner.invoke(main, ["agents", "describe", "--module", "tmp_test_agent:agent"])
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
        ["agents", "describe", "--module", "tmp_test_agent:agent", "--format", "json"],
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
# `apx-agent deploy --experiment` and pyproject fallback
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
                "agents", "deploy",
                "--target", "model-serving",
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
                "agents", "deploy",
                "--target", "model-serving",
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
        'name = "test-agent"\n'
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
                "agents", "deploy",
                "--target", "model-serving",
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
                "agents", "deploy",
                "--target", "model-serving",
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
                "agents", "deploy",
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
                "agents", "deploy",
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
                "agents", "deploy",
                "--target", "model-serving",
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
                "agents", "deploy",
                "--target", "model-serving",
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
    fake_ws = MagicMock()
    # Patch _connect_workspace directly: Config() is called before WorkspaceClient()
    # and is not mocked, so patching only WorkspaceClient causes an auth error.
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
         patch("apx_agent.run_sql", return_value=rows) as mock_sql:
        result = runner.invoke(main, ["watchdog", "violations"])

    assert result.exit_code == 0, result.output
    # The table identifier is now validated per-part and backtick-quoted (audit M2).
    assert "`main`.`watchdog`.`violations`" in mock_sql.call_args.args[1]


def test_watchdog_violations_filters_by_agent_and_hours() -> None:
    runner = CliRunner()
    fake_ws = MagicMock()
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
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
    fake_ws = MagicMock()
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
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
    fake_ws = MagicMock()
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
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
    fake_ws = MagicMock()
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
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
    result = runner.invoke(main, ["agents", "cost"])
    assert result.exit_code != 0
    assert "Pass --agent" in result.output or "Pass --agent" in (result.stderr or "")


def test_cost_agent_and_endpoint_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "cost", "--agent", "x", "--endpoint", "y"])
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

    runner = CliRunner()
    with patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent.cost_for_agent", return_value=fake_breakdown):
        result = runner.invoke(main, ["agents", "cost", "--agent", "customer_triage"])

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

    runner = CliRunner()
    with patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent.cost_for_agent", return_value=fake_breakdown):
        result = runner.invoke(main, [
            "agents", "cost", "--endpoint", "triage", "--hours", "12", "--format", "json",
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
    runner = CliRunner()
    with patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent.cost_for_agent", return_value=empty):
        result = runner.invoke(main, ["agents", "cost", "--agent", "triage"])

    assert result.exit_code == 0
    assert "No usage rows" in result.output


# ---------------------------------------------------------------------------
# `apx trace`
# ---------------------------------------------------------------------------


def test_trace_requires_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no pyproject -> no experiment fallback
    runner = CliRunner()
    result = runner.invoke(main, ["traces", "list"])
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
    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", fake_search):
        result = runner.invoke(main, ["traces", "list"])

    assert result.exit_code == 0, result.output
    # The helper takes the experiment name/id as its first positional arg.
    assert fake_search.call_args.args[0] == "/Users/me/agents/triage"


def test_trace_filters_by_agent_and_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_search = MagicMock(return_value=[])

    runner = CliRunner()
    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", fake_search):
        runner.invoke(main, [
            "traces", "list",
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
    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=fake_df):
        result = runner.invoke(main, [
            "traces", "list",
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
    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=fake_df):
        result = runner.invoke(main, [
            "traces", "list",
            "--experiment", "/Users/me/agents/x",
            "--format", "json",
        ])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed[0]["trace_id"] == "trace-1"
    assert parsed[0]["agent_name"] == "triage"


# ---------------------------------------------------------------------------
# `apx-agent test`
# ---------------------------------------------------------------------------


def test_test_requires_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["eval", "test", "--module", "tmp_test_agent:agent"])
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
            "eval", "test",
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
            "eval", "test",
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
        '[tool.apx.agent]\nname = "test-agent"\nmodel = "databricks-claude-sonnet-4-6"\n'
    )
    monkeypatch.chdir(tmp_path)

    from types import SimpleNamespace as NS
    fake_chat = MagicMock()
    fake_chat.predict.return_value = NS(messages=[NS(role="assistant", content="ok")])

    runner = CliRunner()
    with patch("apx_agent.compile_to_chat_agent", return_value=fake_chat):
        result = runner.invoke(main, ["eval", "test", "--module", "tmp_test_agent:agent"])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0
    assert fake_chat.predict.call_count == 1


# ---------------------------------------------------------------------------
# `apx-agent list`
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
    result = runner.invoke(main, ["agents", "list", "--schema", "agents"])
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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "list"])

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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "list", "--format", "json"])

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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "list"])

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
    result = runner.invoke(main, ["agents", "logs"])
    assert result.exit_code != 0
    assert "either --endpoint" in result.output or "either --endpoint" in (result.stderr or "")


def test_logs_endpoint_and_app_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "logs", "--endpoint", "x", "--app", "y"])
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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "logs", "--endpoint", "customer_triage"])

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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "logs", "--endpoint", "triage", "--build"])

    assert result.exit_code == 0, result.output
    assert "Building" in result.output
    fake_ws.serving_endpoints.build_logs.assert_called_once()
    fake_ws.serving_endpoints.logs.assert_not_called()


def test_logs_explicit_served_model_skips_discovery() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.logs.return_value = SimpleNamespace(logs="ok\n")

    runner = CliRunner()
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(
            main,
            ["agents", "logs", "--endpoint", "triage", "--served-model", "triage-2"],
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
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
        result = runner.invoke(main, ["agents", "logs", "--endpoint", "triage"])

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
        result = runner.invoke(main, ["agents", "logs", "--app", "my-app", "--profile", "prod"])

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
        result = runner.invoke(main, ["agents", "logs", "--app", "missing-app"])

    assert result.exit_code != 0
    assert "app not found" in result.output


def test_logs_app_friendly_error_when_databricks_cli_missing() -> None:
    runner = CliRunner()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = runner.invoke(main, ["agents", "logs", "--app", "x"])

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
        ["uc", "publish", "--module", "tmp_empty:agent", "--dry-run"],
    )
    sys.modules.pop("tmp_empty", None)

    assert result.exit_code == 0
    assert "No @tool(uc=...) decorated tools found" in result.output


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
# `apx memory consolidate`
# ---------------------------------------------------------------------------


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
# `apx-agent deploy` — env-var capture + secret-scan
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
                "agents", "deploy",
                "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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
            "agents", "deploy",
            "--target", "model-serving",
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


# ---------------------------------------------------------------------------
# apx-agent eval — endpoint-url flag, mutex, token parsing
# ---------------------------------------------------------------------------


def _write_evalset(tmp_path: Path) -> Path:
    """Write a minimal JSONL eval set returning a Path."""
    p = tmp_path / "evalset.jsonl"
    p.write_text(
        '{"inputs": {"question": "hi"}, "expectations": {"expected_facts": []}}\n'
    )
    return p


def test_eval_endpoint_url_mutex_with_model(tmp_path: Path) -> None:
    """--endpoint-url + --model raises a friendly UsageError."""
    evalset = _write_evalset(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, [
        "eval", "run", str(evalset),
        "--endpoint-url", "https://app.example.com",
        "--model", "databricks-claude-sonnet-4-6",
    ])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert "--model" in result.output


def test_eval_endpoint_url_mutex_with_module(tmp_path: Path) -> None:
    """--endpoint-url + --module raises a friendly UsageError."""
    evalset = _write_evalset(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, [
        "eval", "run", str(evalset),
        "--endpoint-url", "https://app.example.com",
        "--module", "agent:agent",
    ])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert "--module" in result.output


def test_eval_endpoint_url_with_token_parses_cleanly(tmp_path: Path) -> None:
    """--endpoint-url + --token routes to eval_against_endpoint without error."""
    evalset = _write_evalset(tmp_path)

    fake_result = SimpleNamespace(metrics={"score": 1.0})

    runner = CliRunner()
    with patch(
        "apx_agent.eval_against_endpoint", return_value=fake_result,
    ) as mock_eval:
        result = runner.invoke(main, [
            "eval", "run", str(evalset),
            "--endpoint-url", "https://app.example.com",
            "--token", "T",
        ])

    assert result.exit_code == 0, result.output
    assert mock_eval.called
    kwargs = mock_eval.call_args.kwargs
    args = mock_eval.call_args.args
    # eval_against_endpoint(endpoint_url, data, token=..., ...) — first positional
    # is the URL, second positional is the parsed JSONL data list.
    assert args[0] == "https://app.example.com"
    assert isinstance(args[1], list) and len(args[1]) == 1
    assert kwargs["token"] == "T"
    assert kwargs["stream"] is True  # default


def test_eval_endpoint_url_no_stream_flag(tmp_path: Path) -> None:
    """--no-stream flips the stream kwarg passed to eval_against_endpoint."""
    evalset = _write_evalset(tmp_path)
    fake_result = SimpleNamespace(metrics={})

    runner = CliRunner()
    with patch(
        "apx_agent.eval_against_endpoint", return_value=fake_result,
    ) as mock_eval:
        result = runner.invoke(main, [
            "eval", "run", str(evalset),
            "--endpoint-url", "https://app.example.com",
            "--token", "T",
            "--no-stream",
        ])

    assert result.exit_code == 0, result.output
    assert mock_eval.call_args.kwargs["stream"] is False


def test_eval_in_process_still_requires_model(tmp_path: Path) -> None:
    """Without --endpoint-url, --model is still required."""
    evalset = _write_evalset(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["eval", "run", str(evalset)])
    assert result.exit_code != 0
    assert "--model is required" in result.output


def test_run_passes_app_dir_to_uvicorn() -> None:
    """`apx-agent run` must hand uvicorn an app_dir of the CWD.

    Regression for "Error loading ASGI app. Could not import module 'app'":
    the `apx` console-script's sys.path does not include the CWD, so without
    app_dir uvicorn can't import the scaffold's top-level app.py.
    """
    runner = CliRunner()
    fake_uvicorn = MagicMock()
    with runner.isolated_filesystem():
        # Flat app.py layout (no databricks.yml) → model-serving
        Path("app.py").write_text("app = None\n")
        with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
                patch("apx_agent.cli._preflight_databricks_auth"), \
                patch("apx_agent.cli._probe_import"):
            result = runner.invoke(main, ["agents", "run"])

    assert result.exit_code == 0, result.output
    fake_uvicorn.run.assert_called_once()
    assert fake_uvicorn.run.call_args.args[0] == "app:app"
    assert fake_uvicorn.run.call_args.kwargs.get("app_dir") is not None


def test_detect_target_distinguishes_layouts(tmp_path: Path) -> None:
    """apps layout has agent_server/start_server.py; model-serving has app.py (no databricks.yml)."""
    from apx_agent.cli import _detect_target

    # Empty directory → apps (default)
    assert _detect_target(tmp_path) == "apps"
    # Flat app.py without databricks.yml → model-serving
    (tmp_path / "app.py").write_text("app = None\n")
    assert _detect_target(tmp_path) == "model-serving"
    # apps layout overrides app.py presence
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("app = None\n")
    assert _detect_target(tmp_path) == "apps"


def test_run_autodetects_apps_module() -> None:
    """In an apps layout, `apx-agent run` serves the agent_server module."""
    runner = CliRunner()
    fake_uvicorn = MagicMock()
    with runner.isolated_filesystem(), \
            patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("apx_agent.cli._preflight_databricks_auth"):
        Path("agent_server").mkdir()
        Path("agent_server/start_server.py").write_text("app = None\n")
        result = runner.invoke(main, ["agents", "run"])

    assert result.exit_code == 0, result.output
    assert fake_uvicorn.run.call_args.args[0] == "agent_server.start_server:app"


def test_deploy_autodetects_apps_target() -> None:
    """In an apps layout, `apx-agent deploy` (no --target) takes the apps path."""
    runner = CliRunner()
    with runner.isolated_filesystem(), \
            patch("apx_agent.cli._deploy_apps") as mock_apps:
        Path("agent_server").mkdir()
        Path("agent_server/start_server.py").write_text("app = None\n")
        result = runner.invoke(main, ["agents", "deploy"])

    assert result.exit_code == 0, result.output
    mock_apps.assert_called_once()


def test_deploy_autodetects_model_serving_target() -> None:
    """A flat app.py layout (no databricks.yml) detects as model-serving."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("app.py").write_text("app = None\n")
        result = runner.invoke(main, ["agents", "deploy"])

    assert result.exit_code != 0
    assert "--model is required" in result.output


def test_run_friendly_error_when_auth_unresolved() -> None:
    """When Databricks auth can't resolve, `apx-agent run` gives dev guidance, not a
    deep SDK traceback. The agent connects to a workspace at startup."""
    runner = CliRunner()
    fake_uvicorn = MagicMock()
    fake_config = MagicMock(side_effect=ValueError("ambiguous profile"))
    # Pin the configured-profiles lookup so the "pick a profile" guidance fires
    # deterministically: it depends on ~/.databrickscfg, which CI lacks (there
    # the doctor check falls back to the `databricks auth login` branch).
    with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("databricks.sdk.core.Config", fake_config), \
            patch("apx_agent.cli._databrickscfg_profiles", return_value=["DEFAULT", "prod"]):
        result = runner.invoke(main, ["agents", "run"])

    assert result.exit_code != 0
    assert "DATABRICKS_CONFIG_PROFILE" in result.output
    fake_uvicorn.run.assert_not_called()  # bailed before starting the server


def test_ensure_apx_wheel_resolves_dynamic_version(tmp_path: Path, monkeypatch) -> None:
    """Editable apx-agent with a dynamic (hatch-vcs) version: the wheel name
    isn't in pyproject, so _ensure_apx_wheel must resolve it from the built
    dist/. Regression for the `dist_dir / None` TypeError that broke
    `apx-agent deploy --target apps` after hatch-vcs landed.
    """
    from apx_agent.cli import _ensure_apx_wheel

    # apx-agent source root with a *dynamic* version (no project.version).
    src = tmp_path / "src_root"
    (src / "src" / "apx_agent").mkdir(parents=True)
    (src / "src" / "apx_agent" / "__init__.py").write_text("")
    (src / "pyproject.toml").write_text(
        '[project]\nname = "apx-agent"\ndynamic = ["version"]\n'
    )
    # Project that depends on it via an editable path.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndynamic = ["version"]\n\n'
        '[tool.uv.sources]\napx-agent = { path = "../src_root", editable = true }\n'
    )

    def fake_build(cmd, **kwargs):
        # Simulate `uv build --wheel` producing a dev-versioned wheel.
        dist = src / "dist"
        dist.mkdir(exist_ok=True)
        (dist / "apx_agent-0.2.2.dev6+gabc.d20260527-py3-none-any.whl").write_text("whl")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_build)

    staged = _ensure_apx_wheel(proj)
    assert staged is not None
    assert staged.name == "apx_agent-0.2.2.dev6+gabc.d20260527-py3-none-any.whl"
    assert staged.exists()


def test_sanitize_uv_lock_rewrites_internal_index(tmp_path: Path) -> None:
    """Deploy artifacts must resolve from public PyPI: the internal Databricks
    proxy in a uv.lock's source.registry is rewritten, download URLs untouched."""
    from apx_agent.cli import _sanitize_uv_lock

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://pypi-proxy.dev.databricks.com/simple" }\n'
        'url = "https://files.pythonhosted.org/x/foo-1.0-py3-none-any.whl"\n'
    )
    assert _sanitize_uv_lock(lock) is True
    text = lock.read_text()
    assert "pypi-proxy.dev.databricks.com" not in text
    assert 'registry = "https://pypi.org/simple"' in text
    assert "files.pythonhosted.org/x/foo-1.0-py3-none-any.whl" in text  # untouched
    assert _sanitize_uv_lock(lock) is False  # idempotent


def test_stage_build_manifest_no_wheel_stages_and_sanitizes(tmp_path: Path) -> None:
    """git+https install path (no local wheel): the deploy must still stage the
    source pyproject.toml + uv.lock into .build/ so the Apps container has a
    dependency manifest — otherwise it falls back to the base image's mlflow
    (no mlflow.genai.agent_server) and 502s. The staged lock is sanitized to
    public PyPI. Regression for issue #116.
    """
    from apx_agent.cli import _stage_build_manifest

    proj = tmp_path / "proj"
    (proj / ".build").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["apx-agent[langgraph] @ '
        'git+https://github.com/stuagano/apx-agent.git@main#subdirectory=python"]\n'
    )
    (proj / "uv.lock").write_text(
        'source = { registry = "https://pypi-proxy.dev.databricks.com/simple" }\n'
        'url = "https://files.pythonhosted.org/x/foo-1.0-py3-none-any.whl"\n'
    )

    _stage_build_manifest(proj / ".build", None)

    staged_pyproject = proj / ".build" / "pyproject.toml"
    staged_lock = proj / ".build" / "uv.lock"
    assert staged_pyproject.exists(), ".build/pyproject.toml must be staged"
    assert staged_lock.exists(), ".build/uv.lock must be staged"
    # Source pyproject copied verbatim (no wheel rewrite in this path).
    assert "git+https://github.com/stuagano/apx-agent.git" in staged_pyproject.read_text()
    # Staged lock sanitized away from the internal proxy.
    assert "pypi-proxy.dev.databricks.com" not in staged_lock.read_text()
    assert 'registry = "https://pypi.org/simple"' in staged_lock.read_text()


def test_stage_build_manifest_no_wheel_missing_lock_stages_pyproject_only(
    tmp_path: Path,
) -> None:
    """No source uv.lock present: stage pyproject.toml and let the container
    re-resolve. Must not crash on the absent lock."""
    from apx_agent.cli import _stage_build_manifest

    proj = tmp_path / "proj"
    (proj / ".build").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n'
    )
    # No uv.lock in source.

    _stage_build_manifest(proj / ".build", None)

    assert (proj / ".build" / "pyproject.toml").exists()
    assert not (proj / ".build" / "uv.lock").exists()


def test_scaffold_apps_pins_mlflow_with_genai_agent_server(tmp_path: Path) -> None:
    """The Apps scaffold must pin mlflow to a version that ships
    ``mlflow.genai.agent_server`` (the start_server import). ``>=3.0`` doesn't
    guarantee it; the floor must be >=3.12. Regression for issue #116."""
    from apx_agent.cli import _SCAFFOLD_APPS_PYPROJECT

    assert '"mlflow[databricks]>=3.12"' in _SCAFFOLD_APPS_PYPROJECT
    assert '"mlflow[databricks]>=3.0"' not in _SCAFFOLD_APPS_PYPROJECT


def test_scaffold_bakes_data_target_from_flags(tmp_path: Path) -> None:
    """--catalog/--schema bake the default DataAgent's data source (no probe)."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["agents", "scaffold", "ag", "--catalog", "main", "--schema", "sales",
               "--dir", str(tmp_path), "--no-yaml"],
    )
    assert result.exit_code == 0, result.output
    agent_py = (tmp_path / "ag" / "agent.py").read_text()
    assert 'DataAgent("main", "sales"' in agent_py


def test_splice_tool_wires_into_dataagent_extra_tools() -> None:
    """A generated tool attaches to a DataAgent via extra_tools= (no tools=
    list exists), and the result is valid Python. Regression for the orphaned
    tool on a composed/DataAgent default."""
    import ast as _ast
    from apx_agent._ui_edit import _splice_tool, _agent_tool_names

    src = 'from apx_agent import DataAgent\nagent = DataAgent("samples", "nyctaxi")\n'
    fn = 'def avg_trips(ws):\n    """Average trips."""\n    return {}\n'
    out = _splice_tool(src, fn, "avg_trips", target="agent")
    _ast.parse(out)  # must not be a positional-after-keyword SyntaxError
    assert "extra_tools=[avg_trips]" in out
    assert _agent_tool_names(out, "agent") == ["avg_trips"]


def test_run_auth_error_first_timer_points_to_login() -> None:
    """With no ~/.databrickscfg profiles, the auth error hands a first-timer the
    `databricks auth login` command (not just 'set a profile')."""
    runner = CliRunner()
    fake_uvicorn = MagicMock()
    fake_config = MagicMock(side_effect=ValueError("no creds"))
    with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("databricks.sdk.core.Config", fake_config), \
            patch("apx_agent.cli._databrickscfg_profiles", return_value=[]):
        result = runner.invoke(main, ["agents", "run"])
    assert result.exit_code != 0
    assert "databricks auth login" in result.output
    fake_uvicorn.run.assert_not_called()


def test_scaffold_explicit_target_bakes_example_tool(tmp_path: Path) -> None:
    """--catalog/--schema also bakes an example tool over a probed real table
    (parity with the auto-detected path)."""
    runner = CliRunner()
    with patch("apx_agent.cli._probe_first_table", return_value="trips"):
        result = runner.invoke(
            main, ["agents", "scaffold", "ag", "--catalog", "main", "--schema", "sales",
                   "--dir", str(tmp_path), "--no-yaml"],
        )
    assert result.exit_code == 0, result.output
    agent_py = (tmp_path / "ag" / "agent.py").read_text()
    assert "def sample_trips(" in agent_py
    assert "extra_tools=[sample_trips]" in agent_py


# ---------------------------------------------------------------------------
# `apx-agent doctor`
# ---------------------------------------------------------------------------


def test_doctor_runs_offline_and_reports(tmp_path: Path):
    runner = CliRunner()
    with patch("apx_agent._doctor.check_databricks_auth") as auth:
        from apx_agent._doctor import Check, Status

        auth.return_value = Check("Databricks auth", Status.OK, "ok", None)
        result = runner.invoke(main, ["doctor", "--offline"])
    assert "Environment" in result.output
    assert "Authentication" in result.output
    assert "Project" in result.output


def test_doctor_exit_nonzero_on_fail():
    runner = CliRunner()
    from apx_agent._doctor import Check, Status

    fail = Check("Python", Status.FAIL, "too old", "upgrade")
    with patch("apx_agent._doctor.check_python_version", return_value=fail):
        result = runner.invoke(main, ["doctor", "--offline"])
    assert result.exit_code != 0
    assert "upgrade" in result.output


def test_doctor_json_flag():
    runner = CliRunner()
    from apx_agent._doctor import Check, Status

    with patch("apx_agent._doctor.check_databricks_auth",
               return_value=Check("Databricks auth", Status.OK, "ok", None)):
        result = runner.invoke(main, ["doctor", "--offline", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.output)
    assert "Environment" in payload
    assert isinstance(payload["Environment"], list)


def test_doctor_online_invokes_live_check():
    runner = CliRunner()
    from apx_agent._doctor import Check, Status

    with patch(
        "apx_agent._doctor.check_databricks_workspace"
    ) as ws, patch(
        "apx_agent._doctor.check_databricks_auth",
        return_value=Check("Databricks auth", Status.OK, "ok", None),
    ):
        ws.return_value = Check("Workspace reachable", Status.OK, "ok", None)
        runner.invoke(main, ["doctor"])
    assert ws.called


# ---------------------------------------------------------------------------
# `_fix_msg` helper + refactored `_preflight_databricks_auth`
# ---------------------------------------------------------------------------


def test_fix_msg_format():
    from apx_agent.cli import _fix_msg

    msg = _fix_msg("Title", "what happened", "do this")
    assert "Title" in msg
    assert "what happened" in msg
    assert "Fix:" in msg
    assert "do this" in msg
    assert "apx-agent doctor" in msg


def test_preflight_auth_uses_check(monkeypatch):
    import click as _click

    from apx_agent._doctor import Check, Status

    fail = Check("Databricks auth", Status.FAIL, "no profiles", "login here")
    with patch("apx_agent._doctor.check_databricks_auth", return_value=fail):
        with pytest.raises(_click.ClickException) as exc:
            from apx_agent.cli import _preflight_databricks_auth

            _preflight_databricks_auth()
    assert "login here" in str(exc.value)


# ---------------------------------------------------------------------------
# "Did you mean" typo suggestions
# ---------------------------------------------------------------------------


def test_unknown_command_suggests_closest():
    runner = CliRunner()
    result = runner.invoke(main, ["agants"])  # typo of agents
    assert result.exit_code != 0
    assert "agents" in result.output
    assert "did you mean" in result.output.lower()


def test_moved_command_gives_redirect():
    runner = CliRunner()
    result = runner.invoke(main, ["deploy"])  # moved to agents deploy
    assert result.exit_code != 0
    assert "agents deploy" in result.output


def test_unknown_command_no_close_match():
    runner = CliRunner()
    result = runner.invoke(main, ["zzzzzz"])
    assert result.exit_code != 0
    assert "No such command" in result.output or "zzzzzz" in result.output


# ---------------------------------------------------------------------------
# `apx-agent scaffold` — next-steps footer (Task 10)
# ---------------------------------------------------------------------------


def test_scaffold_prints_next_steps(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "scaffold", "my-agent", "--no-yaml"])
    assert result.exit_code == 0
    out = result.output
    assert "cd my-agent" in out
    assert "uv sync" in out
    assert "apx-agent run" in out


# ---------------------------------------------------------------------------
# `apx-agent run` — pre-import probe (Task 8)
# ---------------------------------------------------------------------------


def test_run_probe_reports_broken_agent(tmp_path: Path, monkeypatch):
    # A scaffolded-looking apps project whose agent module raises on import.
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    agent_server = tmp_path / "agent_server"
    agent_server.mkdir()
    (agent_server / "__init__.py").write_text("")
    (agent_server / "start_server.py").write_text(
        "import does_not_exist_xyz\napp = None\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    with patch("apx_agent.cli._preflight_databricks_auth"), patch(
        "apx_agent._mlflow_tracing.autolog_if_env"
    ):
        result = runner.invoke(main, ["agents", "run"])
    assert result.exit_code != 0
    out = result.output
    assert "does_not_exist_xyz" in out or "start_server" in out
    # Load-bearing: distinguishes the probe's structured error from a raw
    # uvicorn traceback.
    assert "apx-agent doctor" in out


# ---------------------------------------------------------------------------
# Slice A — deploy tracing on by default
# ---------------------------------------------------------------------------


def test_scaffold_apps_start_server_enables_autolog() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_START_SERVER
    assert "autolog_if_env" in _SCAFFOLD_APPS_START_SERVER
    # called before the user agent import (so spans capture the first run)
    s = _SCAFFOLD_APPS_START_SERVER
    assert s.index("autolog_if_env()") < s.index("from agent import agent")


def test_scaffold_apps_databricks_yml_enables_autolog_env() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_DATABRICKS_YML
    assert "APX_AGENT_MLFLOW_AUTOLOG" in _SCAFFOLD_APPS_DATABRICKS_YML


def test_scaffold_apps_start_server_mounts_readyz() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_START_SERVER
    s = _SCAFFOLD_APPS_START_SERVER
    # mount_readyz is imported and called.
    assert "mount_readyz" in s
    assert "mount_readyz(app, agent)" in s
    # The import line includes mount_readyz alongside mount_mcp_endpoints.
    import_line = next(
        ln for ln in s.splitlines() if "from apx_agent import" in ln and "mount_mcp_endpoints" in ln
    )
    assert "mount_readyz" in import_line
    # readyz is mounted right after the MCP mount.
    assert s.index("mount_mcp_endpoints(app, agent)") < s.index("mount_readyz(app, agent)")


def test_scaffold_apps_start_server_calls_finalize_agent() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_START_SERVER
    s = _SCAFFOLD_APPS_START_SERVER
    assert "finalize_agent" in s
    # finalize_agent must run before compile so memory tools are attached
    assert s.index("finalize_agent(") < s.index("compile_to_responses_agent(")
    # and before resolve_conversation_store (which reads agent.session_config)
    assert s.index("finalize_agent(") < s.index("resolve_conversation_store(")


def test_grant_experiment_to_sp_issues_patch(monkeypatch) -> None:
    from apx_agent import cli
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    cli._grant_experiment_to_sp(
        "2960967542309513",
        "ff83b07a-2ab4-4564-88d4-54fb79417b06",
        profile="fe-cowork",
    )
    flat = " ".join(" ".join(c) for c in calls)
    assert "/api/2.0/permissions/experiments/2960967542309513" in flat
    assert "ff83b07a-2ab4-4564-88d4-54fb79417b06" in flat


# ---------------------------------------------------------------------------
# Slice C: `/readyz` deploy gate
# ---------------------------------------------------------------------------


def test_check_readyz_ready(monkeypatch) -> None:
    """A 200 `{"status":"ready"}` response → (True, checks)."""
    import io
    import urllib.request

    from apx_agent import cli

    # Token fetch routes through the _run_databricks_cmd seam.
    def fake_run(args, profile=None):
        assert args[:2] == ["auth", "token"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"access_token": "secret-token-value"}),
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_databricks_cmd", fake_run)

    body = json.dumps(
        {"status": "ready", "checks": {"llm": "ok", "tracing": "ok"}}
    ).encode()

    def fake_urlopen(req, timeout=None):
        # The Authorization header must carry the bearer token (but we never
        # log it). Confirm it threads through without echoing it anywhere.
        assert req.get_header("Authorization") == "Bearer secret-token-value"
        return io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, checks = cli._check_readyz(
        "https://app.example.com", profile="fe-cowork", attempts=1, delay_s=0.0
    )
    assert ok is True
    assert checks == {"llm": "ok", "tracing": "ok"}


def test_check_readyz_degraded(monkeypatch) -> None:
    """A 503 `{"status":"degraded"}` response → (False, checks), no retry."""
    import io
    import urllib.error
    import urllib.request

    from apx_agent import cli

    monkeypatch.setattr(
        cli,
        "_run_databricks_cmd",
        lambda args, profile=None: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"access_token": "tok"}),
            stderr="",
        ),
    )

    body = json.dumps(
        {"status": "degraded", "checks": {"llm": "fail", "tracing": "ok"}}
    ).encode()

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        # urlopen raises HTTPError on 503; the body is readable off the error.
        calls["n"] += 1
        raise urllib.error.HTTPError(
            "https://app.example.com/readyz", 503, "msg", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Fail the test loudly if the degraded (parseable) response triggers a sleep.
    import time as _time

    monkeypatch.setattr(
        _time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept"))
    )

    ok, checks = cli._check_readyz(
        "https://app.example.com", profile=None, attempts=5, delay_s=6.0
    )
    assert ok is False
    assert checks == {"llm": "fail", "tracing": "ok"}
    # A parseable degraded body returns immediately — no retry loop.
    assert calls["n"] == 1


def test_check_readyz_unreachable_returns_error(monkeypatch) -> None:
    """Total failure to reach /readyz → (False, {"error": ...}), never raises."""
    import urllib.error
    import urllib.request

    from apx_agent import cli

    monkeypatch.setattr(
        cli,
        "_run_databricks_cmd",
        lambda args, profile=None: SimpleNamespace(
            returncode=0, stdout=json.dumps({"access_token": "tok"}), stderr=""
        ),
    )

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)

    ok, checks = cli._check_readyz(
        "https://app.example.com", profile=None, attempts=2, delay_s=0.0
    )
    assert ok is False
    assert "error" in checks


def _drive_deploy_to_gate(tmp_path, monkeypatch, *, readyz_gate, check_result):
    """Drive _deploy_apps_impl through to the /readyz gate.

    Returns the list of _check_readyz call args (empty if never called).
    Raises whatever _deploy_apps_impl raises (e.g. ClickException at the gate).
    """
    from apx_agent.cli import _deploy_apps_impl

    (tmp_path / "databricks.yml").write_text(textwrap.dedent("""
        bundle:
          name: my-bundle
        resources:
          apps:
            my-app:
              name: my-app
    """))
    monkeypatch.chdir(tmp_path)

    check_calls: list = []

    def fake_check_readyz(app_url, *, profile, **kw):
        check_calls.append((app_url, profile))
        return check_result

    with patch("apx_agent.cli._preflight_databricks_cli", return_value=None), \
         patch("apx_agent.cli._preflight_apps", return_value=None), \
         patch("apx_agent.cli._validate_responses_agent_compiler", return_value=None), \
         patch(
             "apx_agent.cli._run_databricks_cmd",
             return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
         ), \
         patch(
             "apx_agent.cli._poll_app_ready",
             return_value={
                 "url": "https://app.example.com",
                 "service_principal_client_id": None,
             },
         ), \
         patch("apx_agent.cli._check_readyz", side_effect=fake_check_readyz), \
         patch("apx_agent.cli._fetch_app_log_tail", return_value="  ERROR: boom\n  Traceback..."):
        _deploy_apps_impl(
            cwd=tmp_path,
            module="agent:agent",
            profile=None,
            bundle_target="dev",
            no_run=True,
            auto_update_yml=False,
            auto_build_wheel=False,
            auto_experiment=False,
            vars=(),
            json_output=False,
            readyz_gate=readyz_gate,
            log=lambda msg: None,
        )
    return check_calls


def test_deploy_readyz_gate_fails_when_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """readyz_gate=True + degraded /readyz → ClickException with log tail."""
    import click

    with pytest.raises(click.ClickException) as exc:
        _drive_deploy_to_gate(
            tmp_path,
            monkeypatch,
            readyz_gate=True,
            check_result=(False, {"llm": "fail"}),
        )
    msg = str(exc.value)
    assert "readyz gate failed" in msg
    # Log tail and full-logs command must be surfaced to the user.
    assert "ERROR: boom" in msg
    assert "databricks apps logs my-app" in msg


def test_deploy_readyz_gate_passes_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """readyz_gate=True + ready /readyz → no raise; gate was called once."""
    calls = _drive_deploy_to_gate(
        tmp_path,
        monkeypatch,
        readyz_gate=True,
        check_result=(True, {"llm": "ok"}),
    )
    assert calls == [("https://app.example.com", None)]


def test_deploy_no_readyz_gate_skips_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """readyz_gate=False → _check_readyz is NOT called."""
    calls = _drive_deploy_to_gate(
        tmp_path,
        monkeypatch,
        readyz_gate=False,
        check_result=(False, {"llm": "fail"}),
    )
    assert calls == []


def test_fetch_app_log_tail_handles_missing_cli() -> None:
    """_fetch_app_log_tail never raises even when databricks CLI is absent."""
    from apx_agent.cli import _fetch_app_log_tail
    with patch("subprocess.run", side_effect=FileNotFoundError("databricks not found")):
        result = _fetch_app_log_tail("my-app", profile=None)
    assert "could not fetch logs" in result


def test_fetch_app_log_tail_returns_last_n_lines() -> None:
    """_fetch_app_log_tail trims to the requested number of lines."""
    from apx_agent.cli import _fetch_app_log_tail
    many_lines = "\n".join(f"line {i}" for i in range(100))
    fake_proc = SimpleNamespace(returncode=0, stdout=many_lines, stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        result = _fetch_app_log_tail("my-app", profile=None, lines=10)
    assert result.count("\n") == 9  # 10 lines = 9 newlines
    assert "line 99" in result
    assert "line 90" in result
    assert "line 89" not in result


# ---------------------------------------------------------------------------
# `apx info` — config-declared tools (E2 declarative tools, Task 7)
# ---------------------------------------------------------------------------


def test_apx_info_lists_config_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apx info should list [[tool.apx.tools]] declared in pyproject.toml."""
    # Write a minimal agent with NO inline tools so we can verify that the
    # genie tool comes exclusively from the config.
    (tmp_path / "info_config_tools_agent.py").write_text(textwrap.dedent("""
        from apx_agent import Agent
        agent = Agent(tools=[])
    """))
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """))
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main, ["agents", "describe", "--module", "info_config_tools_agent:agent"]
    )
    sys.modules.pop("info_config_tools_agent", None)

    assert result.exit_code == 0, result.output
    assert "ask_sales" in result.output


# ---------------------------------------------------------------------------
# apps-deploy path — config-declared tools governance (E2 declarative tools,
# Task 10)
# ---------------------------------------------------------------------------


def test_apps_deploy_config_genie_tool_reaches_resource_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Governance: a [[tool.apx.tools]] genie tool must be present on the agent
    that _auto_update_databricks_yml receives, so its genie_space resource is
    merged into databricks.yml.

    Uses a spy on _auto_update_databricks_yml: captures the agent argument, then
    raises a sentinel to short-circuit the downstream I/O (wheel build, bundle
    deploy, apps deploy).  Before the fix in _deploy_apps_impl, the agent was
    loaded but NOT finalized before the call, so collect_resource_specs would
    return no genie_space.  After the fix, finalize_agent runs first and the
    config tool is present.
    """
    from apx_agent._resources import collect_resource_specs
    from apx_agent.cli import _deploy_apps_impl

    # Write a minimal agent module with NO inline tools — genie comes from config.
    (tmp_path / "deploy_apps_config_tools_agent.py").write_text(textwrap.dedent("""
        from apx_agent import Agent
        agent = Agent(tools=[])
    """))
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "cfg-space-xyz"
        name = "ask_data"
    """))
    # Minimal databricks.yml so _read_databricks_yml and _resolve_app_name work.
    (tmp_path / "databricks.yml").write_text(textwrap.dedent("""
        bundle:
          name: my-bundle
        resources:
          apps:
            my-app:
              name: my-app
    """))
    monkeypatch.chdir(tmp_path)

    # Sentinel exception to abort after the seam we're testing.
    class _StopAfterSeam(Exception):
        pass

    captured_agent: list = []

    def _spy_auto_update(cwd, *, agent, bundle_key, log):
        captured_agent.append(agent)
        raise _StopAfterSeam("spy: stopping after seam")

    with patch("apx_agent.cli._preflight_databricks_cli", return_value=None), \
         patch("apx_agent.cli._preflight_apps", return_value=None), \
         patch("apx_agent.cli._validate_responses_agent_compiler", return_value=None), \
         patch("apx_agent.cli._auto_update_databricks_yml", side_effect=_spy_auto_update):
        try:
            _deploy_apps_impl(
                cwd=tmp_path,
                module="deploy_apps_config_tools_agent:agent",
                profile=None,
                bundle_target="dev",
                no_run=True,
                auto_update_yml=True,
                auto_build_wheel=False,
                auto_experiment=False,
                vars=(),
                json_output=False,
                log=lambda msg: None,
            )
        except _StopAfterSeam:
            pass

    sys.modules.pop("deploy_apps_config_tools_agent", None)

    assert captured_agent, "spy was never called — test setup error"
    agent = captured_agent[0]
    specs = collect_resource_specs(agent)
    kinds = {s.kind for s in specs}
    assert "genie_space" in kinds, (
        f"genie_space not in resource specs after finalize; got: {kinds}. "
        "finalize_agent was not called before _auto_update_databricks_yml."
    )


# ---------------------------------------------------------------------------
# Change 3 (T11): apx lint now covers [[tool.apx.tools]] config-declared tools
# ---------------------------------------------------------------------------


def test_lint_sees_config_declared_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization: apx lint (via _load_finalized_agent) must run
    ``_iter_tool_fns`` over config-declared tools so L002 fires for a
    docstring-less config tool.

    Strategy: monkeypatch ``apx_agent._tool_config._registry`` to inject a
    factory that returns a callable with NO docstring.  The factory name must
    stay "genie" (matches the [[tool.apx.tools]] table).  The tool callable is
    named "undocumented_config_tool" so the L002 finding has a stable location.
    We use ``--format json`` for a machine-readable assertion.
    """
    import apx_agent._tool_config as _tc

    module_name = "lint_config_tools_test_agent"

    # Agent has instructions so L001 doesn't fire, giving us a clean L002-only signal.
    (tmp_path / f"{module_name}.py").write_text(textwrap.dedent("""
        from apx_agent import Agent
        agent = Agent(instructions="I help with data.", tools=[])
    """))

    # [[tool.apx.tools]] declares one "genie" table; the monkeypatched registry
    # maps "genie" → factory that returns a docstring-less callable.
    # The factory ignores the 'name' kwarg and the tool's __name__ comes from
    # the closure definition (config_tool_no_doc).
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "lint-test"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "lint-space-01"
    """))

    monkeypatch.chdir(tmp_path)

    def config_tool_no_doc(question: str) -> str:
        # Deliberately no docstring — triggers L002.
        return question

    def _fake_registry() -> dict:
        return {"genie": lambda **kw: config_tool_no_doc}

    monkeypatch.setattr(_tc, "_registry", _fake_registry)

    runner = CliRunner()
    result = runner.invoke(
        main, ["eval", "lint", "--module", f"{module_name}:agent", "--format", "json"]
    )
    sys.modules.pop(module_name, None)

    assert result.exit_code == 0, result.output  # L002 is WARNING, not ERROR
    findings = json.loads(result.output)
    # L002 fires for the config-declared tool — proves lint sees [[tool.apx.tools]] tools.
    l002_locations = [f["location"] for f in findings if f["code"] == "L002"]
    assert "tool:config_tool_no_doc" in l002_locations, (
        f"Expected L002 for 'config_tool_no_doc' in config tools, "
        f"but lint findings were: {findings}. "
        "apx lint may not be running _iter_tool_fns over [[tool.apx.tools]] tools."
    )


# ---------------------------------------------------------------------------
# E3a Task 4 — template config flows through CLI (_load_finalized_agent)
# ---------------------------------------------------------------------------


def test_apx_info_with_template_config(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from apx_agent.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "sales-coworker"
        model = "databricks-claude-sonnet-4-6"
        template = { name = "data", catalog = "main", schema = "sales" }
    """))
    # _load_agent_config discovers pyproject.toml from __main__.__file__ first.
    # In pytest, __main__.__file__ is the pytest runner (inside python/.venv/),
    # so _find_pyproject walks up to the repo's own pyproject.toml instead of
    # tmp_path. Patch __main__.__file__ to a sentinel in tmp_path so the walk
    # starts from there and finds the test's pyproject.toml.
    import sys
    main_mod = sys.modules.get("__main__")
    sentinel = str(tmp_path / "fake_entrypoint.py")
    monkeypatch.setattr(main_mod, "__file__", sentinel, raising=False)
    res = CliRunner().invoke(main, ["agents", "describe", "--module", "nonexistent:agent"])
    assert res.exit_code == 0, res.output
    assert "run_sql" in res.output


def test_scaffold_apps_pyproject_ships_examples() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_PYPROJECT
    assert "examples = [" in _SCAFFOLD_APPS_PYPROJECT


class TestScaffoldSchemaManifest:
    def test_manifest_built_from_introspection(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(
            cli, "introspect_schema_columns",
            lambda ws, c, s: {"customer": ["c_custkey(bigint)"]},
        )
        # _make_ws_for_scaffold returns a dummy; introspection is what we stubbed
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: object())
        m = cli._schema_manifest_for_scaffold("samples", "tpch", profile=None)
        assert m == {
            "catalog": "samples", "schema": "tpch",
            "tables": {"customer": ["c_custkey(bigint)"]},
        }

    def test_manifest_none_when_empty(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "introspect_schema_columns", lambda ws, c, s: {})
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: object())
        assert cli._schema_manifest_for_scaffold("c", "s", profile=None) is None

    def test_manifest_none_when_no_ws(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)
        assert cli._schema_manifest_for_scaffold("c", "s", profile=None) is None

    def test_apps_scaffold_writes_manifest(self, tmp_path, monkeypatch):
        import json
        from apx_agent import cli
        monkeypatch.setattr(
            cli, "_schema_manifest_for_scaffold",
            lambda c, s, profile=None: {"catalog": c, "schema": s, "tables": {"t": ["a(int)"]}},
        )
        cli._scaffold_apps(tmp_path, "demo", force=True, catalog="samples", schema="tpch", table="t")
        manifest = tmp_path / ".apx" / "schema.json"
        assert manifest.is_file()
        assert json.loads(manifest.read_text())["tables"] == {"t": ["a(int)"]}

    def test_apps_scaffold_no_manifest_when_none(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True, catalog="samples", schema="tpch", table="t")
        assert not (tmp_path / ".apx" / "schema.json").exists()


class TestRefreshSchema:
    def test_refresh_rewrites_manifest(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent import cli
        # existing manifest pins samples.tpch
        d = tmp_path / ".apx"; d.mkdir()
        (d / "schema.json").write_text(json.dumps(
            {"catalog": "samples", "schema": "tpch", "tables": {"old": ["a(int)"]}}))
        monkeypatch.setattr(
            cli, "_schema_manifest_for_scaffold",
            lambda c, s, profile=None: {"catalog": c, "schema": s, "tables": {"new": ["b(int)"]}},
        )
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(cli.main, ["agents", "refresh-schema"])
        assert res.exit_code == 0, res.output
        assert json.loads((d / "schema.json").read_text())["tables"] == {"new": ["b(int)"]}

    def test_refresh_errors_without_existing_manifest(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from apx_agent import cli
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(cli.main, ["agents", "refresh-schema"])
        assert res.exit_code != 0
        assert "no .apx/schema.json" in res.output.lower()


class TestScaffoldCoworker:
    def test_apps_coworker_agent_py(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)  # skip introspection
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="coworker")
        agent_py = (tmp_path / "agent.py").read_text()
        assert "CoworkerAgent(" in agent_py
        assert "DataAgent(" not in agent_py       # the alias name carries the intent

    def test_apps_default_is_data_agent(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="data")
        assert "DataAgent(" in (tmp_path / "agent.py").read_text()


# ---------------------------------------------------------------------------
# `apx-agent refresh-schema`
# ---------------------------------------------------------------------------


class TestRefreshSchema:
    def test_error_when_no_schema_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["agents", "refresh-schema"])
        assert result.exit_code != 0
        assert "schema.json" in result.output or "scaffold" in result.output

    def test_happy_path_rewrites_manifest(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.chdir(tmp_path)
        apx_dir = tmp_path / ".apx"
        apx_dir.mkdir()
        (apx_dir / "schema.json").write_text(
            json.dumps({"catalog": "main", "schema": "default", "tables": []})
        )
        new_manifest = {"catalog": "main", "schema": "default", "tables": [{"name": "t1"}]}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: new_manifest)

        result = CliRunner().invoke(main, ["agents", "refresh-schema"])
        assert result.exit_code == 0, result.output
        assert "refreshed" in result.output
        written = json.loads((apx_dir / "schema.json").read_text())
        assert written["tables"] == [{"name": "t1"}]

    def test_error_when_introspect_returns_none(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.chdir(tmp_path)
        apx_dir = tmp_path / ".apx"
        apx_dir.mkdir()
        (apx_dir / "schema.json").write_text(
            json.dumps({"catalog": "main", "schema": "default", "tables": []})
        )
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)

        result = CliRunner().invoke(main, ["agents", "refresh-schema"])
        assert result.exit_code != 0
        assert "could not read tables" in result.output or "check your profile" in result.output


# ---------------------------------------------------------------------------
# `apx-agent publish`
# ---------------------------------------------------------------------------


class TestPublish:
    def test_missing_required_flags(self):
        result = CliRunner().invoke(main, ["agents", "publish"])
        assert result.exit_code != 0

    def test_happy_path(self):
        fake_result = {"tool_id": "tid-123", "status": "ok"}
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value=fake_result) as mock_pub, \
             patch("apx_agent.publish_to_registry"), \
             patch("apx_agent._publish.publish_tools_to_registry", return_value=0):
            result = CliRunner().invoke(main, [
                "agents", "publish",
                "--endpoint", "my-endpoint",
                "--supervisor", "sup-456",
                "--description", "Routes sales questions",
            ])
        assert result.exit_code == 0, result.output
        assert "my-endpoint" in result.output
        assert "sup-456" in result.output
        mock_pub.assert_called_once()

    def test_deprecated_publish_prints_notice(self):
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={}), \
             patch("apx_agent.publish_to_registry"), \
             patch("apx_agent._publish.publish_tools_to_registry", return_value=0):
            result = CliRunner().invoke(main, [
                "agents", "publish", "--endpoint", "my-endpoint",
                "--description", "x", "--supervisor", "sup-1",
            ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output


class TestPublishDisambiguation:
    """agents advertise / supervisor create / supervisor add (object-split)."""

    def test_advertise_writes_registries_not_supervisor(self):
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_registry") as mock_reg, \
             patch("apx_agent._publish.publish_tools_to_registry", return_value=0) as mock_tools, \
             patch("apx_agent.publish_to_supervisor") as mock_sup:
            result = CliRunner().invoke(main, [
                "agents", "advertise", "--endpoint", "my-endpoint",
                "--description", "Routes sales questions", "--no-tools",
            ])
        assert result.exit_code == 0, result.output
        mock_reg.assert_called_once()
        mock_sup.assert_not_called()  # advertise is discovery-only

    def test_supervisor_add_requires_supervisor_id(self):
        result = CliRunner().invoke(main, [
            "supervisor", "add", "--endpoint", "my-endpoint",
        ])
        assert result.exit_code != 0
        assert "supervisor" in result.output.lower()

    def test_supervisor_add_calls_publish_to_supervisor(self):
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={"tool_id": "t1"}) as mock_sup, \
             patch("apx_agent.publish_to_registry") as mock_reg:
            result = CliRunner().invoke(main, [
                "supervisor", "add", "--endpoint", "my-endpoint",
                "--supervisor", "sup-456", "--description", "x",
            ])
        assert result.exit_code == 0, result.output
        mock_sup.assert_called_once()
        mock_reg.assert_not_called()  # add is routing-only, no registry write

    def test_supervisor_create_calls_create_supervisor_agent(self):
        with patch("apx_agent.create_supervisor_agent",
                   return_value={"supervisor_agent_id": "sup-new"}) as mock_create, \
             patch("apx_agent.cli._save_to_pyproject", return_value=False):
            result = CliRunner().invoke(main, [
                "supervisor", "create", "--name", "Acme Assistant", "--no-save",
            ])
        assert result.exit_code == 0, result.output
        mock_create.assert_called_once()
        assert "sup-new" in result.output

    def test_deprecated_create_supervisor_delegates(self):
        with patch("apx_agent.create_supervisor_agent",
                   return_value={"supervisor_agent_id": "sup-x"}) as mock_create, \
             patch("apx_agent.cli._save_to_pyproject", return_value=False):
            result = CliRunner().invoke(main, [
                "agents", "create-supervisor", "--name", "Acme", "--no-save",
            ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# `apx-agent hot-swap`
# ---------------------------------------------------------------------------


class TestHotSwap:
    def test_model_serving_missing_endpoint(self):
        result = CliRunner().invoke(main, [
            "agents", "hot-swap", "--model", "databricks-claude-opus-4",
        ])
        assert result.exit_code != 0
        assert "endpoint" in result.output.lower()

    def test_model_serving_missing_model(self):
        result = CliRunner().invoke(main, [
            "agents", "hot-swap", "--endpoint", "my-ep",
        ])
        assert result.exit_code != 0
        assert "--model" in result.output

    def test_apps_missing_llm_endpoint(self):
        result = CliRunner().invoke(main, [
            "agents", "hot-swap", "--target", "apps",
        ])
        assert result.exit_code != 0
        assert "llm-endpoint" in result.output

    def test_model_serving_happy_path(self):
        fake_result = SimpleNamespace(
            endpoint_name="my-ep",
            new_model="databricks-claude-opus-4",
            previous_model=None,
            served_entities_updated=1,
        )
        with patch("apx_agent._hot_swap.hot_swap_model", return_value=fake_result) as mock_hs:
            result = CliRunner().invoke(main, [
                "agents", "hot-swap",
                "--endpoint", "my-ep",
                "--model", "databricks-claude-opus-4",
            ])
        assert result.exit_code == 0, result.output
        assert "my-ep" in result.output
        mock_hs.assert_called_once()


# ---------------------------------------------------------------------------
# `apx-agent export-traces`
# ---------------------------------------------------------------------------


class TestExportTraces:
    def test_missing_experiment_and_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, [
            "traces", "export", "--table", "main.default.traces",
        ])
        assert result.exit_code != 0
        assert "experiment" in result.output.lower()

    def test_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        fake_result = SimpleNamespace(
            rows_written=5,
            traces_pulled=5,
            target_table="main.default.traces",
            skipped=0,
        )
        fake_ws = MagicMock()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
             patch("apx_agent.export_traces", return_value=fake_result) as mock_et:
            result = CliRunner().invoke(main, [
                "traces", "export",
                "--experiment", "/my/experiment",
                "--table", "main.default.traces",
            ])

        assert result.exit_code == 0, result.output
        assert "5" in result.output
        mock_et.assert_called_once()


# ---------------------------------------------------------------------------
# `apx-agent topology`
# ---------------------------------------------------------------------------


class TestTopology:
    def test_schema_without_catalog_fails(self):
        result = CliRunner().invoke(main, [
            "uc", "topology", "--schema", "myschema",
        ])
        assert result.exit_code != 0
        assert "--catalog" in result.output or "catalog" in result.output.lower()

    def test_happy_path_stdout(self):
        fake_topo = SimpleNamespace(nodes=["a", "b"], edges=["a->b"])
        fake_ws = MagicMock()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
             patch("apx_agent.discover_topology", return_value=fake_topo), \
             patch("apx_agent.render_topology", return_value="graph LR\n  a --> b"):
            result = CliRunner().invoke(main, ["uc", "topology"])

        assert result.exit_code == 0, result.output
        assert "graph LR" in result.output

    def test_output_file(self, tmp_path):
        fake_topo = SimpleNamespace(nodes=["a"], edges=[])
        out = tmp_path / "topo.mmd"
        fake_ws = MagicMock()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
             patch("apx_agent.discover_topology", return_value=fake_topo), \
             patch("apx_agent.render_topology", return_value="graph LR"):
            result = CliRunner().invoke(main, ["uc", "topology", "--output", str(out)])

        assert result.exit_code == 0, result.output
        assert out.read_text().strip() == "graph LR"


# ---------------------------------------------------------------------------
# `apx-agent eval-chain`
# ---------------------------------------------------------------------------


class TestEvalChain:
    def test_happy_path_jsonl(self, tmp_path, monkeypatch):
        evalset = tmp_path / "cases.jsonl"
        evalset.write_text('{"request": "hello"}\n{"request": "bye"}\n')

        fake_agent = MagicMock()
        fake_case = SimpleNamespace(
            request="hello",
            duration_ms=120,
            sub_agents_invoked=["sub1"],
            tool_calls=["tool_a"],
        )
        fake_report = SimpleNamespace(
            cases=[fake_case],
            sub_agent_coverage={"sub1": 1},
        )

        with patch("apx_agent.cli._load_finalized_agent", return_value=fake_agent), \
             patch("apx_agent.evaluate_chain", return_value=fake_report):
            result = CliRunner().invoke(main, [
                "eval", "chain", str(evalset),
                "--model", "databricks-claude-opus-4",
                "--experiment", "/exp/my-eval",
            ])

        assert result.exit_code == 0, result.output
        assert "chain-eval cases: 1" in result.output
        assert "sub1" in result.output

    def test_happy_path_json_array(self, tmp_path):
        evalset = tmp_path / "cases.json"
        evalset.write_text('[{"request": "hi"}]')

        fake_agent = MagicMock()
        fake_report = SimpleNamespace(cases=[], sub_agent_coverage={})

        with patch("apx_agent.cli._load_finalized_agent", return_value=fake_agent), \
             patch("apx_agent.evaluate_chain", return_value=fake_report):
            result = CliRunner().invoke(main, [
                "eval", "chain", str(evalset),
                "--model", "databricks-claude-opus-4",
                "--experiment", "/exp/my-eval",
            ])

        assert result.exit_code == 0, result.output

    def test_bad_json_fails(self, tmp_path):
        evalset = tmp_path / "cases.json"
        evalset.write_text("not-json")

        fake_agent = MagicMock()
        with patch("apx_agent.cli._load_finalized_agent", return_value=fake_agent):
            result = CliRunner().invoke(main, [
                "eval", "chain", str(evalset),
                "--model", "databricks-claude-opus-4",
                "--experiment", "/exp/my-eval",
            ])

        assert result.exit_code != 0
        assert "parse" in result.output.lower() or "json" in result.output.lower()


# ---------------------------------------------------------------------------
# `apx-agent canary`
# ---------------------------------------------------------------------------


class TestCanary:
    def test_canary_status_happy_path(self):
        fake_cfg = SimpleNamespace(
            endpoint="my-ep",
            served_entities=[("entity-v1", "catalog.schema.model", "1")],
            traffic_split={"entity-v1": 100},
        )
        fake_ws = MagicMock()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
             patch("apx_agent.get_canary_config", return_value=fake_cfg):
            result = CliRunner().invoke(main, ["canary", "status", "--endpoint", "my-ep"])

        assert result.exit_code == 0, result.output
        assert "my-ep" in result.output
        assert "entity-v1" in result.output

    def test_canary_status_missing_endpoint(self):
        result = CliRunner().invoke(main, ["canary", "status"])
        assert result.exit_code != 0

    def test_canary_deploy_model_serving_missing_endpoint(self):
        result = CliRunner().invoke(main, [
            "canary", "deploy",
            "--model", "catalog.schema.model",
            "--version", "1",
        ])
        assert result.exit_code != 0
        assert "endpoint" in result.output.lower()

    def test_canary_deploy_model_serving_missing_model(self):
        result = CliRunner().invoke(main, [
            "canary", "deploy",
            "--endpoint", "my-ep",
            "--version", "1",
        ])
        assert result.exit_code != 0
        assert "--model" in result.output

    def test_canary_deploy_model_serving_missing_version(self):
        result = CliRunner().invoke(main, [
            "canary", "deploy",
            "--endpoint", "my-ep",
            "--model", "catalog.schema.model",
        ])
        assert result.exit_code != 0
        assert "--version" in result.output

    def test_canary_deploy_apps_missing_canary_version(self):
        result = CliRunner().invoke(main, [
            "canary", "deploy", "--target", "apps",
        ])
        assert result.exit_code != 0
        assert "canary-version" in result.output


# ---------------------------------------------------------------------------
# `apx-agent traces list` — enriched filters
# ---------------------------------------------------------------------------


class TestTracesListFilters:
    """Tests for the new --user, --min-latency, --error-only, --tag filters."""

    def _make_fake_search_traces(self, rows):
        """Return a stand-in for search_traces_for_experiment yielding fake rows.

        Accepts the helper's positional ``experiment`` arg plus any kwargs."""

        class _FakeDF:
            def to_dict(self, orient=None):
                return rows

        def _search(*args, **kwargs):
            return _FakeDF()

        return _search

    def test_error_only_filters_out_ok_traces(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        rows = [
            {"trace_id": "t1", "tags": {"apx.agent.name": "a"}, "status": "OK", "execution_time_ms": 100},
            {"trace_id": "t2", "tags": {"apx.agent.name": "a"}, "status": "ERROR", "execution_time_ms": 200},
        ]

        import mlflow as _mlflow
        with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", self._make_fake_search_traces(rows)):
            result = CliRunner().invoke(main, [
                "traces", "list", "--error-only",
            ])

        assert result.exit_code == 0, result.output
        # t1 (OK) must be absent; t2 (ERROR) must be present
        assert "t2" in result.output
        assert "t1" not in result.output

    def test_min_latency_filters_fast_traces(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        rows = [
            {"trace_id": "fast", "tags": {}, "status": "OK", "execution_time_ms": 50},
            {"trace_id": "slow", "tags": {}, "status": "OK", "execution_time_ms": 500},
        ]

        import mlflow as _mlflow
        with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", self._make_fake_search_traces(rows)):
            result = CliRunner().invoke(main, [
                "traces", "list", "--min-latency", "100",
            ])

        assert result.exit_code == 0, result.output
        # fast (50ms) below threshold — must be absent
        assert "slow" in result.output
        assert "fast" not in result.output

    def test_tag_filter_requires_key_val(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        import mlflow as _mlflow
        with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", self._make_fake_search_traces([])):
            result = CliRunner().invoke(main, [
                "traces", "list", "--tag", "no-equals-sign",
            ])

        # Must fail with a usage error about KEY=VAL format
        assert result.exit_code != 0
        assert "KEY=VAL" in result.output or "key=val" in result.output.lower()

    def test_missing_experiment_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["traces", "list"])
        assert result.exit_code != 0
        assert "experiment" in result.output.lower()


# ---------------------------------------------------------------------------
# `apx-agent traces get`
# ---------------------------------------------------------------------------


class TestTracesGet:
    def test_trace_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import mlflow as _mlflow
        with patch.object(_mlflow, "get_trace", return_value=None):
            result = CliRunner().invoke(main, ["traces", "get", "nonexistent-id"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_happy_path_text(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        span = SimpleNamespace(
            name="predict",
            span_id="span1",
            parent_span_id=None,
            start_time_ns=1_000_000_000,
            end_time_ns=2_000_000_000,
            status="OK",
            attributes={"apx.operation": "predict"},
        )
        fake_trace = SimpleNamespace(
            info=SimpleNamespace(
                trace_id="abc123",
                request_id=None,
                status="OK",
                execution_time_ms=1000,
                tags={"apx.agent.name": "my-agent"},
            ),
            data=SimpleNamespace(spans=[span]),
        )
        import mlflow as _mlflow
        with patch.object(_mlflow, "get_trace", return_value=fake_trace):
            result = CliRunner().invoke(main, ["traces", "get", "abc123"])
        assert result.exit_code == 0, result.output
        # Trace metadata must appear
        assert "abc123" in result.output
        assert "OK" in result.output
        # Span must appear in the tree
        assert "predict" in result.output

    def test_happy_path_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        span = SimpleNamespace(
            name="tool_call",
            span_id="s1",
            parent_span_id=None,
            start_time_ns=0,
            end_time_ns=1_000_000,
            status="OK",
            attributes={},
        )
        fake_trace = SimpleNamespace(
            info=SimpleNamespace(
                trace_id="trace-json-1",
                request_id=None,
                status="OK",
                execution_time_ms=1,
                tags={},
            ),
            data=SimpleNamespace(spans=[span]),
        )
        import mlflow as _mlflow
        with patch.object(_mlflow, "get_trace", return_value=fake_trace):
            result = CliRunner().invoke(main, ["traces", "get", "trace-json-1", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["trace_id"] == "trace-json-1"
        assert payload["status"] == "OK"
        # Span list must be present and non-empty
        assert len(payload["spans"]) == 1
        assert payload["spans"][0]["name"] == "tool_call"


# ---------------------------------------------------------------------------
# `apx-agent traces delete`
# ---------------------------------------------------------------------------


class TestTracesDelete:
    def test_requires_trace_id_or_hours(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["traces", "delete", "--yes"])
        assert result.exit_code != 0
        assert "trace-id" in result.output or "hours" in result.output.lower()

    def test_trace_id_and_hours_mutually_exclusive(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, [
            "traces", "delete", "--trace-id", "abc", "--hours", "24", "--yes",
        ])
        assert result.exit_code != 0
        assert "exclusive" in result.output.lower() or "mutually" in result.output.lower()

    def test_delete_by_trace_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        span = SimpleNamespace(attributes={})
        fake_trace = SimpleNamespace(
            info=SimpleNamespace(
                trace_id="t1",
                request_id=None,
                experiment_id="exp-123",
                status="OK",
                execution_time_ms=100,
                tags={},
            ),
            data=SimpleNamespace(spans=[span]),
        )
        fake_client = MagicMock()
        fake_client.delete_traces.return_value = 1

        import mlflow as _mlflow
        with patch.object(_mlflow, "get_trace", return_value=fake_trace), \
             patch.object(_mlflow, "MlflowClient", return_value=fake_client):
            result = CliRunner().invoke(main, [
                "traces", "delete", "--trace-id", "t1", "--yes",
            ])

        assert result.exit_code == 0, result.output
        assert "1" in result.output
        # delete_traces must have been called with the correct trace ID
        fake_client.delete_traces.assert_called_once()
        call_kwargs = fake_client.delete_traces.call_args
        assert "t1" in call_kwargs.kwargs.get("trace_ids", call_kwargs.args[1] if len(call_kwargs.args) > 1 else [])

    def test_missing_experiment_for_hours_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["traces", "delete", "--hours", "24", "--yes"])
        assert result.exit_code != 0
        assert "experiment" in result.output.lower()


# ---------------------------------------------------------------------------
# `apx-agent agents delete`
# ---------------------------------------------------------------------------


class TestAgentsDelete:
    def test_happy_path_with_yes(self):
        fake_ws = MagicMock()
        # Simulate UC tags containing the endpoint name
        fake_model = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.name", value="my-agent"),
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
        ])
        fake_ws.registered_models.get.return_value = fake_model

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        # Endpoint delete must have been called using the UC-tag-resolved name
        fake_ws.serving_endpoints.delete.assert_called_once_with("my-ep")
        # Registered model delete must have been called
        fake_ws.registered_models.delete.assert_called_once_with("main.schema.my_model")

    def test_with_explicit_endpoint_and_app(self):
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[])

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.m",
                "--endpoint", "explicit-ep",
                "--app", "my-app",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        fake_ws.serving_endpoints.delete.assert_called_once_with("explicit-ep")
        fake_ws.registered_models.delete.assert_called_once_with("main.schema.m")
        fake_ws.apps.delete.assert_called_once_with("my-app")

    def test_missing_uc_name_fails(self):
        result = CliRunner().invoke(main, ["agents", "delete", "--yes"])
        assert result.exit_code != 0
        assert "uc-name" in result.output or "uc_name" in result.output

    def test_endpoint_delete_failure_surfaces_error(self):
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[])
        fake_ws.serving_endpoints.delete.side_effect = RuntimeError("endpoint not found")

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.m",
                "--endpoint", "bad-ep",
                "--yes",
            ])

        # Must exit non-zero when any deletion fails
        assert result.exit_code != 0
        assert "failed" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# `apx-agent uc validate`
# ---------------------------------------------------------------------------


class TestUcValidate:
    def test_happy_path_all_checks_pass(self):
        fake_ws = MagicMock()
        with patch("apx_agent.cli._require_sdk", return_value=fake_ws), \
             patch("apx_agent.cli._ws_list_catalogs", return_value=["mycat"]), \
             patch("apx_agent.cli._ws_list_schemas", return_value=["myschema"]), \
             patch("apx_agent.cli._ws_list_tables", return_value=[SimpleNamespace(name="t1")]), \
             patch.object(fake_ws.registered_models, "list", return_value=iter([])):
            result = CliRunner().invoke(main, [
                "uc", "validate", "--catalog", "mycat", "--schema", "myschema",
            ])

        assert result.exit_code == 0, result.output
        assert "passed" in result.output.lower() or "ok" in result.output.lower()

    def test_no_tables_reports_missing_select(self):
        fake_ws = MagicMock()
        with patch("apx_agent.cli._require_sdk", return_value=fake_ws), \
             patch("apx_agent.cli._ws_list_catalogs", return_value=["mycat"]), \
             patch("apx_agent.cli._ws_list_schemas", return_value=["myschema"]), \
             patch("apx_agent.cli._ws_list_tables", return_value=[]), \
             patch.object(fake_ws.registered_models, "list", return_value=iter([])):
            result = CliRunner().invoke(main, [
                "uc", "validate", "--catalog", "mycat", "--schema", "myschema",
            ])

        # SELECT check fails → non-zero exit
        assert result.exit_code != 0

    def test_missing_catalog_arg_fails(self):
        result = CliRunner().invoke(main, ["uc", "validate", "--schema", "s"])
        assert result.exit_code != 0
        assert "--catalog" in result.output or "catalog" in result.output.lower()

    def test_json_format(self):
        fake_ws = MagicMock()
        with patch("apx_agent.cli._require_sdk", return_value=fake_ws), \
             patch("apx_agent.cli._ws_list_catalogs", return_value=["c"]), \
             patch("apx_agent.cli._ws_list_schemas", return_value=["s"]), \
             patch("apx_agent.cli._ws_list_tables", return_value=[SimpleNamespace(name="t")]), \
             patch.object(fake_ws.registered_models, "list", return_value=iter([])):
            result = CliRunner().invoke(main, [
                "uc", "validate", "--catalog", "c", "--schema", "s", "--format", "json",
            ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "checks" in payload
        assert payload["catalog"] == "c"
        assert payload["schema"] == "s"


# ---------------------------------------------------------------------------
# `apx-agent eval report`
# ---------------------------------------------------------------------------


class TestEvalReport:
    def test_missing_experiment_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["eval", "report"])
        assert result.exit_code != 0
        assert "experiment" in result.output.lower()

    def test_no_runs_shows_helpful_message(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        fake_exp = SimpleNamespace(experiment_id="exp-1")

        class _EmptyDF:
            def to_dict(self, orient=None):
                return []

        import mlflow as _mlflow
        with patch.object(_mlflow, "get_experiment_by_name", return_value=fake_exp), \
             patch.object(_mlflow, "search_runs", return_value=_EmptyDF()):
            result = CliRunner().invoke(main, ["eval", "report"])

        assert result.exit_code == 0, result.output
        assert "no eval runs" in result.output.lower()

    def test_happy_path_text(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        fake_exp = SimpleNamespace(experiment_id="exp-1")
        runs = [
            {
                "run_id": "run-abc",
                "start_time": "2026-06-10T10:00:00",
                "metrics.pass_rate": 0.9,
                "metrics.avg_latency_ms": 450.0,
            },
            {
                "run_id": "run-def",
                "start_time": "2026-06-09T10:00:00",
                "metrics.pass_rate": 0.75,
                "metrics.avg_latency_ms": None,
            },
        ]

        class _FakeDF:
            def to_dict(self, orient=None):
                return runs

        import mlflow as _mlflow
        with patch.object(_mlflow, "get_experiment_by_name", return_value=fake_exp), \
             patch.object(_mlflow, "search_runs", return_value=_FakeDF()):
            result = CliRunner().invoke(main, ["eval", "report"])

        assert result.exit_code == 0, result.output
        # Both run IDs must appear in the table
        assert "run-abc" in result.output
        assert "run-def" in result.output
        # The pass_rate metric must appear (both present)
        assert "0.9" in result.output

    def test_json_format(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "t"\nexperiment = "/exp/test"\n'
        )
        fake_exp = SimpleNamespace(experiment_id="exp-1")
        runs = [{"run_id": "r1", "start_time": "2026-06-10", "metrics.pass_rate": 1.0}]

        class _FakeDF:
            def to_dict(self, orient=None):
                return runs

        import mlflow as _mlflow
        with patch.object(_mlflow, "get_experiment_by_name", return_value=fake_exp), \
             patch.object(_mlflow, "search_runs", return_value=_FakeDF()):
            result = CliRunner().invoke(main, ["eval", "report", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert payload[0]["run_id"] == "r1"
        assert payload[0]["metrics"]["pass_rate"] == 1.0


# ---------------------------------------------------------------------------
# `apx-agent memory export`
# ---------------------------------------------------------------------------


class TestMemoryExport:
    def _make_store(self, memories):
        store = MagicMock()
        store.list.return_value = memories
        return store

    def _make_memory(self, mid, content, principal_id="alice"):
        return SimpleNamespace(
            id=mid,
            principal_id=principal_id,
            namespace="default",
            content=content,
            tags=[],
            importance=0.5,
            metadata={},
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )

    def test_export_to_file(self, tmp_path):
        out = tmp_path / "out.jsonl"
        memories = [self._make_memory("m1", "hello"), self._make_memory("m2", "world")]

        with patch("apx_agent.cli._load_store", return_value=self._make_store(memories)):
            result = CliRunner().invoke(main, [
                "memory", "export",
                "--principal-id", "alice",
                "--output", str(out),
                "--store-module", "fake:store",
            ])

        assert result.exit_code == 0, result.output
        lines = [l for l in out.read_text().splitlines() if l.strip()]
        # Two memories → two JSONL lines
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["id"] == "m1"
        assert first["content"] == "hello"
        assert "2" in result.output  # count mentioned

    def test_export_to_stdout(self, tmp_path):
        memories = [self._make_memory("m1", "hi")]
        with patch("apx_agent.cli._load_store", return_value=self._make_store(memories)):
            result = CliRunner().invoke(main, [
                "memory", "export",
                "--principal-id", "alice",
                "--output", "-",
                "--store-module", "fake:store",
            ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["id"] == "m1"

    def test_missing_output_fails(self):
        result = CliRunner().invoke(main, [
            "memory", "export",
            "--principal-id", "alice",
            "--store-module", "fake:store",
        ])
        assert result.exit_code != 0
        assert "--output" in result.output or "output" in result.output.lower()


# ---------------------------------------------------------------------------
# `apx-agent examples import`
# ---------------------------------------------------------------------------


class TestExamplesImport:
    def _make_store(self):
        store = MagicMock()
        store.add.side_effect = lambda d: SimpleNamespace(id="new-id", **d)
        return store

    def test_happy_path_jsonl(self, tmp_path):
        jsonl = tmp_path / "data.jsonl"
        jsonl.write_text(
            '{"input": "What is 2+2?", "output": "4", "intent": "math"}\n'
            '{"input": "hi", "output": "hello"}\n'
        )
        store = self._make_store()
        with patch("apx_agent.cli._load_store", return_value=store):
            result = CliRunner().invoke(main, [
                "examples", "import", str(jsonl),
                "--agent-id", "agent-42",
                "--store-module", "fake:store",
            ])

        assert result.exit_code == 0, result.output
        # Both rows must have been added
        assert store.add.call_count == 2
        # 2 imported must appear in output
        assert "2" in result.output

    def test_happy_path_csv(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("input,output,intent\nq1,a1,qa\nq2,a2,qa\nq3,a3,qa\n")
        store = self._make_store()
        with patch("apx_agent.cli._load_store", return_value=store):
            result = CliRunner().invoke(main, [
                "examples", "import", str(csv_file),
                "--agent-id", "ag",
                "--store-module", "fake:store",
            ])

        assert result.exit_code == 0, result.output
        # 3 CSV data rows → 3 add calls
        assert store.add.call_count == 3

    def test_dry_run_does_not_write(self, tmp_path):
        jsonl = tmp_path / "data.jsonl"
        jsonl.write_text('{"input": "q", "output": "a"}\n')
        store = self._make_store()
        with patch("apx_agent.cli._load_store", return_value=store):
            result = CliRunner().invoke(main, [
                "examples", "import", str(jsonl),
                "--agent-id", "ag",
                "--store-module", "fake:store",
                "--dry-run",
            ])

        assert result.exit_code == 0, result.output
        # Dry-run must not call store.add
        store.add.assert_not_called()
        assert "dry-run" in result.output or "would" in result.output.lower()

    def test_missing_input_field_fails(self, tmp_path):
        jsonl = tmp_path / "bad.jsonl"
        jsonl.write_text('{"output": "answer"}\n')
        with patch("apx_agent.cli._load_store", return_value=self._make_store()):
            result = CliRunner().invoke(main, [
                "examples", "import", str(jsonl),
                "--agent-id", "ag",
                "--store-module", "fake:store",
            ])
        assert result.exit_code != 0
        assert "input" in result.output.lower()

    def test_unsupported_file_type_fails(self, tmp_path):
        f = tmp_path / "data.xml"
        f.write_text("<root/>")
        with patch("apx_agent.cli._load_store", return_value=self._make_store()):
            result = CliRunner().invoke(main, [
                "examples", "import", str(f),
                "--agent-id", "ag",
                "--store-module", "fake:store",
            ])
        assert result.exit_code != 0
        assert ".xml" in result.output or "unsupported" in result.output.lower()


# ---------------------------------------------------------------------------
# Regression: coworker-gen must pass ChatMessage objects to serving_endpoints.query,
# not raw dicts. The SDK calls .as_dict() on each message, so dicts AttributeError
# at runtime — the bug this guards (path was previously untested).
# ---------------------------------------------------------------------------


def test_generate_coworker_yaml_passes_chatmessage_not_dicts() -> None:
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    from apx_agent.cli import _generate_coworker_yaml

    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="name: demo\n"))]
    )
    answers = iter(["Salesforce", "deals", "NetSuite", "invoices", "account_id", "RevOps", "unbilled deals"])

    with patch("apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)), patch(
        "databricks.sdk.WorkspaceClient", return_value=fake_ws
    ):
        out = _generate_coworker_yaml(None)

    assert out == "name: demo"
    msgs = fake_ws.serving_endpoints.query.call_args.kwargs["messages"]
    assert msgs and all(isinstance(m, ChatMessage) for m in msgs), (
        f"messages must be ChatMessage objects (SDK calls .as_dict()); got {[type(m).__name__ for m in msgs]}"
    )
    assert msgs[0].role == ChatMessageRole.USER


# ---------------------------------------------------------------------------
# TestMigrateToOKF
# ---------------------------------------------------------------------------


class TestMigrateToOKF:
    def test_migrate_creates_bundle_and_regenerates_cache(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent.cli import agents

        apx = tmp_path / ".apx"
        apx.mkdir()
        manifest = {
            "catalog": "serverless_stable_qh44kx_catalog",
            "schema": "payroll_demo",
            "tables": {"employees": ["employee_id(string)"], "pay_runs": ["gross_pay(decimal(6,2))"]},
        }
        (apx / "schema.json").write_text(json.dumps(manifest))
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["migrate-to-okf"])
        assert result.exit_code == 0, result.output
        assert (apx / "okf" / "datasets" / "payroll_demo.md").is_file()
        assert (apx / "okf" / "tables" / "pay_runs.md").is_file()

        from apx_agent._schema import load_baked_schema
        assert load_baked_schema(tmp_path) == manifest

    def test_refuses_existing_bundle_without_force(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent.cli import agents

        apx = tmp_path / ".apx"
        (apx / "okf").mkdir(parents=True)
        (apx / "schema.json").write_text(json.dumps({"catalog": "c", "schema": "s", "tables": {}}))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(agents, ["migrate-to-okf"])
        assert result.exit_code != 0
        assert "force" in result.output.lower()


class TestRefreshSchemaPreservesOKF:
    def test_refresh_preserves_enriched_body_and_updates_cache(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent import cli
        from apx_agent.cli import agents
        from apx_agent._okf import write_okf_bundle

        apx = tmp_path / ".apx"
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        write_okf_bundle(m, apx / "okf", timestamp="z")
        (apx / "okf" / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nEnriched narrative.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        (apx / "schema.json").write_text(json.dumps(m))
        # live introspection now reports a wider type
        updated = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(10,2))"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: updated)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["refresh-schema"])
        assert result.exit_code == 0, result.output
        body = (apx / "okf" / "tables" / "pay_runs.md").read_text()
        assert "Enriched narrative." in body          # body preserved
        assert "decimal(10,2)" in body                # schema refreshed
        assert json.loads((apx / "schema.json").read_text())["tables"]["pay_runs"] == ["gross_pay(decimal(10,2))"]

    def _two_table_bundle(self, tmp_path):
        from apx_agent._okf import write_okf_bundle
        apx = tmp_path / ".apx"
        m = {"catalog": "c", "schema": "s", "tables": {"keep": ["x(int)"], "gone": ["y(int)"]}}
        write_okf_bundle(m, apx / "okf", timestamp="z")
        (apx / "schema.json").write_text(json.dumps(m))
        return apx

    def test_default_refresh_keeps_table_missing_from_live_schema(self, tmp_path, monkeypatch):
        # Live introspection no longer returns `gone`; default refresh must NOT drop it.
        from apx_agent import cli
        from apx_agent.cli import agents
        apx = self._two_table_bundle(tmp_path)
        live = {"catalog": "c", "schema": "s", "tables": {"keep": ["x(int)"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: live)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["refresh-schema"])
        assert result.exit_code == 0, result.output
        assert (apx / "okf" / "tables" / "gone.md").is_file()   # preserved by default
        assert set(json.loads((apx / "schema.json").read_text())["tables"]) == {"keep", "gone"}

    def test_prune_flag_removes_table_missing_from_live_schema(self, tmp_path, monkeypatch):
        from apx_agent import cli
        from apx_agent.cli import agents
        apx = self._two_table_bundle(tmp_path)
        live = {"catalog": "c", "schema": "s", "tables": {"keep": ["x(int)"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: live)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["refresh-schema", "--prune-missing-tables"])
        assert result.exit_code == 0, result.output
        assert not (apx / "okf" / "tables" / "gone.md").exists()  # dropped on opt-in
        assert set(json.loads((apx / "schema.json").read_text())["tables"]) == {"keep"}


# ---------------------------------------------------------------------------
# TestPullComments
# ---------------------------------------------------------------------------


class TestPullComments:
    def test_pull_fills_bundle_from_uc_comments(self, tmp_path, monkeypatch):
        import json
        from types import SimpleNamespace
        from click.testing import CliRunner
        from apx_agent import cli
        from apx_agent.cli import agents
        from apx_agent._okf import write_okf_bundle

        apx = tmp_path / ".apx"
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        write_okf_bundle(m, apx / "okf", timestamp="z")
        (apx / "schema.json").write_text(json.dumps(m))

        def col(name, comment):
            return SimpleNamespace(name=name, comment=comment)

        fake_tables = [
            SimpleNamespace(
                name="pay_runs",
                comment="Core payroll table.",
                columns=[col("gross_pay", "Gross before deductions.")],
            )
        ]
        fake_ws = SimpleNamespace(
            tables=SimpleNamespace(
                list=lambda catalog_name, schema_name: fake_tables
            )
        )
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda *a, **k: fake_ws)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["pull-comments"])
        assert result.exit_code == 0, result.output
        body = (apx / "okf" / "tables" / "pay_runs.md").read_text()
        assert "Gross before deductions." in body   # column comment -> Description cell
        assert "Core payroll table." in body         # table comment -> # Overview

    def test_pull_no_bundle_errors_cleanly(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from apx_agent.cli import agents

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(agents, ["pull-comments"])
        assert result.exit_code != 0  # no .apx -> clean ClickException, not a crash

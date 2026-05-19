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

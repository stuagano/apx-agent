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
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from apx_agent._doctor import SubAgentProbe
from apx_agent.cli import _load_agent, _parse_module_spec, _ReadyzResult, main


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


def test_run_missing_yaml_gives_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["agents", "run", "nope.yaml"])
    assert result.exit_code != 0
    assert "Spec file not found" in result.output


def test_run_unknown_dir_does_not_falsely_match_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cwd is NOT an apx project — run must error, not serve cwd as "apps".
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["agents", "run", "ghost"])
    assert result.exit_code != 0
    assert "No runnable agent project" in result.output


def test_bake_schema_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    import apx_agent.cli as cli

    monkeypatch.setattr(
        cli, "_schema_manifest_for_scaffold",
        lambda c, s, p: {"catalog": c, "schema": s, "tables": {"orders": ["id(int)"]}},
    )
    cfg = SimpleNamespace(template={"catalog": "main", "schema": "sales"})
    assert cli._bake_schema_into_project(tmp_path, cfg, None) is True
    baked = json.loads((tmp_path / ".apx" / "schema.json").read_text())
    assert baked["catalog"] == "main"
    assert baked["tables"] == {"orders": ["id(int)"]}


def test_bake_schema_skips_placeholder_catalog(tmp_path: Path) -> None:
    # Unresolved $CATALOG/$SCHEMA → no introspection, no file (agent falls back).
    from types import SimpleNamespace

    from apx_agent.cli import _bake_schema_into_project

    cfg = SimpleNamespace(template={"catalog": "$CATALOG", "schema": "sales"})
    assert _bake_schema_into_project(tmp_path, cfg, None) is False
    assert not (tmp_path / ".apx" / "schema.json").exists()


def test_bake_schema_no_template(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from apx_agent.cli import _bake_schema_into_project

    cfg = SimpleNamespace(template=None)
    assert _bake_schema_into_project(tmp_path, cfg, None) is False


def test_status_prompt_outside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    result = CliRunner().invoke(main, ["status", "--prompt"])
    assert result.exit_code == 0
    assert result.output.strip() == "apx"  # no project, no profile


def test_status_prompt_in_project_with_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\nname = "demo"\n'
    )
    (tmp_path / "agent_server").mkdir()  # apps-layout marker for _detect_target
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "fe-stable")
    result = CliRunner().invoke(main, ["status", "--prompt"])
    assert result.exit_code == 0
    assert result.output.strip() == "apx:demo(apps) ▸ fe-stable"


# ---------------------------------------------------------------------------
# `apx-agent scaffold`
# ---------------------------------------------------------------------------


def test_scaffold_no_longer_has_yaml_flag() -> None:
    result = CliRunner().invoke(main, ["agents", "scaffold", "--help"])
    assert result.exit_code == 0
    assert "--yaml" not in result.output
    assert "--no-yaml" not in result.output


def test_plain_scaffold_always_materializes_full_project(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "plain-agent",
            "--catalog", "samples", "--schema", "nyctaxi",
            "--dir", str(tmp_path),
            "--no-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "plain-agent" / "agent.py").exists()
    assert not (tmp_path / "plain-agent.yaml").exists()


def test_scaffold_creates_expected_files(tmp_path: Path) -> None:
    runner = CliRunner()
    # Pin model-serving (flat agent.py + app.py); apps is the default and is
    # covered by test_scaffold_apps.py.
    result = runner.invoke(
        main,
        ["agents", "scaffold", "my_agent", "--target", "model-serving", "--dir", str(tmp_path)],
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
        ["agents", "scaffold", "existing", "--dir", str(tmp_path)],
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
        ["agents", "scaffold", "existing", "--dir", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0
    assert "# old content" not in (target / "agent.py").read_text()
    assert "from apx_agent import" in (target / "agent.py").read_text()


def test_scaffold_apps_threads_custom_instructions(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent",
            "--template", "base",
            "--target", "apps",
            "--dir", str(tmp_path),
            "--no-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    # base template with no explicit instructions still gets the default —
    # this test only proves the plumbing exists; the wizard-driven case
    # (interactive prompt -> instructions) is covered by
    # test_scaffold_wizard_instructions_reach_agent_py below.
    agent_py = (tmp_path / "my_agent" / "agent.py").read_text()
    assert "instructions='You are a helpful assistant.'" in agent_py


def test_scaffold_wizard_instructions_reach_agent_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mock _scaffold_wizard and _scaffold_sanity_check directly rather than
    # feeding a None/fake WorkspaceClient through the real wizard — those
    # functions' internals (catalog/schema probing) aren't something this
    # test should depend on; it only needs to prove instructions plumbing.
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_prompt_for_instructions", lambda: "Answer HR pay questions.")
    monkeypatch.setattr(
        cli, "_scaffold_wizard",
        lambda ws, target, template, catalog, schema: ("apps", "base", None, None, None, None, None),
    )
    monkeypatch.setattr(cli, "_scaffold_sanity_check", lambda ws, template, catalog, schema: None)
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "hr_agent",
            "--dir", str(tmp_path),
            "--interactive",
            "--target", "apps",
        ],
    )
    assert result.exit_code == 0, result.output
    agent_py = (tmp_path / "hr_agent" / "agent.py").read_text()
    assert "instructions='Answer HR pay questions.'" in agent_py


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


def test_publish_tools_registry_failure_fails_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #528: a registry-write failure used to only print a yellow
    # warning and still exit 0 — inconsistent with deploy's step-outcome
    # ledger, where a best-effort sub-step failure still fails the command.
    from apx_agent._tool_publish import PublishResult

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_result = PublishResult(
        uc_name="main.tools.classify_intent", function_name="classify_intent",
        grants_applied=("agent_consumers",),
    )
    runner = CliRunner()
    with patch("apx_agent.publish_tools_to_uc", return_value=[fake_result]), \
         patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent._publish.publish_standalone_tools_to_registry",
               side_effect=RuntimeError("permission denied")):
        result = runner.invoke(
            main, ["uc", "publish", "--module", "tmp_test_agent:agent"],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    assert "Tools registry write failed" in result.output
    assert "permission denied" in result.output


def test_publish_tools_json_output_reports_registry_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent._tool_publish import PublishResult

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_result = PublishResult(
        uc_name="main.tools.classify_intent", function_name="classify_intent",
        grants_applied=("agent_consumers",),
    )
    runner = CliRunner()
    with patch("apx_agent.publish_tools_to_uc", return_value=[fake_result]), \
         patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent._publish.publish_standalone_tools_to_registry",
               side_effect=RuntimeError("permission denied")):
        result = runner.invoke(
            main, ["uc", "publish", "--module", "tmp_test_agent:agent", "--json-output"],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["registry_error"] == "permission denied"
    assert payload["published"][0]["uc_name"] == "main.tools.classify_intent"


def test_publish_tools_json_output_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent._tool_publish import PublishResult

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_result = PublishResult(
        uc_name="main.tools.classify_intent", function_name="classify_intent",
        grants_applied=("agent_consumers",),
    )
    runner = CliRunner()
    with patch("apx_agent.publish_tools_to_uc", return_value=[fake_result]), \
         patch("apx_agent.cli._connect_workspace", return_value=(MagicMock(), MagicMock())), \
         patch("apx_agent._publish.publish_standalone_tools_to_registry", return_value=1):
        result = runner.invoke(
            main, ["uc", "publish", "--module", "tmp_test_agent:agent", "--json-output"],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["registry"] == {"table": "main.apx.agent_tools", "count": 1}
    assert payload["registry_error"] is None


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
                "--no-publish-tools",
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
                "--no-publish-tools",
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
                "--no-publish-tools",
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
                "--no-publish-tools",
            ],
        )
    sys.modules.pop("tmp_test_agent", None)

    assert fake_log_agent.call_args.kwargs["experiment"] is None


# ---------------------------------------------------------------------------
# model-serving deploy: sub-step failure contract (#402) + --json-output (#405)
# ---------------------------------------------------------------------------


def _invoke_model_serving_deploy(
    extra_args: list[str],
    *,
    log_agent: MagicMock | None = None,
    publish: MagicMock | None = None,
    set_tags: MagicMock | None = None,
) -> Result:
    """Invoke `agents deploy --target model-serving` with mocked sub-steps.

    log_agent / publish / set_tags default to succeeding fakes.
    """
    if log_agent is None:
        log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))
    if publish is None:
        publish = MagicMock(return_value=[])
    if set_tags is None:
        set_tags = MagicMock(return_value={"apx.agent.name": "x"})

    runner = CliRunner()
    with patch("apx_agent.log_agent", log_agent), \
         patch("apx_agent.publish_tools_to_uc", publish), \
         patch("apx_agent.set_uc_tags_for_agent", set_tags), \
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
            ] + extra_args,
        )
    sys.modules.pop("tmp_test_agent", None)
    return result


def test_deploy_publish_tools_failure_fails_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#402: a publish_tools failure is best-effort (log still runs) but must
    NOT vanish — it is aggregated and fails the command's exit code."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))
    fake_publish = MagicMock(side_effect=RuntimeError("UC permission denied"))

    result = _invoke_model_serving_deploy(
        [], log_agent=fake_log_agent, publish=fake_publish,
    )

    assert result.exit_code != 0, result.output
    # Best-effort semantics preserved: log + register still ran.
    fake_log_agent.assert_called_once()
    # The failure is named in the final error, not just a mid-flow warning.
    assert "publish-tools" in result.output
    assert "UC permission denied" in result.output
    assert "failed sub-step" in result.output


def test_deploy_set_uc_tags_failure_fails_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#402: a set_uc_tags failure must exit non-zero — an untagged agent
    silently never shows up in list / topology / watchdog."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_set_tags = MagicMock(side_effect=RuntimeError("tag write denied"))

    result = _invoke_model_serving_deploy([], set_tags=fake_set_tags)

    assert result.exit_code != 0, result.output
    assert "set-uc-tags" in result.output
    assert "tag write denied" in result.output


def test_deploy_midflow_abort_names_orphaned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#402: if log_agent raises after tools were published, the error names
    the already-created (orphaned) UC tools."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_publish = MagicMock(return_value=[
        SimpleNamespace(uc_name="main.tools.classify_intent", grants_applied=["agent_consumers"]),
    ])
    fake_log_agent = MagicMock(side_effect=RuntimeError("mlflow exploded"))

    result = _invoke_model_serving_deploy(
        [], log_agent=fake_log_agent, publish=fake_publish,
    )

    assert result.exit_code != 0, result.output
    assert "log_agent failed" in result.output
    assert "mlflow exploded" in result.output
    # The orphaned published tool is named so the operator can clean up.
    assert "main.tools.classify_intent" in result.output
    assert "NOT rolled back" in result.output


def test_deploy_model_serving_json_output_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#405: --json-output emits one parseable JSON object on stdout with
    ok / uc_name / version / per-step outcomes; progress goes to stderr."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke_model_serving_deploy(["--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)   # stdout is ONLY the JSON object
    assert payload["ok"] is True
    assert payload["uc_name"] == "main.agents.x"
    assert payload["version"] == "1"
    assert payload["steps"] == {
        "publish_tools": "ok",
        "log": "ok",
        "deploy": "skipped",     # --no-deploy
        "gate": "skipped",       # --no-deploy: nothing to health-check (#406)
        "set_uc_tags": "ok",
    }
    # Progress logs were routed to stderr, not stdout.
    assert "Logged main.agents.x" in result.stderr


def test_deploy_model_serving_json_output_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#405: --json-output emits {ok: false, error, steps} and exits 1 when a
    sub-step fails."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_set_tags = MagicMock(side_effect=RuntimeError("tag write denied"))

    result = _invoke_model_serving_deploy(["--json-output"], set_tags=fake_set_tags)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "set-uc-tags: tag write denied" in payload["error"]
    assert payload["steps"]["set_uc_tags"] == "failed"
    assert payload["steps"]["log"] == "ok"


_POISONED_LOCK = (
    'source = { registry = "https://pypi-proxy.dev.databricks.com/simple" }\n'
    'url = "https://pypi-proxy.dev.databricks.com/packages/ab/cd/foo-1.0-py3-none-any.whl"\n'
)


def test_deploy_failure_restores_sanitized_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#416: the model-serving deploy sanitizes the project's OWN uv.lock in
    place; if the deploy then fails, the original bytes must come back — the
    working tree must not be left silently mutated."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text(_POISONED_LOCK)

    fake_log_agent = MagicMock(side_effect=RuntimeError("mlflow exploded"))
    result = _invoke_model_serving_deploy([], log_agent=fake_log_agent)

    assert result.exit_code != 0, result.output
    assert (tmp_path / "uv.lock").read_text() == _POISONED_LOCK  # byte-identical
    assert "restored original uv.lock" in result.output


def test_deploy_success_keeps_sanitized_uv_lock_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#416: on success the sanitized (public-PyPI) lock is kept — repo policy —
    and the in-place rewrite is announced instead of happening silently."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text(_POISONED_LOCK)

    result = _invoke_model_serving_deploy([])

    assert result.exit_code == 0, result.output
    text = (tmp_path / "uv.lock").read_text()
    assert "pypi-proxy.dev.databricks.com" not in text
    assert 'registry = "https://pypi.org/simple"' in text
    assert "rewrote uv.lock in place" in result.output


def test_deploy_warns_on_unknown_mirror_in_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#416: an unknown mirror host in uv.lock isn't rewritten (no invented
    rules) and doesn't block the deploy, but the warning names the host."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    mirror_lock = (
        'source = { registry = "https://artifactory.corp.example.com/api/pypi/simple" }\n'
    )
    (tmp_path / "uv.lock").write_text(mirror_lock)

    result = _invoke_model_serving_deploy([])

    assert result.exit_code == 0, result.output  # warn, don't block
    assert (tmp_path / "uv.lock").read_text() == mirror_lock  # untouched
    assert "WARNING" in result.output
    assert "artifactory.corp.example.com" in result.output


# ---------------------------------------------------------------------------
# model-serving deploy: health gate (#406) — endpoint READY + smoke invocation
# ---------------------------------------------------------------------------


def _endpoint_state(ready: str, config_update: str) -> SimpleNamespace:
    """Shape of ``ws.serving_endpoints.get(...)`` the gate reads."""
    return SimpleNamespace(
        state=SimpleNamespace(ready=ready, config_update=config_update),
    )


def _invoke_serving_deploy_with_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    *,
    ws: MagicMock,
    deploy_mock: MagicMock | None = None,
) -> Result:
    """Invoke `agents deploy --target model-serving` WITHOUT --no-deploy.

    ``databricks.agents`` (not installed in the test venv) is faked with a
    deploy() returning an endpoint name (pass ``deploy_mock`` to inspect the
    kwargs it received), and the SDK WorkspaceClient behind the health gate
    is replaced with ``ws``.
    """
    import types as _types

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    if deploy_mock is None:
        deploy_mock = MagicMock(
            return_value=SimpleNamespace(endpoint_name="agents_main-agents-x"),
        )
    fake_agents = _types.ModuleType("databricks.agents")
    fake_agents.deploy = deploy_mock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks.agents", fake_agents)

    runner = CliRunner()
    with patch(
        "apx_agent.log_agent",
        MagicMock(return_value=SimpleNamespace(registered_model_version="1")),
    ), \
         patch("apx_agent.set_uc_tags_for_agent",
               MagicMock(return_value={"apx.agent.name": "x"})), \
         patch("databricks.sdk.WorkspaceClient", return_value=ws), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "agents", "deploy",
            "--target", "model-serving",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-publish-tools",
        ] + extra_args)
    sys.modules.pop("tmp_test_agent", None)
    return result


def test_deploy_serving_gate_pass_is_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: endpoint READY + smoke invocation answered → exit 0, steps.gate=ok."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state("READY", "NOT_UPDATING")
    ws.serving_endpoints.query.return_value = SimpleNamespace(choices=[])

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--json-output"], ws=ws,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["endpoint"] == "agents_main-agents-x"
    assert payload["steps"]["deploy"] == "ok"
    assert payload["steps"]["gate"] == "ok"
    # Exactly one smoke invocation, against the endpoint the deploy named.
    ws.serving_endpoints.query.assert_called_once()
    assert ws.serving_endpoints.query.call_args.kwargs["name"] == "agents_main-agents-x"


def test_deploy_serving_gate_update_failed_fails_and_names_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: a terminal UPDATE_FAILED fast-fails the deploy — non-zero exit,
    version + endpoint named, escape hatch mentioned, no smoke query sent."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state(
        "NOT_READY", "UPDATE_FAILED",
    )

    result = _invoke_serving_deploy_with_gate(tmp_path, monkeypatch, [], ws=ws)

    assert result.exit_code != 0, result.output
    assert "version 1" in result.output
    assert "agents_main-agents-x" in result.output
    assert "UPDATE_FAILED" in result.output
    assert "--no-readyz-gate" in result.output
    ws.serving_endpoints.query.assert_not_called()


def test_deploy_serving_gate_timeout_names_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: an endpoint that never reaches READY times out the gate — the
    deploy fails and the error names the deployed version. Time is faked so
    the 300s poll budget elapses instantly."""
    from apx_agent import cli as cli_mod

    class _Clock:
        now = 0.0

        def time(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    monkeypatch.setattr(cli_mod, "time", _Clock())

    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state(
        "NOT_READY", "IN_PROGRESS",
    )

    result = _invoke_serving_deploy_with_gate(tmp_path, monkeypatch, [], ws=ws)

    assert result.exit_code != 0, result.output
    assert "timed out after 300s" in result.output
    assert "version 1" in result.output
    assert "--no-readyz-gate" in result.output
    ws.serving_endpoints.query.assert_not_called()


def test_deploy_serving_gate_smoke_failure_fails_with_json_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: READY endpoint whose smoke invocation errors → non-zero exit;
    the --json-output failure carries the gate detail + step ledger."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state("READY", "NOT_UPDATING")
    ws.serving_endpoints.query.side_effect = RuntimeError("boom")

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--json-output"], ws=ws,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["steps"]["deploy"] == "ok"
    assert payload["steps"]["gate"] == "failed"
    assert payload["version"] == "1"
    assert payload["endpoint"] == "agents_main-agents-x"
    assert "smoke invocation failed" in payload["error"]
    assert "boom" in payload["error"]
    assert "--no-readyz-gate" in payload["error"]


def test_deploy_serving_no_readyz_gate_skips_poll_and_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: --no-readyz-gate is the escape hatch — no poll, no smoke query,
    steps.gate=skipped, deploy still green."""
    ws = MagicMock()

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--no-readyz-gate", "--json-output"], ws=ws,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["steps"]["deploy"] == "ok"
    assert payload["steps"]["gate"] == "skipped"
    ws.serving_endpoints.get.assert_not_called()
    ws.serving_endpoints.query.assert_not_called()


def test_deploy_serving_no_deploy_skips_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#406: --no-deploy never touches the gate — nothing was deployed, so
    there is nothing to health-check (steps.gate=skipped, exit 0)."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke_model_serving_deploy(["--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"]["deploy"] == "skipped"
    assert payload["steps"]["gate"] == "skipped"


# ---------------------------------------------------------------------------
# model-serving deploy: scale passthrough + --env-suffix naming (#407)
# ---------------------------------------------------------------------------


def _ready_ws() -> MagicMock:
    """A WorkspaceClient fake whose endpoint is READY and answers the smoke."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state("READY", "NOT_UPDATING")
    ws.serving_endpoints.query.return_value = SimpleNamespace(choices=[])
    return ws


def test_deploy_serving_scale_flags_reach_agents_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: --scale-to-zero + --workload-size are forwarded verbatim to
    databricks.agents.deploy and land in the --json-output summary."""
    deploy_mock = MagicMock(
        return_value=SimpleNamespace(endpoint_name="agents_main-agents-x"),
    )

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch,
        ["--scale-to-zero", "--workload-size", "Medium", "--json-output"],
        ws=_ready_ws(), deploy_mock=deploy_mock,
    )

    assert result.exit_code == 0, result.output
    kwargs = deploy_mock.call_args.kwargs
    assert kwargs["scale_to_zero"] is True
    assert kwargs["workload_size"] == "Medium"
    payload = json.loads(result.stdout)
    assert payload["scale_to_zero"] is True
    assert payload["workload_size"] == "Medium"


def test_deploy_serving_no_scale_to_zero_forwards_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: the flag is tri-state — --no-scale-to-zero forwards an explicit
    False (distinct from "not passed", which forwards nothing)."""
    deploy_mock = MagicMock(
        return_value=SimpleNamespace(endpoint_name="agents_main-agents-x"),
    )

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--no-scale-to-zero", "--json-output"],
        ws=_ready_ws(), deploy_mock=deploy_mock,
    )

    assert result.exit_code == 0, result.output
    kwargs = deploy_mock.call_args.kwargs
    assert kwargs["scale_to_zero"] is False
    assert "workload_size" not in kwargs
    payload = json.loads(result.stdout)
    assert payload["scale_to_zero"] is False
    assert "workload_size" not in payload


def test_deploy_serving_unset_scale_flags_are_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: when neither scale flag is passed, NOTHING is forwarded —
    databricks.agents.deploy's own defaults stay in charge — and the JSON
    summary omits the keys."""
    deploy_mock = MagicMock(
        return_value=SimpleNamespace(endpoint_name="agents_main-agents-x"),
    )

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--json-output"],
        ws=_ready_ws(), deploy_mock=deploy_mock,
    )

    assert result.exit_code == 0, result.output
    kwargs = deploy_mock.call_args.kwargs
    assert "scale_to_zero" not in kwargs
    assert "workload_size" not in kwargs
    payload = json.loads(result.stdout)
    assert "scale_to_zero" not in payload
    assert "workload_size" not in payload


def test_deploy_env_suffix_suffixes_uc_name_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: --env-suffix staging registers + deploys under
    catalog.schema.model-staging — a naming convention, applied once, that
    flows through log, deploy, and the JSON summary."""
    deploy_mock = MagicMock(
        return_value=SimpleNamespace(endpoint_name="agents_main-agents-x-staging"),
    )

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--env-suffix", "staging", "--json-output"],
        ws=_ready_ws(), deploy_mock=deploy_mock,
    )

    assert result.exit_code == 0, result.output
    # databricks.agents.deploy saw the suffixed UC name (positional arg 0).
    assert deploy_mock.call_args.args[0] == "main.agents.x-staging"
    payload = json.loads(result.stdout)
    assert payload["uc_name"] == "main.agents.x-staging"
    assert payload["steps"]["deploy"] == "ok"


def test_deploy_name_falls_back_to_configured_registered_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: --name is optional when [tool.apx.agent].registered_model is set
    in pyproject.toml — the configured UC name is used end-to-end."""
    _write_agent_module(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\nregistered_model = "main.agents.from_cfg"\n'
    )
    monkeypatch.chdir(tmp_path)

    fake_log_agent = MagicMock(
        return_value=SimpleNamespace(registered_model_version="1"),
    )
    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("apx_agent.set_uc_tags_for_agent",
               MagicMock(return_value={"apx.agent.name": "x"})), \
         patch("mlflow.start_run"):
        result = runner.invoke(main, [
            "agents", "deploy",
            "--target", "model-serving",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--no-publish-tools",
            "--no-deploy",
            "--json-output",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert (
        fake_log_agent.call_args.kwargs["registered_model_name"]
        == "main.agents.from_cfg"
    )
    payload = json.loads(result.stdout)
    assert payload["uc_name"] == "main.agents.from_cfg"


def test_deploy_name_missing_and_unconfigured_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#407: with no --name and no configured registered_model, the error
    names BOTH ways to provide the UC name."""
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [
        "agents", "deploy",
        "--target", "model-serving",
        "--module", "tmp_test_agent:agent",
        "--model", "databricks-claude-sonnet-4-6",
        "--no-deploy",
    ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code != 0
    assert "--name is required" in result.output
    assert "[tool.apx.agent].registered_model" in result.output


# ---------------------------------------------------------------------------
# model-serving deploy: --dry-run plan, --timeout, --version redeploy (#414)
# ---------------------------------------------------------------------------


def test_deploy_serving_dry_run_prints_plan_without_logging_or_lock_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#414: --dry-run on model-serving prints the plan (with --env-suffix
    applied to the UC name) and exits 0 — no log_agent call, no
    databricks.agents import, and uv.lock is byte-identical (no sanitize)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text(_POISONED_LOCK)
    lock_before = (tmp_path / "uv.lock").read_bytes()

    fake_log_agent = MagicMock(
        side_effect=AssertionError("log_agent must not run under --dry-run"),
    )
    with patch("apx_agent.log_agent", fake_log_agent):
        result = CliRunner().invoke(main, [
            "agents", "deploy",
            "--target", "model-serving",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--env-suffix", "staging",
            "--dry-run",
        ])

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.stdout
    assert "main.agents.x-staging" in result.stdout  # suffix applied in plan
    assert "publish_tools: run" in result.stdout
    assert "log: run" in result.stdout
    fake_log_agent.assert_not_called()
    assert (tmp_path / "uv.lock").read_bytes() == lock_before  # untouched
    # databricks.agents is not installed in the test venv — a real import
    # attempt would have failed the command, and it must not be loaded.
    assert "databricks.agents" not in sys.modules


def test_deploy_serving_dry_run_json_plan_with_config_fallback_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#414: --dry-run --json-output emits {plan: true, ...} with the UC name
    resolved from [tool.apx.agent].registered_model and the --version
    redeploy step list (publish_tools + log skipped)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.apx.agent]\nregistered_model = "main.agents.from_cfg"\n'
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [
        "agents", "deploy",
        "--target", "model-serving",
        "--model", "databricks-claude-sonnet-4-6",
        "--version", "3",
        "--no-set-uc-tags",
        "--dry-run", "--json-output",
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["plan"] is True
    assert payload["target"] == "model-serving"
    assert payload["uc_name"] == "main.agents.from_cfg"
    assert payload["version"] == 3
    assert payload["steps"]["publish_tools"] == "skipped (--version redeploy)"
    assert payload["steps"]["log"] == "skipped (--version redeploy)"
    assert payload["steps"]["deploy"] == "run"
    assert payload["steps"]["set_uc_tags"] == "skipped (--no-set-uc-tags)"


def test_timeout_flag_reaches_serving_health_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#414: --timeout drives the serving gate's READY poll budget — the same
    flag that bounds the apps ACTIVE/RUNNING poll."""
    gate = MagicMock(return_value=None)
    monkeypatch.setattr("apx_agent.cli._serving_health_gate", gate)

    result = _invoke_serving_deploy_with_gate(
        tmp_path, monkeypatch, ["--timeout", "77"], ws=MagicMock(),
    )

    assert result.exit_code == 0, result.output
    assert gate.call_args.kwargs["timeout_seconds"] == 77


def test_successful_model_serving_deploy_records_local_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_path = tmp_path / "deploy-history.json"
    monkeypatch.setattr("apx_agent.cli._deploy_history_path", lambda: history_path)

    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_state("READY", "NOT_UPDATING")
    ws.serving_endpoints.query.return_value = SimpleNamespace(choices=[])

    result = _invoke_serving_deploy_with_gate(tmp_path, monkeypatch, [], ws=ws)

    assert result.exit_code == 0, result.output
    entry = json.loads(history_path.read_text())["main.agents.x"]
    assert entry["path"] == str(tmp_path)
    assert entry["target"] == "model-serving"


def test_no_deploy_flag_records_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-deploy logs/registers a version but never actually deploys —
    no history entry, since nothing is actually running."""
    history_path = tmp_path / "deploy-history.json"
    monkeypatch.setattr("apx_agent.cli._deploy_history_path", lambda: history_path)

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_log_agent = MagicMock(return_value=SimpleNamespace(registered_model_version="1"))

    with patch("apx_agent.log_agent", fake_log_agent), patch("mlflow.start_run"):
        result = CliRunner().invoke(main, [
            "agents", "deploy", "--target", "model-serving",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy", "--no-publish-tools",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    assert not history_path.exists()


def test_deploy_version_redeploys_logged_version_skipping_publish_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#414: --version N never calls publish_tools_to_uc or log_agent
    (ledger: skipped) and goes straight to databricks.agents.deploy(name, N)
    + gate + tags."""
    import types as _types

    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text(_POISONED_LOCK)

    deploy_mock = MagicMock(
        return_value=SimpleNamespace(endpoint_name="agents_main-agents-x"),
    )
    fake_agents = _types.ModuleType("databricks.agents")
    fake_agents.deploy = deploy_mock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks.agents", fake_agents)
    fake_log_agent = MagicMock(
        side_effect=AssertionError("log_agent must not run with --version"),
    )
    fake_publish = MagicMock(
        side_effect=AssertionError("publish_tools must not run with --version"),
    )

    runner = CliRunner()
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("apx_agent.publish_tools_to_uc", fake_publish), \
         patch("apx_agent.set_uc_tags_for_agent",
               MagicMock(return_value={"apx.agent.name": "x"})), \
         patch("databricks.sdk.WorkspaceClient", return_value=_ready_ws()):
        result = runner.invoke(main, [
            "agents", "deploy",
            "--target", "model-serving",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--version", "7",
            "--json-output",
        ])
    sys.modules.pop("tmp_test_agent", None)

    assert result.exit_code == 0, result.output
    fake_log_agent.assert_not_called()
    fake_publish.assert_not_called()
    assert deploy_mock.call_args.args[0] == "main.agents.x"
    assert deploy_mock.call_args.kwargs["model_version"] == "7"
    payload = json.loads(result.stdout)
    assert payload["version"] == "7"
    assert payload["steps"]["publish_tools"] == "skipped"
    assert payload["steps"]["log"] == "skipped"
    assert payload["steps"]["deploy"] == "ok"
    assert payload["steps"]["gate"] == "ok"
    # A redeploy logs nothing, so the lock sanitize never runs (#414).
    assert (tmp_path / "uv.lock").read_text() == _POISONED_LOCK


def test_deploy_version_with_no_deploy_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#414: --version + --no-deploy is a no-op combination — refuse it."""
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [
        "agents", "deploy",
        "--target", "model-serving",
        "--model", "databricks-claude-sonnet-4-6",
        "--name", "main.agents.x",
        "--version", "7",
        "--no-deploy",
    ])

    assert result.exit_code != 0
    assert "--version" in result.output
    assert "--no-deploy" in result.output


# ---------------------------------------------------------------------------
# `apx watchdog violations` / `apx watchdog status`
# ---------------------------------------------------------------------------


def test_watchdog_violations_requires_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APX_WATCHDOG_VIOLATIONS_TABLE", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["watchdog", "violations"])
    assert result.exit_code != 0
    assert "table" in result.output.lower() or "table" in result.stderr.lower()


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
    assert "Pass --agent" in result.output or "Pass --agent" in result.stderr


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
    assert "No apx agents found" in result.output


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
    assert "either --endpoint" in result.output or "either --endpoint" in result.stderr


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
        assert "Missing option" in (result.output + result.stderr)
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
            "--no-publish-tools",
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
    """Each layout maps to a (target, reason) pair; apps is the catch-all default (#411)."""
    from apx_agent.cli import _detect_target

    # Empty directory → apps (catch-all default)
    assert _detect_target(tmp_path) == ("apps", "no layout markers; apps is the default")
    # Flat app.py without databricks.yml → model-serving
    (tmp_path / "app.py").write_text("app = None\n")
    assert _detect_target(tmp_path) == (
        "model-serving", "app.py present without databricks.yml",
    )
    # databricks.yml routes to apps even alongside app.py (the ADK-style case)
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    assert _detect_target(tmp_path) == ("apps", "databricks.yml present")
    # apps layout marker wins over everything else
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("app = None\n")
    assert _detect_target(tmp_path) == ("apps", "agent_server/start_server.py present")


def test_deploy_echoes_autodetected_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-detection echoes target + reason; explicit --target stays silent (#411)."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    with patch("apx_agent.cli._deploy_apps", return_value=None):
        detected = runner.invoke(main, ["agents", "deploy"])
        assert detected.exit_code == 0, detected.output
        assert "target: apps (auto-detected: databricks.yml present" in detected.stderr

        explicit = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
        assert explicit.exit_code == 0, explicit.output
        assert "auto-detected" not in explicit.stderr


def test_preflight_apps_suggests_model_serving_for_adk_layout(tmp_path: Path) -> None:
    """agent.py + databricks.yml misrouted to apps → suggest --target model-serving (#411)."""
    import click

    from apx_agent.cli import _preflight_apps

    (tmp_path / "agent.py").write_text("agent = None\n")
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname = 'x'\n")
    with pytest.raises(click.ClickException) as excinfo:
        _preflight_apps(tmp_path)
    assert "--target model-serving" in excinfo.value.message
    assert "scaffold" not in excinfo.value.message

    # A bare directory keeps the scaffold hint — nothing suggests model-serving.
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(click.ClickException) as excinfo:
        _preflight_apps(bare)
    assert "scaffold" in excinfo.value.message
    assert "--target model-serving" not in excinfo.value.message


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


def test_sanitize_uv_lock_rewrites_proxy_package_urls(tmp_path: Path) -> None:
    """Some proxies (pypi-proxy.dev.databricks.com) also serve the wheel files
    themselves; those /packages/ URLs are unreachable from a deployed App and
    must be re-pointed at files.pythonhosted.org (same path layout)."""
    from apx_agent.cli import _sanitize_uv_lock

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://pypi-proxy.dev.databricks.com/simple" }\n'
        'url = "https://pypi-proxy.dev.databricks.com/packages/09/7d/abc/scipy-1.17.1.whl"\n'
    )
    assert _sanitize_uv_lock(lock) is True
    text = lock.read_text()
    assert "pypi-proxy.dev.databricks.com" not in text
    assert (
        "https://files.pythonhosted.org/packages/09/7d/abc/scipy-1.17.1.whl" in text
    )


def test_warn_unknown_lock_mirrors_names_hosts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """#416: a mirror with no rewrite rule (Artifactory, a prod proxy variant)
    survives _sanitize_uv_lock unchanged; the deploy must warn loudly, name the
    host, and say the container's `uv sync` will likely fail."""
    from apx_agent.cli import _sanitize_uv_lock, _warn_unknown_lock_mirrors

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://artifactory.corp.example.com/api/pypi/simple" }\n'
        'url = "https://artifactory.corp.example.com/pypi/foo-1.0-py3-none-any.whl"\n'
    )
    # No rewrite rule is invented for unknown mirrors — the file is untouched.
    assert _sanitize_uv_lock(lock) is False
    _warn_unknown_lock_mirrors(lock)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "artifactory.corp.example.com" in err
    assert "uv sync" in err


def test_warn_unknown_lock_mirrors_silent_on_public_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """#416: public-PyPI locks — including git+https sources like apx-agent
    pinned from GitHub — produce no mirror warning."""
    from apx_agent.cli import _warn_unknown_lock_mirrors

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://pypi.org/simple" }\n'
        'source = { git = "https://github.com/stuagano/apx-agent.git?rev=abc" }\n'
        'url = "https://files.pythonhosted.org/x/foo-1.0-py3-none-any.whl"\n'
    )
    _warn_unknown_lock_mirrors(lock)
    assert capsys.readouterr().err == ""


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


def test_scaffold_apps_readme_documents_promotion() -> None:
    """The Apps README documents the dev->staging->prod promotion recipe:
    override variables per target in databricks.yml, then deploy with
    --bundle-target/--profile (#323) — no new command, existing flags."""
    from apx_agent.cli import _SCAFFOLD_APPS_README

    assert "Promoting to another environment" in _SCAFFOLD_APPS_README
    assert "--bundle-target staging" in _SCAFFOLD_APPS_README
    assert "--profile" in _SCAFFOLD_APPS_README
    assert "targets:" in _SCAFFOLD_APPS_README


def test_scaffold_bakes_data_target_from_flags(tmp_path: Path) -> None:
    """--catalog/--schema bake the default DataAgent's data source (no probe),
    as an env-var-overridable default (#323) — not a literal call, so a
    deployed app's APX_CATALOG/APX_SCHEMA env vars can override it per
    environment."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["agents", "scaffold", "ag", "--catalog", "main", "--schema", "sales",
               "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    agent_py = (tmp_path / "ag" / "agent.py").read_text()
    assert '_CATALOG = "main"' in agent_py
    assert '_SCHEMA = "sales"' in agent_py
    assert 'os.environ.get("APX_CATALOG", _CATALOG)' in agent_py
    assert 'os.environ.get("APX_SCHEMA", _SCHEMA)' in agent_py
    assert 'DataAgent("main", "sales"' not in agent_py


def test_scaffold_builds_workspace_client_once_across_branches(tmp_path, monkeypatch) -> None:
    """#522: a flag combo that reaches both the explicit-sanity-check branch
    and the interactive-wizard branch must construct the scaffold-time
    WorkspaceClient once, not once per branch."""
    from apx_agent import cli

    calls = {"n": 0}

    def _counting_ws(profile):
        calls["n"] += 1
        return object()

    monkeypatch.setattr(cli, "_make_ws_for_scaffold", _counting_ws)
    # Keep the branches cheap and offline: the sanity check and wizard both
    # receive the cached client; stub their side effects out.
    monkeypatch.setattr(cli, "_scaffold_sanity_check", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_scaffold_wizard",
        lambda ws, target, template, catalog, schema: (
            "apps", "data", catalog, schema, None, None, None,
        ),
    )
    monkeypatch.setattr(cli, "_probe_first_table", lambda *a, **k: None)
    # Stop at the project writer — the manifest step is a separate helper with
    # its own client (out of #522's branch-dedup scope); isolate the branches.
    monkeypatch.setattr(cli, "_scaffold_apps", lambda *a, **k: None)

    result = CliRunner().invoke(main, [
        "agents", "scaffold", "ag",
        "--template", "data", "--catalog", "main", "--schema", "sales",
        "--interactive", "--dir", str(tmp_path),
    ])

    assert result.exit_code == 0, result.output
    assert calls["n"] == 1, f"expected one WorkspaceClient build, got {calls['n']}"


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
                   "--dir", str(tmp_path)],
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
    result = runner.invoke(main, ["agents", "scaffold", "my-agent"])
    assert result.exit_code == 0
    out = result.output
    assert "cd my-agent" in out
    assert "uv sync" in out
    assert "apx-agent agents run" in out


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


def test_apps_databricks_yml_has_staging_target_for_all_templates(tmp_path):
    """The staging target is unconditional — every --target apps template
    gets dev/staging/prod, regardless of whether it has a catalog/schema."""
    from apx_agent import cli
    cli._scaffold_apps(tmp_path, "demo", force=True, catalog="", schema="", template="base")
    yml = (tmp_path / "databricks.yml").read_text()
    assert "  staging:" in yml
    assert "    mode: production" in yml
    # staging appears between dev and prod, shaped like prod.
    dev_idx = yml.index("  dev:")
    staging_idx = yml.index("  staging:")
    prod_idx = yml.index("  prod:")
    assert dev_idx < staging_idx < prod_idx


def test_apps_databricks_yml_catalog_schema_vars_for_data_template(tmp_path):
    """A data/coworker template's databricks.yml declares catalog/schema DAB
    variables (defaulting to the scaffolded values) and wires them into the
    app's env as APX_CATALOG/APX_SCHEMA (#323)."""
    from apx_agent import cli
    cli._scaffold_apps(tmp_path, "demo", force=True,
                       catalog="samples", schema="tpch", table="customer",
                       template="data")
    yml = (tmp_path / "databricks.yml").read_text()
    assert "  catalog:" in yml
    assert "    default: samples" in yml
    assert "  schema:" in yml
    assert "    default: tpch" in yml
    assert "- name: APX_CATALOG" in yml
    assert "value: ${var.catalog}" in yml
    assert "- name: APX_SCHEMA" in yml
    assert "value: ${var.schema}" in yml


def test_apps_databricks_yml_no_catalog_vars_for_base_template(tmp_path):
    """A base (LlmAgent, no data source) template's databricks.yml gets the
    staging target but NOT dead catalog/schema config (#323)."""
    from apx_agent import cli
    cli._scaffold_apps(tmp_path, "demo", force=True, catalog="", schema="", template="base")
    yml = (tmp_path / "databricks.yml").read_text()
    assert "  catalog:" not in yml
    assert "  schema:" not in yml
    assert "APX_CATALOG" not in yml
    assert "APX_SCHEMA" not in yml


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
            # no_run must be False: --no-run now skips the readiness poll and
            # the readyz gate entirely (issue #413), and this helper exists to
            # drive the deploy INTO the gate. bundle run is mocked to rc 0.
            no_run=False,
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
# databricks.yml merge writers preserve comments/formatting (#527)
# ---------------------------------------------------------------------------


class TestDatabricksYmlMergePreservesComments:
    _SEED = (
        "# top-level comment: do not remove me\n"
        "bundle:\n"
        "  name: my-agent\n"
        "\n"
        "resources:\n"
        "  apps:\n"
        "    my-agent:\n"
        "      name: my-agent  # inline comment\n"
        "      # a note about this block\n"
        "      config: {}\n"
    )

    def test_merge_env_preserves_comments(self, tmp_path):
        from apx_agent.cli import _merge_env_into_databricks_yml, _EnvPair

        yml_path = tmp_path / "databricks.yml"
        yml_path.write_text(self._SEED)

        result = _merge_env_into_databricks_yml(
            tmp_path,
            bundle_key="my-agent",
            env_pairs=[_EnvPair(name="FOO", value="bar")],
            secret_env_pairs=[],
            log=lambda msg: None,
        )

        out = yml_path.read_text()
        assert "# top-level comment: do not remove me" in out
        assert "# inline comment" in out
        assert "# a note about this block" in out
        assert "FOO" in out
        assert result.env_added == ["FOO"]

    def test_merge_env_quotes_yaml_ambiguous_values(self, tmp_path):
        # ruamel's round-trip dumper does not auto-quote plain scalars that
        # YAML's implicit resolver would misread as bool/null/number on
        # reload (on/off/yes/no/true/false/null/123) — unlike yaml.safe_dump,
        # which does. Left unquoted, --env FLAG=on would silently become the
        # Python bool True (not the string "on") the next time this file is
        # read. Covers both --env and --secret-env free-text fields.
        import yaml as pyyaml
        from apx_agent.cli import _merge_env_into_databricks_yml, _EnvPair, _SecretEnvRef

        yml_path = tmp_path / "databricks.yml"
        yml_path.write_text(
            "resources:\n"
            "  apps:\n"
            "    my-agent:\n"
            "      name: my-agent\n"
            "      config: {}\n"
        )

        _merge_env_into_databricks_yml(
            tmp_path,
            bundle_key="my-agent",
            env_pairs=[_EnvPair(name="FEATURE_FLAG", value="on"), _EnvPair(name="123", value="123")],
            secret_env_pairs=[_SecretEnvRef(name="NO", scope="yes", key="true")],
            log=lambda msg: None,
        )

        reloaded = pyyaml.safe_load(yml_path.read_text())
        app = reloaded["resources"]["apps"]["my-agent"]
        env_by_name = {e["name"]: e.get("value") for e in app["config"]["env"]}
        assert env_by_name == {"FEATURE_FLAG": "on", "123": "123", "NO": None}
        secret_resource = next(r["secret"] for r in app["resources"] if "secret" in r)
        assert secret_resource["scope"] == "yes"
        assert secret_resource["key"] == "true"

    def test_merge_env_never_clobbers_existing_entry(self, tmp_path):
        from apx_agent.cli import _merge_env_into_databricks_yml, _EnvPair

        yml_path = tmp_path / "databricks.yml"
        yml_path.write_text(
            "resources:\n"
            "  apps:\n"
            "    my-agent:\n"
            "      name: my-agent\n"
            "      config:\n"
            "        env:\n"
            "        - name: FOO\n"
            "          value: original\n"
        )

        result = _merge_env_into_databricks_yml(
            tmp_path,
            bundle_key="my-agent",
            env_pairs=[_EnvPair(name="FOO", value="new")],
            secret_env_pairs=[],
            log=lambda msg: None,
        )

        assert result.skipped == ["FOO"]
        assert "original" in yml_path.read_text()
        assert "new" not in yml_path.read_text()

    def test_auto_update_yml_preserves_comments(self, tmp_path, monkeypatch):
        from apx_agent.cli import _auto_update_databricks_yml

        yml_path = tmp_path / "databricks.yml"
        yml_path.write_text(self._SEED)
        monkeypatch.setattr(
            "apx_agent._resources.collect_resource_specs", lambda agent: [],
        )
        monkeypatch.setattr(
            "apx_agent._resources.user_api_scopes_for", lambda specs: [],
        )

        _auto_update_databricks_yml(
            tmp_path, agent=object(), bundle_key="my-agent", log=lambda msg: None,
        )

        out = yml_path.read_text()
        assert "# top-level comment: do not remove me" in out
        assert "# inline comment" in out
        assert "# a note about this block" in out


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


def test_describe_reads_instructions_from_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scaffold→describe loop: a YAML spec's instructions are read back.
    (tmp_path / "sf.yaml").write_text(
        "name: sf\nmodel: databricks-claude-sonnet-4-6\n"
        "instructions: Answer Salesforce pipeline questions.\n"
    )
    monkeypatch.chdir(tmp_path)
    # Explicit spec
    res = CliRunner().invoke(main, ["agents", "describe", "sf.yaml"])
    assert res.exit_code == 0, res.output
    assert "Answer Salesforce pipeline questions." in res.output
    # Auto-detected lone spec (no args)
    res = CliRunner().invoke(main, ["agents", "describe"])
    assert res.exit_code == 0, res.output
    assert "Answer Salesforce pipeline questions." in res.output


def test_describe_spec_empty_instructions_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sf.yaml").write_text(
        "name: sf\nmodel: databricks-claude-sonnet-4-6\ninstructions: ''\n"
    )
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["agents", "describe", "sf.yaml"])
    assert res.exit_code == 0, res.output
    assert "empty" in res.output.lower()


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

    def test_apps_data_agent_reads_catalog_schema_from_env(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="data")
        agent_py = (tmp_path / "agent.py").read_text()
        assert "import os" in agent_py
        assert '_CATALOG = "samples"' in agent_py
        assert '_SCHEMA = "tpch"' in agent_py
        assert 'os.environ.get("APX_CATALOG", _CATALOG)' in agent_py
        assert 'os.environ.get("APX_SCHEMA", _SCHEMA)' in agent_py

    def test_apps_coworker_reads_catalog_schema_from_env(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold",
                            lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True,
                           catalog="samples", schema="tpch", table="customer",
                           template="coworker")
        agent_py = (tmp_path / "agent.py").read_text()
        assert "import os" in agent_py
        assert '_CATALOG = "samples"' in agent_py
        assert '_SCHEMA = "tpch"' in agent_py
        assert 'os.environ.get("APX_CATALOG", _CATALOG)' in agent_py
        assert 'os.environ.get("APX_SCHEMA", _SCHEMA)' in agent_py


def test_coworker_flag_no_longer_documents_generate() -> None:
    result = CliRunner().invoke(main, ["agents", "scaffold", "--help"])
    assert result.exit_code == 0
    assert "'generate' to LLM-author" not in result.output


def test_coworker_generate_value_is_rejected_or_treated_as_a_name(tmp_path: Path) -> None:
    # "generate" is no longer special — it's now just an (invalid) gallery name.
    result = CliRunner().invoke(
        main,
        ["agents", "scaffold", "x", "--coworker", "generate", "--dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "No coworker named 'generate'" in result.output


def test_coworker_gallery_pick_materializes_full_project(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my-payroll",
            "--coworker", "payroll",
            "--catalog", "main", "--schema", "payroll_demo",
            "--dir", str(tmp_path),
            "--no-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my-payroll"
    assert (base / "agent.py").exists()
    assert (base / "pyproject.toml").exists()
    assert not (tmp_path / "my-payroll.yaml").exists(), (
        "gallery pick must no longer write a standalone .yaml"
    )
    pyproject = (base / "pyproject.toml").read_text()
    assert "[tool.apx.agent]" in pyproject

    # Prove agent.py actually imports cleanly, not just that it exists —
    # same standard Task 9's generate test holds itself to.
    prev = os.getcwd()
    os.chdir(base)
    try:
        describe_result = CliRunner().invoke(main, ["agents", "describe"])
    finally:
        os.chdir(prev)
    assert describe_result.exit_code == 0, describe_result.output


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

    def test_default_yaml_scaffold_error_points_to_target_flag(self, tmp_path, monkeypatch):
        # Issue #520: the default `agents scaffold NAME` (no --target) writes
        # only a YAML spec, never .apx/schema.json. The error from
        # refresh-schema after that default scaffold must point at the flag
        # that actually produces a project scaffold (--target), not repeat
        # the bare `apx-agent scaffold <name>` invocation that doesn't work.
        runner = CliRunner()
        scaffold_result = runner.invoke(main, [
            "agents", "scaffold", "my-agent",
            "--template", "base",
            "--no-interactive",
            "--dir", str(tmp_path),
        ])
        assert scaffold_result.exit_code == 0, scaffold_result.output
        assert not (tmp_path / "my-agent" / ".apx" / "schema.json").exists()

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["agents", "refresh-schema"])
        assert result.exit_code != 0
        assert "--target" in result.output

    def test_full_project_scaffold_then_refresh_schema_chain(self, tmp_path, monkeypatch):
        # Issue #520: the real --target apps scaffold DOES chain into
        # refresh-schema, unlike the default YAML scaffold above.
        from apx_agent import cli

        manifest = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: manifest)
        runner = CliRunner()
        scaffold_result = runner.invoke(main, [
            "agents", "scaffold", "proj",
            "--target", "apps",
            "--catalog", "c", "--schema", "s",
            "--no-interactive",
            "--dir", str(tmp_path),
        ], catch_exceptions=False, env={"DATABRICKS_CONFIG_PROFILE": "__none__"})
        assert scaffold_result.exit_code == 0, scaffold_result.output
        target = tmp_path / "proj"
        assert (target / ".apx" / "schema.json").is_file()

        monkeypatch.chdir(target)
        refreshed_manifest = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)", "b(text)"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: refreshed_manifest)
        result = CliRunner().invoke(main, ["agents", "refresh-schema"])
        assert result.exit_code == 0, result.output


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

    def test_supervisor_add_app_registers_app_tool(self):
        """`supervisor add --app` publishes the Databricks App target (#444)."""
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={"tool_id": "t1"}) as mock_sup:
            result = CliRunner().invoke(main, [
                "supervisor", "add", "--app", "payroll-coworker",
                "--supervisor", "sup-456", "--description", "x",
            ])
        assert result.exit_code == 0, result.output
        call = mock_sup.call_args
        assert call.kwargs["app_name"] == "payroll-coworker"
        assert call.kwargs["serving_endpoint"] is None

    def test_supervisor_add_endpoint_and_app_mutually_exclusive(self):
        result = CliRunner().invoke(main, [
            "supervisor", "add", "--endpoint", "my-endpoint",
            "--app", "my-app", "--supervisor", "sup-456",
        ])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_supervisor_add_app_resolves_uc_name_via_tag(self):
        """--app with a UC model name resolves apx.apps.app_name like delete/status."""
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        mock_ws.registered_models.get.return_value = SimpleNamespace(
            tags=[SimpleNamespace(key="apx.apps.app_name", value="payroll-coworker")]
        )
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={}) as mock_sup:
            result = CliRunner().invoke(main, [
                "supervisor", "add", "--app", "main.agents.payroll",
                "--supervisor", "sup-456", "--description", "x",
            ])
        assert result.exit_code == 0, result.output
        mock_ws.registered_models.get.assert_called_once_with("main.agents.payroll")
        assert mock_sup.call_args.kwargs["app_name"] == "payroll-coworker"

    def test_supervisor_add_app_uc_name_without_tag_fails_loud(self):
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        mock_ws.registered_models.get.return_value = SimpleNamespace(tags=[])
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor") as mock_sup:
            result = CliRunner().invoke(main, [
                "supervisor", "add", "--app", "main.agents.payroll",
                "--supervisor", "sup-456", "--description", "x",
            ])
        assert result.exit_code != 0
        assert "apx.apps.app_name" in result.output
        mock_sup.assert_not_called()

    def test_deprecated_publish_apps_type_registers_app_tool(self):
        """`agents publish` (default --endpoint-type apps) chains the app path,
        not a serving-endpoint tool pointing at an app name (#444)."""
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={}) as mock_sup, \
             patch("apx_agent.publish_to_registry"), \
             patch("apx_agent._publish.publish_tools_to_registry", return_value=0):
            result = CliRunner().invoke(main, [
                "agents", "publish", "--endpoint", "my-agent-app",
                "--description", "x", "--supervisor", "sup-1",
            ])
        assert result.exit_code == 0, result.output
        call = mock_sup.call_args
        assert call.kwargs["app_name"] == "my-agent-app"
        assert call.kwargs["serving_endpoint"] is None

    def test_deprecated_publish_model_serving_type_keeps_endpoint_tool(self):
        mock_ws = MagicMock()
        mock_ws.config.host = "https://my-workspace.databricks.com"
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws), \
             patch("databricks.sdk.config.Config"), \
             patch("apx_agent.publish_to_supervisor", return_value={}) as mock_sup, \
             patch("apx_agent.publish_to_registry"), \
             patch("apx_agent._publish.publish_tools_to_registry", return_value=0):
            result = CliRunner().invoke(main, [
                "agents", "publish", "--endpoint", "my-endpoint",
                "--endpoint-type", "model-serving",
                "--description", "x", "--supervisor", "sup-1",
            ])
        assert result.exit_code == 0, result.output
        call = mock_sup.call_args
        assert call.kwargs["serving_endpoint"] == "my-endpoint"
        assert call.kwargs["app_name"] is None

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

    def test_json_output_reports_deleted_and_ok(self):
        # Issue #531: --json emits a single structured result instead of
        # progress text, so automation can tell exactly what succeeded.
        fake_ws = MagicMock()
        fake_model = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.name", value="my-agent"),
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
        ])
        fake_ws.registered_models.get.return_value = fake_model

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes", "--json",
            ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert "endpoint:my-ep" in payload["deleted"]
        assert "uc_model:main.schema.my_model" in payload["deleted"]
        assert payload["errors"] == []

    def test_json_output_reports_failure_and_nonzero_exit(self):
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[])
        fake_ws.registered_models.delete.side_effect = RuntimeError("locked")

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes", "--json",
            ])

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert any("locked" in e for e in payload["errors"])

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

    def test_app_auto_resolved_from_uc_tag(self):
        fake_ws = MagicMock()
        # Apps-target agent: manifest carries the apx.apps.app_name tag
        fake_model = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
            SimpleNamespace(key="apx.apps.app_name", value="tag-app"),
        ])
        fake_ws.registered_models.get.return_value = fake_model

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        # App delete must have been called using the UC-tag-resolved name
        fake_ws.apps.delete.assert_called_once_with("tag-app")
        assert "tag-app" in result.output

    def test_explicit_app_overrides_uc_tag(self):
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.apps.app_name", value="tag-app"),
        ])

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.m",
                "--app", "explicit-app",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        fake_ws.apps.delete.assert_called_once_with("explicit-app")

    def test_no_app_tag_skips_app_deletion(self):
        fake_ws = MagicMock()
        # Pure model-serving agent: no apx.apps.app_name tag
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
        ])

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.m",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        fake_ws.apps.delete.assert_not_called()

    def test_resolved_app_shown_in_confirmation_summary(self):
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.apps.app_name", value="tag-app"),
        ])

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            # Answer "n" at the prompt — we only want the summary, not the delete.
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.m",
            ], input="n\n")

        assert "Databricks App: tag-app" in result.output
        fake_ws.apps.delete.assert_not_called()

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

    # --- --purge (issue #409) -------------------------------------------

    @staticmethod
    def _purgeable_ws():
        """A workspace where every --purge leftover is discoverable."""
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
            SimpleNamespace(key="apx.apps.app_name", value="my-app"),
            SimpleNamespace(key="apx.apps.bundle_target", value="prod"),
        ])
        fake_ws.current_user.me.return_value = SimpleNamespace(user_name="me@x.com")
        fake_ws.experiments.get_by_name.return_value = SimpleNamespace(
            experiment=SimpleNamespace(experiment_id="exp-123"),
        )
        fake_ws.apps.list.return_value = [
            SimpleNamespace(name="my-app-canary-v1"),
            SimpleNamespace(name="unrelated-app"),
        ]
        return fake_ws

    def test_purge_deletes_experiment_canary_and_bundle_files(self):
        fake_ws = self._purgeable_ws()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge", "--yes",
            ])

        assert result.exit_code == 0, result.output
        # Experiment resolved by the deploy --auto-experiment path convention.
        fake_ws.experiments.get_by_name.assert_called_once_with(
            "/Users/me@x.com/my-app-prod"
        )
        fake_ws.experiments.delete_experiment.assert_called_once_with("exp-123")
        # Prod app AND the soaking canary go; unrelated apps stay.
        deleted_apps = [c.args[0] for c in fake_ws.apps.delete.call_args_list]
        assert deleted_apps == ["my-app", "my-app-canary-v1"]
        # Bundle deploy root removed recursively after an existence check.
        fake_ws.workspace.get_status.assert_called_once_with(
            "/Users/me@x.com/.bundle/my-app/prod"
        )
        fake_ws.workspace.delete.assert_called_once_with(
            "/Users/me@x.com/.bundle/my-app/prod", recursive=True,
        )
        # Lakebase carve-out: tables are named but never dropped.
        assert "apx_conversations" in result.output
        assert "manual" in result.output

    def test_purge_uses_explicit_experiment_tag_when_recorded(self):
        fake_ws = self._purgeable_ws()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.apps.app_name", value="my-app"),
            SimpleNamespace(key="apx.mlflow.experiment_id", value="exp-77"),
        ])

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge", "--yes",
            ])

        assert result.exit_code == 0, result.output
        fake_ws.experiments.delete_experiment.assert_called_once_with("exp-77")
        fake_ws.experiments.get_by_name.assert_not_called()

    def test_purge_unresolvable_app_prints_not_removed_and_skips(self):
        fake_ws = MagicMock()
        # No apx.apps.app_name tag → canary + bundle path underivable.
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
        ])
        fake_ws.current_user.me.return_value = SimpleNamespace(user_name="me@x.com")

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge", "--yes",
            ])

        assert result.exit_code == 0, result.output
        assert "not removed" in result.output
        fake_ws.workspace.delete.assert_not_called()
        fake_ws.experiments.delete_experiment.assert_not_called()
        fake_ws.apps.list.assert_not_called()
        fake_ws.apps.delete.assert_not_called()

    def test_purge_missing_bundle_path_prints_not_removed(self):
        fake_ws = self._purgeable_ws()
        fake_ws.workspace.get_status.side_effect = RuntimeError("RESOURCE_DOES_NOT_EXIST")

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge", "--yes",
            ])

        assert result.exit_code == 0, result.output
        assert "not removed: bundle workspace files" in result.output
        fake_ws.workspace.delete.assert_not_called()

    # --- advertise-registry cleanup + dependents warning (#446) ----------

    @staticmethod
    def _registry_ws():
        """A workspace where the advertise-registry tables exist."""
        fake_ws = MagicMock()
        fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
            SimpleNamespace(key="apx.agent.model", value="my-ep"),
            SimpleNamespace(key="apx.apps.app_name", value="my-app"),
        ])
        fake_ws.tables.exists.return_value = SimpleNamespace(table_exists=True)
        return fake_ws

    def test_delete_removes_registry_rows(self):
        fake_ws = self._registry_ws()
        calls: list[str] = []

        def _fake_run_sql(_ws, sql, warehouse_id=None, **_kw):
            calls.append(sql)
            return []

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
                patch("apx_agent._sql.run_sql", side_effect=_fake_run_sql):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        deletes = [s for s in calls if "DELETE FROM" in s]
        # Both advertise names (endpoint + app) are deregistered from both tables.
        for agent_id in ("my_ep", "my_app"):
            assert any(
                "main.apx.agent_registry" in s and f"'{agent_id}'" in s for s in deletes
            ), (agent_id, deletes)
            assert any(
                "main.apx.agent_tools" in s and f"'{agent_id}'" in s for s in deletes
            ), (agent_id, deletes)
        assert "Removed advertise-registry rows" in result.output
        # No dependents → the warning stays quiet.
        assert "reference this one" not in result.output

    def test_delete_skips_registry_with_notice_when_tables_missing(self):
        fake_ws = self._registry_ws()
        fake_ws.tables.exists.return_value = SimpleNamespace(table_exists=False)

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
                patch("apx_agent._sql.run_sql") as run_sql:
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        assert "Notice: registry cleanup skipped" in result.output
        run_sql.assert_not_called()

    def test_dependents_warning_lists_agents_and_row_age(self):
        import time as _time

        fake_ws = self._registry_ws()
        calls: list[str] = []

        def _fake_run_sql(_ws, sql, warehouse_id=None, **_kw):
            calls.append(sql)
            if "SELECT" in sql and "agent_tools" in sql:
                return [{
                    "agent_name": "billing-bot",
                    "name": "ask_victim",
                    "sub_agent_url": "https://h/apps/my-app",
                    "updated_at": str(_time.time() - 42 * 86400),
                }]
            return []

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
                patch("apx_agent._sql.run_sql", side_effect=_fake_run_sql):
            # Abort at the prompt — the warning must render BEFORE deletion.
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
            ], input="n\n")

        assert "1 other agent(s) reference this one" in result.output
        assert "billing-bot" in result.output
        assert "https://h/apps/my-app" in result.output
        assert "last advertised 42d ago" in result.output
        # Aborted → nothing deleted, in the workspace or the registry.
        assert not any("DELETE FROM" in s for s in calls)
        fake_ws.serving_endpoints.delete.assert_not_called()

    def test_dependents_scan_failure_is_notice_not_crash(self):
        fake_ws = self._registry_ws()

        def _fake_run_sql(_ws, sql, warehouse_id=None, **_kw):
            if "SELECT" in sql:
                raise RuntimeError("no SELECT grant on registry")
            return []

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
                patch("apx_agent._sql.run_sql", side_effect=_fake_run_sql):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        # Scan failure never blocks the delete; the cleanup still ran fine.
        assert result.exit_code == 0, result.output
        assert "could not scan the registry for dependents" in result.output
        assert "Removed advertise-registry rows" in result.output

    def test_registry_cleanup_failure_aggregates_to_nonzero(self):
        fake_ws = self._registry_ws()

        def _fake_run_sql(_ws, sql, warehouse_id=None, **_kw):
            if "DELETE FROM" in sql:
                raise RuntimeError("PERMISSION_DENIED")
            return []

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())), \
                patch("apx_agent._sql.run_sql", side_effect=_fake_run_sql):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code != 0
        assert "could not remove registry rows" in result.output

    def test_delete_help_mentions_registry_cleanup(self):
        result = CliRunner().invoke(main, ["agents", "delete", "--help"])
        assert result.exit_code == 0
        assert "advertise-registry rows" in result.output

    def test_without_purge_no_extra_deletions(self):
        fake_ws = self._purgeable_ws()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--yes",
            ])

        assert result.exit_code == 0, result.output
        fake_ws.apps.delete.assert_called_once_with("my-app")
        fake_ws.experiments.delete_experiment.assert_not_called()
        fake_ws.workspace.delete.assert_not_called()
        fake_ws.apps.list.assert_not_called()
        fake_ws.current_user.me.assert_not_called()

    def test_purge_confirmation_lists_purge_set(self):
        fake_ws = self._purgeable_ws()

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            # Answer "n" — we want the summary, not the deletions.
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge",
            ], input="n\n")

        assert "/Users/me@x.com/my-app-prod" in result.output
        assert "Canary App: my-app-canary-v1" in result.output
        assert "/Users/me@x.com/.bundle/my-app/prod" in result.output
        assert "apx_conversations" in result.output
        fake_ws.apps.delete.assert_not_called()
        fake_ws.experiments.delete_experiment.assert_not_called()
        fake_ws.workspace.delete.assert_not_called()

    def test_purge_failures_aggregate_to_nonzero_exit(self):
        fake_ws = self._purgeable_ws()
        fake_ws.experiments.delete_experiment.side_effect = RuntimeError("boom")

        with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, MagicMock())):
            result = CliRunner().invoke(main, [
                "agents", "delete",
                "--uc-name", "main.schema.my_model",
                "--purge", "--yes",
            ])

        assert result.exit_code != 0
        assert "failed" in result.output.lower()
        # Best-effort: the other purge deletions still ran.
        fake_ws.workspace.delete.assert_called_once()
        deleted_apps = [c.args[0] for c in fake_ws.apps.delete.call_args_list]
        assert "my-app-canary-v1" in deleted_apps


# ---------------------------------------------------------------------------
# `apx-agent uc validate`
# ---------------------------------------------------------------------------


class TestWsListHelpersSurfaceErrors:
    # Issue #523: these helpers used to swallow every exception with a bare
    # `except Exception: return []`/`None`, so an expired token or a network
    # error looked identical to "genuinely empty" — a user would debug the
    # wrong problem. They now print a warning to stderr before falling back.

    def test_ws_list_catalogs_warns_on_exception(self, capsys):
        from apx_agent import cli

        ws = MagicMock()
        ws.catalogs.list.side_effect = RuntimeError("token expired")

        result = cli._ws_list_catalogs(ws)

        assert result == []
        assert "token expired" in capsys.readouterr().err

    def test_ws_list_schemas_warns_on_exception(self, capsys):
        from apx_agent import cli

        ws = MagicMock()
        ws.schemas.list.side_effect = RuntimeError("permission denied")

        result = cli._ws_list_schemas(ws, "mycat")

        assert result == []
        captured = capsys.readouterr().err
        assert "permission denied" in captured
        assert "mycat" in captured

    def test_ws_list_tables_warns_on_exception(self, capsys):
        from apx_agent import cli

        ws = MagicMock()
        ws.tables.list.side_effect = RuntimeError("network down")

        result = cli._ws_list_tables(ws, "c", "s")

        assert result == []
        captured = capsys.readouterr().err
        assert "network down" in captured
        assert "c.s" in captured

    def test_ws_list_functions_warns_on_exception(self, capsys):
        from apx_agent import cli

        ws = MagicMock()
        ws.functions.list.side_effect = RuntimeError("network down")

        result = cli._ws_list_functions(ws, "c", "s")

        assert result == []
        captured = capsys.readouterr().err
        assert "network down" in captured
        assert "c.s" in captured

    def test_make_ws_for_scaffold_warns_on_exception(self, monkeypatch, capsys):
        from apx_agent import cli

        def boom(*a, **k):
            raise RuntimeError("no default auth")

        monkeypatch.setattr("databricks.sdk.WorkspaceClient", boom)

        result = cli._make_ws_for_scaffold(None)

        assert result is None
        assert "no default auth" in capsys.readouterr().err


class TestUcProfileFlagPosition:
    # Issue #529: --profile used to only work *before* the subcommand for
    # catalogs/schemas/tables/tools/validate (group-level ctx.obj), while
    # publish/topology required it *after* (their own leaf-level option) and
    # silently ignored the group-level form. All uc subcommands now take a
    # leaf-level --profile, matching the rest of the CLI.

    def test_validate_leaf_level_profile_is_forwarded(self):
        fake_ws = MagicMock()
        with patch("apx_agent.cli._require_sdk", return_value=fake_ws) as require_sdk, \
             patch("apx_agent.cli._ws_list_catalogs", return_value=["mycat"]), \
             patch("apx_agent.cli._ws_list_schemas", return_value=["myschema"]), \
             patch("apx_agent.cli._ws_list_tables", return_value=[SimpleNamespace(name="t1")]), \
             patch.object(fake_ws.registered_models, "list", return_value=iter([])):
            result = CliRunner().invoke(main, [
                "uc", "validate", "--catalog", "mycat", "--schema", "myschema",
                "--profile", "my-profile",
            ])

        assert result.exit_code == 0, result.output
        require_sdk.assert_called_once_with("my-profile")

    def test_catalogs_leaf_level_profile_is_forwarded(self):
        with patch("apx_agent.cli._require_sdk", return_value=MagicMock()) as require_sdk, \
             patch("apx_agent.cli._ws_list_catalogs", return_value=[]):
            result = CliRunner().invoke(main, [
                "uc", "catalogs", "--profile", "my-profile",
            ])

        assert result.exit_code == 0, result.output
        require_sdk.assert_called_once_with("my-profile")

    def test_uc_group_has_no_group_level_profile_option(self):
        # A group-level --profile before the subcommand must no longer be
        # accepted at all (it used to silently work for some subcommands
        # and silently no-op for others -- now it's just an error).
        result = CliRunner().invoke(main, [
            "uc", "--profile", "my-profile", "catalogs",
        ])
        assert result.exit_code != 0

    def test_topology_leaf_level_profile_is_forwarded(self):
        # `topology` was one of the two subcommands (#529) that declared its
        # own leaf-level --profile shadowing the removed group option. Pin
        # that the leaf --profile actually reaches _connect_workspace.
        fake_topo = SimpleNamespace(nodes=["a"], edges=[])
        with patch("apx_agent.cli._connect_workspace",
                   return_value=(MagicMock(), MagicMock())) as connect, \
             patch("apx_agent.discover_topology", return_value=fake_topo), \
             patch("apx_agent.render_topology", return_value="graph LR"):
            result = CliRunner().invoke(main, [
                "uc", "topology", "--profile", "my-profile",
            ])

        assert result.exit_code == 0, result.output
        connect.assert_called_once_with("my-profile")

    def test_publish_leaf_level_profile_is_forwarded(self):
        # `publish` was the other #529 subcommand. It resolves the profile
        # into DATABRICKS_CONFIG_PROFILE before doing any UC work; pin that
        # the leaf --profile wins over the ambient environment.
        with patch("apx_agent.cli._read_apx_agent_config", return_value={}), \
             patch("apx_agent.cli._load_finalized_agent", return_value=MagicMock()), \
             patch("apx_agent.publish_tools_to_uc", return_value=[]), \
             patch.dict(os.environ, {"DATABRICKS_CONFIG_PROFILE": "ambient"}, clear=False):
            result = CliRunner().invoke(main, [
                "uc", "publish", "--profile", "my-profile",
            ])
            assert result.exit_code == 0, result.output
            assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "my-profile"


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
# apx-agent onboard — response parsing + spec validation (#318)
# ---------------------------------------------------------------------------


class TestSplitOnboardingResponse:
    def test_extracts_both_blocks(self) -> None:
        from apx_agent.cli import _split_onboarding_response

        text = (
            "```markdown\n# Plan\n\nDo the thing.\n```\n\n"
            "```toml\n"
            'catalog = "TBD-catalog"\n'
            "```\n"
        )
        result = _split_onboarding_response(text)
        assert result.plan_markdown == "# Plan\n\nDo the thing."
        assert result.spec_toml == 'catalog = "TBD-catalog"'

    def test_missing_blocks_return_none(self) -> None:
        from apx_agent.cli import _split_onboarding_response

        result = _split_onboarding_response("no fenced blocks here")
        assert result.plan_markdown is None
        assert result.spec_toml is None


class TestValidateSpecToml:
    def test_valid_toml_returns_none(self) -> None:
        from apx_agent.cli import _validate_spec_toml

        toml_text = (
            'catalog = "TBD-catalog"\n'
            'schema = "TBD-schema"\n'
            'persona = "a program director"\n'
            'objective = "surface at-risk loans"\n'
            'memory = "persistent"\n'
        )
        assert _validate_spec_toml(toml_text) is None

    def test_malformed_toml_returns_error(self) -> None:
        from apx_agent.cli import _validate_spec_toml

        error = _validate_spec_toml("catalog = not valid toml @@@")
        assert error is not None
        assert "TOML parse error" in error

    def test_missing_required_field_returns_error(self) -> None:
        from apx_agent.cli import _validate_spec_toml

        # catalog and schema are required (no default) on CoworkerTemplate.Spec.
        error = _validate_spec_toml('persona = "someone"\n')
        assert error is not None
        assert "Spec validation error" in error


def _canned_onboarding_answers() -> "Any":
    return iter([
        "Example Org",
        "We provide microloans to small farmers.",
        "Kiva, spreadsheets",
        "Manually reconciling loan repayments every week",
        "Which loans are at risk of default",
        "about 2,000 active loans",
        "Program Director",
    ])


_VALID_ONBOARDING_RESPONSE = (
    "```markdown\n"
    "# Onboarding Plan\n\n"
    "## Phase 1: Land the data\n"
    "Bring Kiva exports into Unity Catalog. 1-2 weeks.\n\n"
    "## Phase 2: Stand up the coworker\n"
    "Ground a CoworkerAgent in the landed schema. 1 week.\n"
    "```\n\n"
    "```toml\n"
    'catalog = "TBD-catalog"\n'
    'schema = "TBD-schema"\n'
    'persona = "a microfinance program director"\n'
    'join_key = "loan_id"\n'
    'objective = "surface loans at risk of default before they lapse"\n'
    'memory = "persistent"\n'
    "```\n"
)


class TestGenerateOnboardingPlan:
    def test_valid_on_first_response(self) -> None:
        from apx_agent.cli import _generate_onboarding_plan

        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_VALID_ONBOARDING_RESPONSE))]
        )
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _generate_onboarding_plan(None)

        assert result.org_name == "Example Org"
        assert "Phase 1: Land the data" in result.plan_markdown
        assert result.validation_error is None
        assert 'catalog = "TBD-catalog"' in result.spec_toml
        assert fake_ws.serving_endpoints.query.call_count == 1

    def test_passes_chatmessage_not_dicts(self) -> None:
        """The SDK calls .as_dict() on each message, so dicts AttributeError at runtime."""
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        from apx_agent.cli import _generate_onboarding_plan

        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_VALID_ONBOARDING_RESPONSE))]
        )
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            _generate_onboarding_plan(None)

        msgs = fake_ws.serving_endpoints.query.call_args.kwargs["messages"]
        assert msgs and all(isinstance(m, ChatMessage) for m in msgs)
        assert msgs[0].role == ChatMessageRole.USER

    def test_retries_on_invalid_toml(self) -> None:
        from apx_agent.cli import _generate_onboarding_plan

        bad_response = (
            "```markdown\n# Plan\nPhase 1: land data.\n```\n\n"
            "```toml\ncatalog = not valid @@@\n```\n"
        )
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=bad_response))]),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_VALID_ONBOARDING_RESPONSE))]
            ),
        ]
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _generate_onboarding_plan(None)

        assert result.validation_error is None
        assert fake_ws.serving_endpoints.query.call_count == 2

    def test_falls_back_when_retry_also_invalid(self) -> None:
        from apx_agent.cli import _generate_onboarding_plan

        bad_response = (
            "```markdown\n# Plan\nPhase 1: land data.\n```\n\n"
            "```toml\ncatalog = still not valid @@@\n```\n"
        )
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=bad_response))]
        )
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _generate_onboarding_plan(None)

        assert result.validation_error is not None
        assert "Plan" in result.plan_markdown  # plan still captured though spec failed
        assert fake_ws.serving_endpoints.query.call_count == 2

    def test_missing_toml_block_is_treated_as_invalid(self) -> None:
        from apx_agent.cli import _generate_onboarding_plan

        no_toml_response = "```markdown\n# Plan\nPhase 1: land data.\n```\n"
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=no_toml_response))]
        )
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _generate_onboarding_plan(None)

        assert result.validation_error is not None
        assert "no ```toml block" in result.validation_error
        assert fake_ws.serving_endpoints.query.call_count == 2  # one retry attempted

    def test_missing_markdown_block_is_treated_as_invalid(self) -> None:
        from apx_agent.cli import _generate_onboarding_plan

        no_markdown_response = (
            "```toml\n"
            'catalog = "TBD-catalog"\n'
            'schema = "TBD-schema"\n'
            'persona = "a program director"\n'
            'objective = "surface at-risk loans"\n'
            'memory = "persistent"\n'
            "```\n"
        )
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=no_markdown_response))]
        )
        answers = _canned_onboarding_answers()
        with patch(
            "apx_agent.cli.click.prompt", side_effect=lambda *a, **k: next(answers)
        ), patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _generate_onboarding_plan(None)

        assert result.validation_error is not None
        assert "no ```markdown block" in result.validation_error
        assert fake_ws.serving_endpoints.query.call_count == 2  # one retry attempted


class TestClassifyAgentDescription:
    def test_classify_agent_description_parses_coworker_shape(self) -> None:
        from apx_agent.cli import _classify_agent_description

        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({
                "template": "coworker",
                "name": "unbilled-revenue-agent",
                "persona": "a revenue operations analyst",
                "objective": "flag unbilled revenue",
                "join_key": "account_id",
                "catalog_hint": None,
                "schema_hint": None,
                "missing": [],
            })
        ))])
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = fake_response

        with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _classify_agent_description(
                None, "an agent that joins Salesforce closed deals with NetSuite "
                "payments by account_id and flags unbilled revenue"
            )

        assert result.template == "coworker"
        assert result.name == "unbilled-revenue-agent"
        assert result.join_key == "account_id"
        assert result.missing == ()

    def test_classify_agent_description_retries_on_invalid_json(self) -> None:
        from apx_agent.cli import _classify_agent_description

        bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))])
        good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({
                "template": "base", "name": "helper-agent", "persona": None,
                "objective": None, "join_key": None, "catalog_hint": None,
                "schema_hint": None, "missing": [],
            })
        ))])
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.side_effect = [bad, good]

        with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _classify_agent_description(None, "a simple helper agent")

        assert result.template == "base"
        assert fake_ws.serving_endpoints.query.call_count == 2

    def test_classify_agent_description_raises_after_second_failure(self) -> None:
        import click
        from apx_agent.cli import _classify_agent_description

        bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="still not json"))])
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.return_value = bad

        with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            with pytest.raises(click.ClickException):
                _classify_agent_description(None, "a simple helper agent")

    def test_classify_agent_description_retries_on_out_of_enum_template(self) -> None:
        # Valid JSON but an out-of-enum "template" value must not reach the
        # _GEN_AUTHOR_PROMPTS[classification.template] lookup in
        # _author_agent_yaml as an uncaught KeyError -- it should be treated
        # like any other malformed classification and trigger the retry.
        from apx_agent.cli import _classify_agent_description

        bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({
                "template": "analytics", "name": "x", "persona": None,
                "objective": None, "join_key": None, "catalog_hint": None,
                "schema_hint": None, "missing": [],
            })
        ))])
        good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({
                "template": "base", "name": "helper-agent", "persona": None,
                "objective": None, "join_key": None, "catalog_hint": None,
                "schema_hint": None, "missing": [],
            })
        ))])
        fake_ws = MagicMock()
        fake_ws.serving_endpoints.query.side_effect = [bad, good]

        with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
            result = _classify_agent_description(None, "a simple helper agent")

        assert result.template == "base"
        assert fake_ws.serving_endpoints.query.call_count == 2


class TestOnboardCommand:
    def _mock_generate(self, monkeypatch: "pytest.MonkeyPatch", result: "Any") -> None:
        import apx_agent.cli as cli_mod

        monkeypatch.setattr(cli_mod, "_generate_onboarding_plan", lambda profile: result)

    def test_writes_plan_and_valid_spec(self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        from apx_agent.cli import _OnboardingPlan, main

        result = _OnboardingPlan(
            org_name="Example Org",
            plan_markdown="# Plan\nDo the thing.",
            spec_toml='catalog = "TBD-catalog"\n',
            validation_error=None,
        )
        self._mock_generate(monkeypatch, result)

        runner = CliRunner()
        cli_result = runner.invoke(main, ["onboard", "--dir", str(tmp_path)])

        assert cli_result.exit_code == 0, cli_result.output
        plan_path = tmp_path / "example_org-onboarding-plan.md"
        spec_path = tmp_path / "example_org-coworker.toml"
        assert plan_path.read_text() == "# Plan\nDo the thing."
        assert spec_path.read_text() == 'catalog = "TBD-catalog"\n'
        assert "Next step: apx-agent agents scaffold" in cli_result.output

    def test_writes_draft_toml_when_invalid(self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        from apx_agent.cli import _OnboardingPlan, main

        result = _OnboardingPlan(
            org_name="Example Org",
            plan_markdown="# Plan",
            spec_toml="catalog = not valid",
            validation_error="TOML parse error: bad syntax",
        )
        self._mock_generate(monkeypatch, result)

        runner = CliRunner()
        cli_result = runner.invoke(main, ["onboard", "--dir", str(tmp_path)])

        assert cli_result.exit_code == 0, cli_result.output
        draft_path = tmp_path / "example_org-coworker.DRAFT.toml"
        assert draft_path.exists()
        assert "UNVALIDATED" in draft_path.read_text()
        assert not (tmp_path / "example_org-coworker.toml").exists()
        assert "UNVALIDATED" in cli_result.output

    def test_refuses_to_overwrite_without_force(self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        from apx_agent.cli import _OnboardingPlan, main

        result = _OnboardingPlan(
            org_name="Example Org", plan_markdown="# Plan",
            spec_toml='catalog = "TBD-catalog"\n', validation_error=None,
        )
        self._mock_generate(monkeypatch, result)
        (tmp_path / "example_org-onboarding-plan.md").write_text("existing")

        runner = CliRunner()
        cli_result = runner.invoke(main, ["onboard", "--dir", str(tmp_path)])

        assert cli_result.exit_code != 0
        assert "already exists" in cli_result.output

    def test_force_overwrites_existing(self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        from apx_agent.cli import _OnboardingPlan, main

        result = _OnboardingPlan(
            org_name="Example Org", plan_markdown="# New plan",
            spec_toml='catalog = "TBD-catalog"\n', validation_error=None,
        )
        self._mock_generate(monkeypatch, result)
        (tmp_path / "example_org-onboarding-plan.md").write_text("stale")

        runner = CliRunner()
        cli_result = runner.invoke(main, ["onboard", "--dir", str(tmp_path), "--force"])

        assert cli_result.exit_code == 0, cli_result.output
        assert (tmp_path / "example_org-onboarding-plan.md").read_text() == "# New plan"

    def test_raises_on_empty_plan_markdown(self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        from apx_agent.cli import _OnboardingPlan, main

        result = _OnboardingPlan(
            org_name="Example Org",
            plan_markdown="   ",
            spec_toml='catalog = "TBD-catalog"\n',
            validation_error=None,
        )
        self._mock_generate(monkeypatch, result)

        runner = CliRunner()
        cli_result = runner.invoke(main, ["onboard", "--dir", str(tmp_path)])

        assert cli_result.exit_code != 0
        assert not (tmp_path / "example_org-onboarding-plan.md").exists()


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
        assert "No .apx" in result.output or "no .apx" in result.output.lower()

    def test_pull_uc_read_failure_errors_cleanly(self, tmp_path, monkeypatch):
        # ws.tables.list raises (permission/network) -> the command must fail
        # with a clean ClickException naming the cause, not crash on the
        # traceback.
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

        def boom(catalog_name, schema_name):
            raise RuntimeError("PERMISSION_DENIED")

        fake_ws = SimpleNamespace(tables=SimpleNamespace(list=boom))
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda *a, **k: fake_ws)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["pull-comments"])
        assert result.exit_code != 0
        assert "Could not read comments" in result.output


# ---------------------------------------------------------------------------
# Model-serving deploy: provenance version tags (issue #403)
# ---------------------------------------------------------------------------


_PROVENANCE = {
    "apx.apps.git_sha": "b" * 40,
    "apx.git_dirty": "false",
    "apx.lock_sha256": "c" * 64,
}


def _invoke_provenance_deploy(monkeypatch, tmp_path, fake_client):
    _write_agent_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apx_agent.cli._provenance_version_tags", lambda cwd: dict(_PROVENANCE),
    )
    fake_log_agent = MagicMock(
        return_value=SimpleNamespace(registered_model_version="7")
    )
    with patch("apx_agent.log_agent", fake_log_agent), \
         patch("mlflow.start_run"), \
         patch("mlflow.tracking.MlflowClient", return_value=fake_client):
        result = CliRunner().invoke(main, [
            "agents", "deploy",
            "--target", "model-serving",
            "--module", "tmp_test_agent:agent",
            "--model", "databricks-claude-sonnet-4-6",
            "--name", "main.agents.x",
            "--no-deploy", "--no-publish-tools", "--no-set-uc-tags",
        ])
    sys.modules.pop("tmp_test_agent", None)
    return result


def test_deploy_model_serving_stamps_provenance_on_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After log+register, the registered model VERSION carries the same
    provenance tags the Apps manifest gets (issue #403)."""
    fake_client = MagicMock()
    result = _invoke_provenance_deploy(monkeypatch, tmp_path, fake_client)
    assert result.exit_code == 0, result.output
    written = {
        call.args[2]: call.args[3]
        for call in fake_client.set_model_version_tag.call_args_list
    }
    assert written == _PROVENANCE
    # Every write targeted the registered name + version just logged.
    assert all(
        call.args[:2] == ("main.agents.x", "7")
        for call in fake_client.set_model_version_tag.call_args_list
    )
    assert "provenance: 3 tags written on version 7" in result.output


def test_deploy_model_serving_provenance_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provenance tag-write failure warns but never reddens the deploy."""
    fake_client = MagicMock()
    fake_client.set_model_version_tag.side_effect = RuntimeError("UC down")
    result = _invoke_provenance_deploy(monkeypatch, tmp_path, fake_client)
    assert result.exit_code == 0, result.output
    assert "provenance tags failed (non-fatal): UC down" in result.output


# ---------------------------------------------------------------------------
# `apx agents status` — post-deploy health + provenance (issue #410)
# ---------------------------------------------------------------------------

_STATUS_HEAD = "a" * 40
_STATUS_OTHER = "0" * 40
_STATUS_UC = "main.agents.x"


class _StatusInvocation(NamedTuple):
    result: Result
    readyz_mock: MagicMock
    ws: MagicMock


def _status_version(version: str, tags: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(version=version, tags=tags)


def _apps_version(
    *, sha: str = _STATUS_HEAD, dirty: str = "false",
) -> SimpleNamespace:
    return _status_version("7", {
        "apx.serving": "apps",
        "apx.apps.app_name": "my-app",
        "apx.apps.git_sha": sha,
        "apx.git_dirty": dirty,
    })


def _running_app() -> SimpleNamespace:
    return SimpleNamespace(
        app_status=SimpleNamespace(state=SimpleNamespace(value="RUNNING")),
        url="https://my-app.example.com",
    )


def _invoke_status(
    args: list[str],
    *,
    versions: list[SimpleNamespace] | None = None,
    search_error: Exception | None = None,
    app: SimpleNamespace | None = None,
    app_error: Exception | None = None,
    endpoint: SimpleNamespace | None = None,
    rm_tags: dict[str, str] | None = None,
    readyz: _ReadyzResult | None = None,
    head_sha: str | None = _STATUS_HEAD,
    sub_probes: list[SubAgentProbe] | None = None,
) -> _StatusInvocation:
    """Run `agents status` with the workspace + UC surface fully mocked."""
    fake_ws = MagicMock()
    if app_error is not None:
        fake_ws.apps.get.side_effect = app_error
    elif app is not None:
        fake_ws.apps.get.return_value = app
    if endpoint is not None:
        fake_ws.serving_endpoints.get.return_value = endpoint
    fake_ws.registered_models.get.return_value = SimpleNamespace(tags=[
        SimpleNamespace(key=k, value=v) for k, v in (rm_tags or {}).items()
    ])

    client = MagicMock()
    if search_error is not None:
        client.search_model_versions.side_effect = search_error
    else:
        client.search_model_versions.return_value = list(versions or [])

    readyz_mock = MagicMock(
        return_value=readyz
        if readyz is not None
        else _ReadyzResult(is_ready=True, checks={"agent": "ok"}),
    )
    fake_cfg = SimpleNamespace(host="https://ws.example.com")
    with patch("apx_agent.cli._connect_workspace", return_value=(fake_ws, fake_cfg)), \
         patch("mlflow.tracking.MlflowClient", return_value=client), \
         patch("apx_agent.cli._check_readyz", readyz_mock), \
         patch("apx_agent.cli._git_head_sha", return_value=head_sha), \
         patch("apx_agent.cli._probe_local_sub_agents",
               return_value=list(sub_probes or [])):
        result = CliRunner().invoke(main, ["agents", "status", *args])
    return _StatusInvocation(result=result, readyz_mock=readyz_mock, ws=fake_ws)


def test_agents_status_apps_healthy_exits_zero_with_sha_and_drift() -> None:
    """RUNNING app + ready /readyz → exit 0, sha shown, drift line says match."""
    inv = _invoke_status(
        [_STATUS_UC], versions=[_apps_version()], app=_running_app(),
    )
    assert inv.result.exit_code == 0, inv.result.output
    assert "RUNNING" in inv.result.output
    assert _STATUS_HEAD[:12] in inv.result.output
    assert "matches local HEAD" in inv.result.output
    assert "HEALTHY" in inv.result.output and "yes" in inv.result.output
    # Snappy status probe: fewer attempts than the deploy gate's default 5.
    assert inv.readyz_mock.call_args.kwargs["attempts"] == 2


def test_agents_status_apps_drift_line_on_sha_mismatch() -> None:
    """Deployed sha != local HEAD → drift line names both commits, still exit 0."""
    inv = _invoke_status(
        [_STATUS_UC],
        versions=[_apps_version(sha=_STATUS_OTHER)],
        app=_running_app(),
    )
    assert inv.result.exit_code == 0, inv.result.output
    assert _STATUS_OTHER[:12] in inv.result.output
    assert _STATUS_HEAD[:12] in inv.result.output
    assert "re-deploy to ship HEAD" in inv.result.output


def test_agents_status_apps_readyz_failed_exits_one() -> None:
    """App RUNNING but /readyz degraded → unhealthy, exit 1."""
    inv = _invoke_status(
        [_STATUS_UC],
        versions=[_apps_version()],
        app=_running_app(),
        readyz=_ReadyzResult(is_ready=False, checks={"agent": "llm timeout"}),
    )
    assert inv.result.exit_code == 1, inv.result.output
    assert "failed" in inv.result.output
    assert "llm timeout" in inv.result.output
    assert "no" in inv.result.output


def test_agents_status_serving_ready_exits_zero() -> None:
    """Model-serving target: endpoint READY → healthy, exit 0."""
    inv = _invoke_status(
        [_STATUS_UC],
        versions=[_status_version("3", {"apx.apps.git_sha": _STATUS_HEAD})],
        rm_tags={"apx.agent.model": "my-endpoint"},
        endpoint=SimpleNamespace(
            state=SimpleNamespace(ready=SimpleNamespace(value="READY")),
        ),
    )
    assert inv.result.exit_code == 0, inv.result.output
    assert "model-serving" in inv.result.output
    assert "my-endpoint" in inv.result.output
    assert "READY" in inv.result.output
    inv.ws.serving_endpoints.get.assert_called_once_with("my-endpoint")
    # No smoke invocation in this iteration — status never posts traffic.
    inv.readyz_mock.assert_not_called()


def test_agents_status_serving_not_ready_exits_one() -> None:
    inv = _invoke_status(
        [_STATUS_UC],
        versions=[_status_version("3", {})],
        rm_tags={"apx.agent.model": "my-endpoint"},
        endpoint=SimpleNamespace(
            state=SimpleNamespace(ready=SimpleNamespace(value="NOT_READY")),
        ),
    )
    assert inv.result.exit_code == 1, inv.result.output
    assert "NOT_READY" in inv.result.output


def test_agents_status_nothing_deployed_is_clean_error() -> None:
    """No registered versions → actionable ClickException, no traceback."""
    inv = _invoke_status([_STATUS_UC], versions=[])
    assert inv.result.exit_code != 0
    assert "nothing deployed" in inv.result.output
    assert "Traceback" not in inv.result.output


def test_agents_status_no_name_outside_project_is_clean_error() -> None:
    """No NAME and no resolvable project → actionable error, not a crash."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["agents", "status"])
    assert result.exit_code != 0
    assert "No agent to check" in result.output
    assert "Traceback" not in result.output


def test_agents_status_resolves_uc_name_from_project(tmp_path: Path) -> None:
    """NAME omitted inside a project → registered_model resolves, same as doctor."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            '[tool.apx.agent]\nregistered_model = "main.agents.x"\n'
        )
        client = MagicMock()
        client.search_model_versions.return_value = []
        with patch("mlflow.tracking.MlflowClient", return_value=client):
            result = runner.invoke(main, ["agents", "status"])
    assert "main.agents.x" in result.output
    client.search_model_versions.assert_called_once_with("name='main.agents.x'")


def test_agents_status_json_emits_one_parseable_object() -> None:
    inv = _invoke_status(
        [_STATUS_UC, "--json"], versions=[_apps_version()], app=_running_app(),
    )
    assert inv.result.exit_code == 0, inv.result.output
    payload = json.loads(inv.result.output)
    assert payload["uc_name"] == _STATUS_UC
    assert payload["target"] == "apps"
    assert payload["name"] == "my-app"
    assert payload["state"] == "RUNNING"
    assert payload["url"] == "https://my-app.example.com"
    assert payload["version"] == "7"
    assert payload["git_sha"] == _STATUS_HEAD
    assert payload["git_dirty"] is False
    assert payload["drift"] == "match"
    assert payload["readyz"] is True
    assert payload["healthy"] is True


def test_agents_status_app_unreachable_is_clean_error() -> None:
    """Network error probing the app → 'could not reach', non-zero, no traceback."""
    inv = _invoke_status(
        [_STATUS_UC],
        versions=[_apps_version()],
        app_error=ConnectionError("network is down"),
    )
    assert inv.result.exit_code != 0
    assert "could not reach app 'my-app'" in inv.result.output
    assert "network is down" in inv.result.output
    assert "Traceback" not in inv.result.output


def test_agents_status_uc_unreachable_is_clean_error() -> None:
    inv = _invoke_status([_STATUS_UC], search_error=ConnectionError("offline"))
    assert inv.result.exit_code != 0
    assert "could not reach Unity Catalog" in inv.result.output
    assert "Traceback" not in inv.result.output


# `agents status` sub-agent reachability section (issue #445) ---------------

_STATUS_SUB_PROBES = [
    SubAgentProbe(
        url="https://orders.example.com", reachable=True, name="orders-agent",
    ),
    SubAgentProbe(
        url="$PEER_URL", reachable=False,
        error="env ref resolved to empty — variable unset",
    ),
]


def test_agents_status_sub_agents_section_reachable_and_unreachable() -> None:
    """Declared sub-agents get a section: card name when up, reason when not.

    Informational only — a down peer never flips healthy/exit code.
    """
    inv = _invoke_status(
        [_STATUS_UC], versions=[_apps_version()], app=_running_app(),
        sub_probes=_STATUS_SUB_PROBES,
    )
    assert inv.result.exit_code == 0, inv.result.output
    assert "Sub-agents (2):" in inv.result.output
    assert "https://orders.example.com  reachable (orders-agent)" in inv.result.output
    assert "$PEER_URL  unreachable (env ref resolved to empty" in inv.result.output


def test_agents_status_no_sub_agents_no_section() -> None:
    """No declared sub-agents → no section (skip cleanly, no noise)."""
    inv = _invoke_status(
        [_STATUS_UC], versions=[_apps_version()], app=_running_app(),
    )
    assert inv.result.exit_code == 0, inv.result.output
    assert "Sub-agents" not in inv.result.output


def test_agents_status_json_sub_agents_shape() -> None:
    """--json carries sub_agents as [{url, reachable, name?|error?}]."""
    inv = _invoke_status(
        [_STATUS_UC, "--json"], versions=[_apps_version()], app=_running_app(),
        sub_probes=_STATUS_SUB_PROBES,
    )
    assert inv.result.exit_code == 0, inv.result.output
    payload = json.loads(inv.result.output)
    assert payload["sub_agents"] == [
        {"url": "https://orders.example.com", "reachable": True, "name": "orders-agent"},
        {"url": "$PEER_URL", "reachable": False,
         "error": "env ref resolved to empty — variable unset"},
    ]
    assert payload["healthy"] is True


# ---------------------------------------------------------------------------
# _materialize_agent
# ---------------------------------------------------------------------------


def test_materialize_agent_writes_full_project(tmp_path: Path) -> None:
    from apx_agent._models import AgentConfig
    from apx_agent.cli import _materialize_agent

    config = AgentConfig(
        name="mat-agent",
        model="databricks-claude-sonnet-4-6",
        instructions="Answer questions.",
        template={"name": "base"},
    )
    target = tmp_path / "mat-agent"
    _materialize_agent(config, target, force=False)

    # generate_project()'s own output
    assert (target / "agent.py").exists()
    assert (target / "pyproject.toml").exists()
    assert (target / "databricks.yml").exists()
    assert (target / "app.yml").exists()
    assert (target / "agent_server" / "start_server.py").exists()
    # the auxiliary files generate_project() doesn't write but _scaffold_apps
    # does — _materialize_agent must add these so gallery-pick/generate don't
    # produce a thinner starter pack than plain scaffold.
    assert (target / "README.md").exists()
    assert (target / ".env.example").exists()
    assert (target / ".gitignore").exists()
    assert (target / "scripts" / "__init__.py").exists()
    assert (target / "scripts" / "quickstart.py").exists()


def test_materialize_agent_refuses_overwrite_without_force(tmp_path: Path) -> None:
    import click
    from apx_agent._models import AgentConfig
    from apx_agent.cli import _materialize_agent

    target = tmp_path / "existing"
    target.mkdir()
    (target / "junk.txt").write_text("hi")
    config = AgentConfig(name="existing", model="databricks-claude-sonnet-4-6", instructions="")
    with pytest.raises(click.ClickException, match="already exists"):
        _materialize_agent(config, target, force=False)


# ---------------------------------------------------------------------------
# `_resolve_generate_data_source`
# ---------------------------------------------------------------------------


def test_resolve_generate_data_source_prefers_explicit_flags() -> None:
    from apx_agent.cli import _GenerateClassification, _resolve_generate_data_source

    classification = _GenerateClassification(
        template="data", name="x", persona=None, objective=None, join_key=None,
        catalog_hint="hinted_cat", schema_hint="hinted_sch", missing=(),
    )
    result = _resolve_generate_data_source(classification, "explicit_cat", "explicit_sch", None)
    assert result.catalog == "explicit_cat"
    assert result.schema == "explicit_sch"


def test_resolve_generate_data_source_base_template_has_no_source() -> None:
    from apx_agent.cli import _GenerateClassification, _resolve_generate_data_source

    classification = _GenerateClassification(
        template="base", name="x", persona=None, objective=None, join_key=None,
        catalog_hint=None, schema_hint=None, missing=(),
    )
    result = _resolve_generate_data_source(classification, None, None, None)
    assert result.catalog is None
    assert result.schema is None


def test_resolve_generate_data_source_falls_back_to_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent.cli as cli
    from apx_agent.cli import _GenerateClassification, _resolve_generate_data_source

    monkeypatch.setattr(cli, "_discover_default_data", lambda profile=None: None)
    classification = _GenerateClassification(
        template="data", name="x", persona=None, objective=None, join_key=None,
        catalog_hint=None, schema_hint=None, missing=(),
    )
    result = _resolve_generate_data_source(classification, None, None, None)
    assert result.catalog == "samples"
    assert result.schema == "nyctaxi"


# ---------------------------------------------------------------------------
# `_author_agent_yaml`
# ---------------------------------------------------------------------------


def test_author_agent_yaml_base_template_round_trips_through_load_spec(tmp_path: Path) -> None:
    from apx_agent._yaml_spec import load_spec
    from apx_agent.cli import (
        _GenerateClassification, _ResolvedDataSource, _author_agent_yaml,
    )

    classification = _GenerateClassification(
        template="base", name="helper-agent", persona=None, objective=None,
        join_key=None, catalog_hint=None, schema_hint=None, missing=(),
    )
    data_source = _ResolvedDataSource(catalog=None, schema=None)
    yaml_text = "name: helper-agent\nmodel: databricks-claude-sonnet-4-6\ninstructions: Help with things.\ntools: []\n"

    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=yaml_text))]
    )
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = _author_agent_yaml(None, classification, data_source, {})

    spec_path = tmp_path / "helper-agent.yaml"
    spec_path.write_text(result)
    config = load_spec(spec_path, strict=False)
    assert config.name == "helper-agent"


def test_author_agent_yaml_retries_on_invalid_yaml(tmp_path: Path) -> None:
    from apx_agent.cli import (
        _GenerateClassification, _ResolvedDataSource, _author_agent_yaml,
    )

    classification = _GenerateClassification(
        template="base", name="helper-agent", persona=None, objective=None,
        join_key=None, catalog_hint=None, schema_hint=None, missing=(),
    )
    data_source = _ResolvedDataSource(catalog=None, schema=None)

    bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not: valid: : yaml: :"))])
    good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="name: helper-agent\nmodel: databricks-claude-sonnet-4-6\ninstructions: Help.\ntools: []\n"
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [bad, good]

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = _author_agent_yaml(None, classification, data_source, {})

    assert "helper-agent" in result
    assert fake_ws.serving_endpoints.query.call_count == 2


def test_author_agent_yaml_coworker_template_round_trips(tmp_path: Path) -> None:
    from apx_agent._yaml_spec import load_spec
    from apx_agent.cli import (
        _GenerateClassification, _ResolvedDataSource, _author_agent_yaml,
    )

    classification = _GenerateClassification(
        template="coworker", name="unbilled-revenue-agent",
        persona="a revenue operations analyst", objective="flag unbilled revenue",
        join_key="account_id", catalog_hint=None, schema_hint=None, missing=(),
    )
    data_source = _ResolvedDataSource(catalog="main", schema="revops")
    yaml_text = (
        "name: unbilled-revenue-agent\n"
        "model: databricks-claude-sonnet-4-6\n"
        "instructions: You are a revenue operations analyst.\n"
        "template:\n  name: coworker\n  catalog: main\n  schema: revops\n"
        "  persona: a revenue operations analyst\n  join_key: account_id\n"
        "  objective: flag unbilled revenue\n  memory: persistent\n"
        "tools: []\n"
    )
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=yaml_text))]
    )
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = _author_agent_yaml(None, classification, data_source, {})

    spec_path = tmp_path / "unbilled-revenue-agent.yaml"
    spec_path.write_text(result)
    config = load_spec(spec_path, strict=False)
    assert config.template["catalog"] == "main"


def test_author_agent_yaml_data_template_round_trips(tmp_path: Path) -> None:
    from apx_agent._yaml_spec import load_spec
    from apx_agent.cli import (
        _GenerateClassification, _ResolvedDataSource, _author_agent_yaml,
    )

    classification = _GenerateClassification(
        template="data", name="sales-agent", persona=None, objective=None,
        join_key=None, catalog_hint=None, schema_hint=None, missing=(),
    )
    data_source = _ResolvedDataSource(catalog="main", schema="sales")
    yaml_text = (
        "name: sales-agent\nmodel: databricks-claude-sonnet-4-6\n"
        "instructions: Answer sales questions.\n"
        "template:\n  name: data\n  catalog: main\n  schema: sales\n"
        "tools: []\n"
    )
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=yaml_text))]
    )
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = _author_agent_yaml(None, classification, data_source, {})

    spec_path = tmp_path / "sales-agent.yaml"
    spec_path.write_text(result)
    config = load_spec(spec_path, strict=False)
    assert config.template["schema"] == "sales"


# ---------------------------------------------------------------------------
# `apx-agent generate`
# ---------------------------------------------------------------------------


def test_generate_command_materializes_describable_project(tmp_path: Path) -> None:
    classify_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({
            "template": "base", "name": "helper-agent", "persona": None,
            "objective": None, "join_key": None, "catalog_hint": None,
            "schema_hint": None, "missing": [],
        })
    ))])
    author_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=(
            "name: helper-agent\nmodel: databricks-claude-sonnet-4-6\n"
            "instructions: Help with general questions.\ntools: []\n"
        )
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [classify_response, author_response]

    runner = CliRunner()
    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = runner.invoke(
            main,
            ["generate", "a simple helper agent that answers general questions",
             "--dir", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output

    project = tmp_path / "helper-agent"
    assert (project / "agent.py").exists()
    assert (project / "pyproject.toml").exists()

    # click.testing.CliRunner.invoke (click 8.3.1, this repo's pinned
    # version) has no cwd kwarg — chdir manually instead. Also clear the
    # bare "agent" module from sys.modules: other tests (e.g. the
    # coworker-gallery test) import an agent.py under that same default
    # module name from a different directory, and importlib.import_module
    # would otherwise hand back their cached module instead of this one's.
    prev = os.getcwd()
    sys.modules.pop("agent", None)
    os.chdir(project)
    try:
        describe_result = CliRunner().invoke(main, ["agents", "describe"])
    finally:
        os.chdir(prev)
        sys.modules.pop("agent", None)
    assert describe_result.exit_code == 0, describe_result.output
    assert "helper-agent" in describe_result.output or "Help with general questions" in describe_result.output


def test_generate_sanitizes_llm_authored_name_before_building_path(tmp_path: Path) -> None:
    # AgentConfig.name is an unconstrained str -- an LLM-authored YAML could
    # contain a path-traversal-shaped name. generate() must not use it
    # unsanitized to build the materialize target, matching the guard
    # scaffold() already applies to its own `name` argument.
    classify_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({
            "template": "base", "name": "escape-agent", "persona": None,
            "objective": None, "join_key": None, "catalog_hint": None,
            "schema_hint": None, "missing": [],
        })
    ))])
    author_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=(
            "name: ../../escaped-agent\nmodel: databricks-claude-sonnet-4-6\n"
            "instructions: Help.\ntools: []\n"
        )
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [classify_response, author_response]

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = CliRunner().invoke(
            main, ["generate", "an agent", "--dir", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "escaped-agent").exists()
    assert not (tmp_path.parent / "escaped-agent").exists()


def test_generate_target_exists_needs_force(tmp_path: Path) -> None:
    (tmp_path / "helper-agent").mkdir()
    (tmp_path / "helper-agent" / "junk.txt").write_text("hi")

    classify_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({
            "template": "base", "name": "helper-agent", "persona": None,
            "objective": None, "join_key": None, "catalog_hint": None,
            "schema_hint": None, "missing": [],
        })
    ))])
    author_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="name: helper-agent\nmodel: databricks-claude-sonnet-4-6\ninstructions: Help.\ntools: []\n"
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [classify_response, author_response]

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = CliRunner().invoke(
            main, ["generate", "a helper agent", "--dir", str(tmp_path)],
        )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_generate_llm_failure_points_at_scaffold_fallback() -> None:
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = RuntimeError("connection refused")

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = CliRunner().invoke(main, ["generate", "a helper agent"])
    assert result.exit_code != 0
    assert "apx-agent agents scaffold" in result.output


def test_generate_prompts_only_for_classifier_flagged_missing_fields(tmp_path: Path) -> None:
    # missing=["objective"] must drive exactly one click.prompt for the
    # objective field — proves `filled` is actually threaded from the
    # classifier's `missing` list through to the interactive fill step.
    classify_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({
            "template": "data", "name": "sales-agent", "persona": None,
            "objective": None, "join_key": None, "catalog_hint": None,
            "schema_hint": None, "missing": ["objective"],
        })
    ))])
    author_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=(
            "name: sales-agent\nmodel: databricks-claude-sonnet-4-6\n"
            "instructions: Answer sales questions.\n"
            "template:\n  name: data\n  catalog: main\n  schema: sales\n"
            "tools: []\n"
        )
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [classify_response, author_response]

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = CliRunner().invoke(
            main,
            [
                "generate", "a data agent for finance",
                "--catalog", "main", "--schema", "sales",
                "--dir", str(tmp_path),
            ],
            input="unbilled deals\n",
        )
    assert result.exit_code == 0, result.output
    assert "What should the agent surface?" in result.output
    assert (tmp_path / "sales-agent" / "agent.py").exists()


# ---------------------------------------------------------------------------
# Local deploy-history index (`agents redeploy` support)
# ---------------------------------------------------------------------------


class TestDeployHistoryIndex:
    def test_record_then_load_round_trips(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.setattr(cli, "_deploy_history_path", lambda: tmp_path / "deploy-history.json")

        cli._record_deploy_history(
            Path("/some/project"), "main.apx.my_agent", "apps",
            {"apx.apps.git_sha": "abc123", "apx.git_dirty": "false"},
        )

        entry = cli._load_deploy_history_entry("main.apx.my_agent")
        assert entry is not None
        assert entry["path"] == "/some/project"
        assert entry["target"] == "apps"
        assert entry["git_sha"] == "abc123"
        assert entry["git_dirty"] is False
        assert "deployed_at" in entry

    def test_record_overwrites_prior_entry_for_same_name(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.setattr(cli, "_deploy_history_path", lambda: tmp_path / "deploy-history.json")

        cli._record_deploy_history(Path("/old/path"), "main.apx.my_agent", "apps", {})
        cli._record_deploy_history(Path("/new/path"), "main.apx.my_agent", "apps", {})

        entry = cli._load_deploy_history_entry("main.apx.my_agent")
        assert entry["path"] == "/new/path"

    def test_record_missing_provenance_tags_stores_none(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.setattr(cli, "_deploy_history_path", lambda: tmp_path / "deploy-history.json")

        cli._record_deploy_history(Path("/p"), "main.apx.my_agent", "model-serving", {})

        entry = cli._load_deploy_history_entry("main.apx.my_agent")
        assert entry["git_sha"] is None
        assert entry["git_dirty"] is None

    def test_record_never_raises_on_write_failure(self, tmp_path, monkeypatch):
        from apx_agent import cli

        # Point at a path whose parent can't be created (a file, not a dir).
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setattr(cli, "_deploy_history_path", lambda: blocker / "sub" / "deploy-history.json")

        cli._record_deploy_history(Path("/p"), "main.apx.my_agent", "apps", {})  # must not raise

    def test_load_returns_none_for_missing_file(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.setattr(cli, "_deploy_history_path", lambda: tmp_path / "does-not-exist.json")

        assert cli._load_deploy_history_entry("main.apx.my_agent") is None

    def test_load_returns_none_for_missing_key(self, tmp_path, monkeypatch):
        from apx_agent import cli

        monkeypatch.setattr(cli, "_deploy_history_path", lambda: tmp_path / "deploy-history.json")
        cli._record_deploy_history(Path("/p"), "main.apx.other_agent", "apps", {})

        assert cli._load_deploy_history_entry("main.apx.my_agent") is None

    def test_load_returns_none_for_corrupt_json(self, tmp_path, monkeypatch):
        from apx_agent import cli

        path = tmp_path / "deploy-history.json"
        path.write_text("{not valid json")
        monkeypatch.setattr(cli, "_deploy_history_path", lambda: path)

        assert cli._load_deploy_history_entry("main.apx.my_agent") is None

"""Tests for ``apx deploy coworker.yaml`` — the YAML-first deploy path.

When a .yaml / .yml argument is passed to ``apx deploy``, the command must:
  1. Call _deploy_from_yaml (not the old deploy path).
  2. NOT call _deploy_from_yaml when no spec or a non-yaml spec is passed.
  3. Raise a ClickException (exit != 0) with the file path in the message
     for an invalid YAML spec.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from apx_agent.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_yaml(tmp_path: Path, *, name: str = "my-agent") -> Path:
    """Write a minimal valid coworker.yaml and return its path."""
    p = tmp_path / f"{name}.yaml"
    p.write_text(f"name: {name}\ndescription: test agent\n")
    return p


# ---------------------------------------------------------------------------
# Test 1: .yaml argument routes to _deploy_from_yaml
# ---------------------------------------------------------------------------


def test_deploy_yaml_argument_calls_deploy_from_yaml(tmp_path):
    """When a .yaml path is passed, _deploy_from_yaml is invoked."""
    spec = _minimal_yaml(tmp_path)
    runner = CliRunner()

    with patch("apx_agent.cli._deploy_from_yaml") as mock_deploy:
        result = runner.invoke(main, ["deploy", str(spec)], catch_exceptions=False)

    mock_deploy.assert_called_once()
    call_kwargs = mock_deploy.call_args
    assert call_kwargs.kwargs.get("yaml_path") == spec or call_kwargs.args[0] == spec


def test_deploy_yml_extension_also_routes(tmp_path):
    """A .yml (not .yaml) extension also routes to _deploy_from_yaml."""
    spec = tmp_path / "agent.yml"
    spec.write_text("name: agent-yml\ndescription: test\n")
    runner = CliRunner()

    with patch("apx_agent.cli._deploy_from_yaml") as mock_deploy:
        result = runner.invoke(main, ["deploy", str(spec)], catch_exceptions=False)

    mock_deploy.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: No argument (or non-yaml) does NOT call _deploy_from_yaml
# ---------------------------------------------------------------------------


def test_deploy_target_apps_no_spec_skips_deploy_from_yaml(tmp_path):
    """When --target apps and no spec, _deploy_from_yaml is NOT called."""
    runner = CliRunner()

    # Stub _deploy_apps so the old path doesn't fail on missing project files.
    with patch("apx_agent.cli._deploy_from_yaml") as mock_yaml, \
         patch("apx_agent.cli._deploy_apps") as mock_apps:
        result = runner.invoke(
            main,
            ["deploy", "--target", "apps"],
            catch_exceptions=False,
        )

    mock_yaml.assert_not_called()
    mock_apps.assert_called_once()


def test_deploy_no_spec_does_not_call_deploy_from_yaml(tmp_path):
    """Invoking deploy without any positional arg skips _deploy_from_yaml."""
    runner = CliRunner()
    with patch("apx_agent.cli._deploy_from_yaml") as mock_yaml, \
         patch("apx_agent.cli._deploy_apps") as mock_apps:
        # Provide --target apps so _deploy_apps is the branch taken;
        # otherwise model-serving raises UsageError before we can assert.
        runner.invoke(
            main,
            ["deploy", "--target", "apps"],
            catch_exceptions=False,
        )
    mock_yaml.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Invalid YAML spec raises ClickException
# ---------------------------------------------------------------------------


def test_deploy_invalid_yaml_raises_click_exception(tmp_path):
    """An invalid spec (missing 'name') must exit non-zero with file path."""
    bad_spec = tmp_path / "bad.yaml"
    # Write a YAML file that is missing the required 'name' field.
    bad_spec.write_text("description: no name here\n")

    runner = CliRunner()
    result = runner.invoke(main, ["deploy", str(bad_spec)])

    assert result.exit_code != 0
    # The error message must mention the file path so the user knows which
    # file failed validation.
    output = result.output + (str(result.exception) if result.exception else "")
    assert str(bad_spec) in output or "bad.yaml" in output

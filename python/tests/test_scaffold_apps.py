"""Tests for ``apx scaffold --target apps`` — the Databricks Apps scaffold.

Covers:
  1. ``--target apps`` writes the expected file tree.
  2. Generated ``databricks.yml`` is valid YAML and contains the app +
     experiment resources.
  3. Generated ``pyproject.toml`` is valid TOML and lists apx-agent +
     mlflow[databricks].
  4. Generated ``agent_server/agent.py`` parses as valid Python.
  5. ``apx scaffold`` with no ``--target`` still produces the Model Serving
     layout (backwards compatibility).
  6. ``--target apps --force`` overwrites an existing directory.

Uses ``click.testing.CliRunner`` + ``tmp_path``.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apx_agent.cli import main


APPS_EXPECTED_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "databricks.yml",
    ".env.example",
    ".gitignore",
    "README.md",
    "agent_server/__init__.py",
    "agent_server/agent.py",
    "agent_server/start_server.py",
    "scripts/__init__.py",
    "scripts/quickstart.py",
)


# ---------------------------------------------------------------------------
# Test 1: file tree
# ---------------------------------------------------------------------------


def test_scaffold_apps_creates_expected_file_tree(tmp_path: Path) -> None:
    """``apx scaffold my_agent --target apps`` creates every expected file."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"
    for rel in APPS_EXPECTED_FILES:
        assert (base / rel).exists(), f"missing {rel}"


# ---------------------------------------------------------------------------
# Test 2: databricks.yml is valid YAML and wires up the app + experiment
# ---------------------------------------------------------------------------


def test_scaffold_apps_databricks_yml_is_valid_yaml(tmp_path: Path) -> None:
    """``databricks.yml`` parses as YAML and lists the bundle's app + experiment."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "databricks.yml").read_text()
    parsed = yaml.safe_load(content)

    assert parsed["bundle"]["name"] == "my_agent"
    apps = parsed["resources"]["apps"]
    assert "my_agent" in apps
    assert apps["my_agent"]["name"] == "my_agent"
    assert apps["my_agent"]["source_code_path"] == "./"

    experiments = parsed["resources"]["experiments"]
    assert "my_agent_experiment" in experiments

    # Targets are pre-wired for dev (default) + prod.
    assert "dev" in parsed["targets"]
    assert "prod" in parsed["targets"]
    assert parsed["targets"]["dev"]["default"] is True


# ---------------------------------------------------------------------------
# Test 3: pyproject.toml is valid TOML + lists required dependencies
# ---------------------------------------------------------------------------


def test_scaffold_apps_pyproject_is_valid_toml(tmp_path: Path) -> None:
    """``pyproject.toml`` parses and lists apx-agent + mlflow[databricks]."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "pyproject.toml").read_text()
    parsed = tomllib.loads(content)

    assert parsed["project"]["name"] == "my_agent"
    deps = parsed["project"]["dependencies"]
    assert "apx-agent" in deps
    # The mlflow dependency is pinned with the [databricks] extra and a
    # minimum version. Search loosely so the version pin can move.
    assert any("mlflow[databricks]" in d for d in deps), deps

    # The console scripts wire up `uv run start-server` and `uv run quickstart`.
    scripts = parsed["project"]["scripts"]
    assert scripts["start-server"] == "agent_server.start_server:main"
    assert scripts["quickstart"] == "scripts.quickstart:main"


# ---------------------------------------------------------------------------
# Test 4: agent_server/agent.py parses as valid Python
# ---------------------------------------------------------------------------


def test_scaffold_apps_agent_module_is_valid_python(tmp_path: Path) -> None:
    """``agent_server/agent.py`` parses cleanly (without resolving imports).

    We use ``ast.parse`` instead of ``importlib`` because the generated file
    imports ``compile_to_responses_agent`` and ``mlflow.genai.agent_server``,
    which may not be available in the test environment. Parse-only is enough
    to catch syntax bugs in the template.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    agent_src = (tmp_path / "my_agent" / "agent_server" / "agent.py").read_text()
    # Will raise SyntaxError if the template is malformed.
    ast.parse(agent_src)

    # Sanity-check the start_server + quickstart modules too — they're
    # generated from the same scaffold pipeline.
    start_src = (tmp_path / "my_agent" / "agent_server" / "start_server.py").read_text()
    ast.parse(start_src)

    quickstart_src = (tmp_path / "my_agent" / "scripts" / "quickstart.py").read_text()
    ast.parse(quickstart_src)


# ---------------------------------------------------------------------------
# Test 5: backwards compat — no --target keeps the Model Serving shape
# ---------------------------------------------------------------------------


def test_scaffold_default_target_is_model_serving(tmp_path: Path) -> None:
    """No ``--target`` flag still emits the flat agent.py + app.py layout."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"

    # Model-serving shape: flat agent.py + app.py
    for rel in ("pyproject.toml", "agent.py", "app.py", ".gitignore", "README.md"):
        assert (base / rel).exists(), f"missing {rel}"

    # Apps-only artefacts must NOT appear.
    assert not (base / "agent_server").exists()
    assert not (base / "scripts").exists()
    assert not (base / "databricks.yml").exists()


# ---------------------------------------------------------------------------
# Test 6: --force overwrites an existing directory
# ---------------------------------------------------------------------------


def test_scaffold_apps_force_overwrites_existing_dir(tmp_path: Path) -> None:
    """``--target apps --force`` replaces stale files in an existing tree."""
    runner = CliRunner()
    base = tmp_path / "my_agent"
    (base / "agent_server").mkdir(parents=True)
    sentinel = base / "agent_server" / "agent.py"
    sentinel.write_text("# OLD STALE CONTENT")

    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0, result.output

    fresh = sentinel.read_text()
    assert "# OLD STALE CONTENT" not in fresh
    assert "compile_to_responses_agent" in fresh

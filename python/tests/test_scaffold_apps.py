"""Tests for ``apx scaffold --target apps`` — the Databricks Apps scaffold.

Covers:
  1. ``--target apps`` writes the expected file tree (ADK-style: top-level
     ``agent.py`` + framework-only ``agent_server/start_server.py``).
  2. Generated ``databricks.yml`` is valid YAML, contains the app +
     experiment resources, AND its ``artifacts.default.build`` step copies
     the top-level ``agent.py`` into ``.build/``.
  3. Generated ``pyproject.toml`` is valid TOML and lists apx-agent +
     mlflow[databricks].
  4. Generated top-level ``agent.py`` parses as valid Python and the
     framework boilerplate at ``agent_server/start_server.py`` imports
     ``from agent import agent``.
  5. ``apx scaffold`` with no ``--target`` still produces the Model Serving
     layout (backwards compatibility).
  6. ``--target apps --force`` overwrites an existing directory.
  7. The ADK-style layout: ``agent_server/agent.py`` MUST NOT exist
     (legacy split-file shape is removed).

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
    # ADK-style: user content at top level, framework boilerplate under agent_server/.
    "agent.py",
    "agent_server/__init__.py",
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

    # ADK-style layout: the legacy ``agent_server/agent.py`` shape is gone.
    # All user content lives at top-level ``agent.py``.
    assert not (base / "agent_server" / "agent.py").exists()


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
    # Build artifacts step copies sources into ./.build before deploy so the
    # apx-agent wheel can ride along — see commit 6f84ad24 for the rationale.
    assert apps["my_agent"]["source_code_path"] == "./.build"

    # ADK-style layout: the artifacts script must copy the top-level
    # ``agent.py`` into ``.build/``. Without this line the deployed App
    # container can't import ``from agent import agent`` and falls over at
    # startup. Regression guard for the layout migration.
    build_script = parsed["artifacts"]["default"]["build"]
    assert "cp agent.py" in build_script, build_script

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
    # The [langgraph] extra is REQUIRED at runtime — compile_to_responses_agent
    # pulls in langchain_core via compile_to_langgraph. Bare 'apx-agent' fails
    # at first request inside the deployed App. See commit 6f84ad24.
    assert any(d == "apx-agent[langgraph]" or d.startswith("apx-agent[") for d in deps), deps
    # The mlflow dependency is pinned with the [databricks] extra and a
    # minimum version. Search loosely so the version pin can move.
    assert any("mlflow[databricks]" in d for d in deps), deps

    # ``uv run quickstart`` is still the canonical bootstrap entry point.
    # ``start-server`` is gone — the deploy now uses uvicorn against
    # ``agent_server.start_server:app`` directly (see databricks.yml).
    scripts = parsed["project"]["scripts"]
    assert scripts["quickstart"] == "scripts.quickstart:main"
    assert "start-server" not in scripts


# ---------------------------------------------------------------------------
# Test 4: agent_server/agent.py parses as valid Python
# ---------------------------------------------------------------------------


def test_scaffold_apps_agent_module_is_valid_python(tmp_path: Path) -> None:
    """Top-level ``agent.py`` parses cleanly (without resolving imports).

    We use ``ast.parse`` instead of ``importlib`` because the generated file
    imports apx-agent / mlflow.genai pieces that may not be installed in the
    test environment. Parse-only catches syntax bugs in the template.

    Also asserts ``agent_server/start_server.py`` wires the ADK-style
    ``from agent import agent`` import — the load-bearing line that ties
    framework boilerplate to the user-authored top-level agent.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    agent_src = (tmp_path / "my_agent" / "agent.py").read_text()
    # Will raise SyntaxError if the template is malformed.
    ast.parse(agent_src)
    # The user's agent module must expose an ``agent`` symbol that
    # start_server.py imports.
    assert "agent =" in agent_src or "\nagent=" in agent_src

    start_src = (tmp_path / "my_agent" / "agent_server" / "start_server.py").read_text()
    ast.parse(start_src)
    # Framework boilerplate consumes the top-level agent via ``from agent import agent``.
    assert "from agent import agent" in start_src
    # And wires the standard compile + register + MCP-mount stack.
    assert "compile_to_responses_agent" in start_src
    assert "mount_mcp_endpoints" in start_src

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
    base.mkdir(parents=True)
    sentinel = base / "agent.py"
    sentinel.write_text("# OLD STALE CONTENT")

    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0, result.output

    fresh = sentinel.read_text()
    assert "# OLD STALE CONTENT" not in fresh
    # The top-level scaffold template defines an ``agent = DataAgent(...)`` block.
    assert "agent = DataAgent(" in fresh


# ---------------------------------------------------------------------------
# Test 7: .gitignore ignores the personal canvas sidecar
# ---------------------------------------------------------------------------


def test_scaffold_apps_gitignore_includes_sidecar(tmp_path: Path) -> None:
    """The scaffold's .gitignore should ignore the personal canvas sidecar."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    gitignore = (tmp_path / "my_agent" / ".gitignore").read_text()
    assert ".apx-builder.json" in gitignore

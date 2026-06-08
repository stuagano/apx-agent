"""Tests for ``apx-agent scaffold --target apps`` — the Databricks Apps scaffold.

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
  5. ``apx-agent scaffold`` with no ``--target`` still produces the Model Serving
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
    """``apx-agent scaffold my_agent --target apps`` creates every expected file."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
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
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
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
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "pyproject.toml").read_text()
    parsed = tomllib.loads(content)

    assert parsed["project"]["name"] == "my_agent"
    deps = parsed["project"]["dependencies"]
    # apx-agent includes langgraph/langchain as required deps — no extra needed.
    # The dep line references just "apx-agent" (or a git+https URL to the same).
    assert any("apx-agent" in d for d in deps), deps
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
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
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


def test_scaffold_default_target_is_apps(tmp_path: Path) -> None:
    """No ``--target`` flag now emits the Databricks Apps bundle layout."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "my_agent", "--dir", str(tmp_path), "--no-yaml"],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"

    # Apps shape: agent.py + agent_server/ + databricks.yml bundle.
    for rel in ("pyproject.toml", "agent.py", "databricks.yml",
                "agent_server/start_server.py", "scripts/quickstart.py"):
        assert (base / rel).exists(), f"missing {rel}"

    # The flat model-serving app.py must NOT appear by default anymore.
    assert not (base / "app.py").exists()


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
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--force", "--no-yaml"],
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
        ["scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
    )
    assert result.exit_code == 0, result.output
    gitignore = (tmp_path / "my_agent" / ".gitignore").read_text()
    assert ".apx-builder.json" in gitignore


# ---------------------------------------------------------------------------
# Test 8: framework-checkout auto-redirect (the "I ran apx-agent scaffold at the
# repo root" gotcha that otherwise produces a broken `path = ".."` install)
# ---------------------------------------------------------------------------


def _make_fake_framework_checkout(root: Path) -> Path:
    """Build a minimal fake apx-agent repo checkout at ``root``: an empty
    ``python/`` subdir with a ``pyproject.toml`` that declares
    ``[project].name = "apx-agent"`` — enough to trip the detection."""
    (root / "python").mkdir(parents=True)
    (root / "python" / "pyproject.toml").write_text(
        '[project]\nname = "apx-agent"\nversion = "0.1.0"\n'
    )
    return root / "python"


def test_scaffold_at_framework_repo_root_redirects_into_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``apx-agent scaffold X`` from the framework repo root auto-redirects
    into ``python/X/`` so the editable ``path = ".."`` install resolves."""
    framework_root = tmp_path / "fakeframework"
    framework_python = _make_fake_framework_checkout(framework_root)

    monkeypatch.chdir(framework_root)
    runner = CliRunner()
    result = runner.invoke(main, ["scaffold", "myagent", "--no-yaml"])

    assert result.exit_code == 0, result.output
    assert (framework_python / "myagent" / "pyproject.toml").exists()
    assert not (framework_root / "myagent").exists()
    assert "Scaffolding into python/" in result.output


def test_scaffold_inside_python_does_not_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect must NOT fire when the user is already in the framework's
    ``python/`` dir — the existing layout is correct and shouldn't move."""
    framework_root = tmp_path / "fakeframework"
    framework_python = _make_fake_framework_checkout(framework_root)

    monkeypatch.chdir(framework_python)
    runner = CliRunner()
    result = runner.invoke(main, ["scaffold", "myagent", "--no-yaml"])

    assert result.exit_code == 0, result.output
    assert (framework_python / "myagent" / "pyproject.toml").exists()
    assert "Scaffolding into python/" not in result.output


def test_scaffold_here_overrides_auto_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--here`` opts out of the redirect; the scaffold lands at cwd and
    its pyproject uses a git+https install instead of the editable path."""
    framework_root = tmp_path / "fakeframework"
    framework_python = _make_fake_framework_checkout(framework_root)

    monkeypatch.chdir(framework_root)
    runner = CliRunner()
    result = runner.invoke(main, ["scaffold", "myagent", "--here", "--no-yaml"])

    assert result.exit_code == 0, result.output
    assert (framework_root / "myagent" / "pyproject.toml").exists()
    assert not (framework_python / "myagent").exists()

    pyproject = (framework_root / "myagent" / "pyproject.toml").read_text()
    assert "git+https://github.com/stuagano/apx-agent.git" in pyproject
    assert 'path = ".."' not in pyproject


# ---------------------------------------------------------------------------
# --template coworker: persona via interactive flow
# ---------------------------------------------------------------------------


def test_scaffold_coworker_with_persona_baked_into_agent(tmp_path: Path) -> None:
    """Interactive coworker scaffold bakes persona into agent.py."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scaffold", "my_coworker",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--template", "coworker",
            "--interactive",
            "--no-yaml",
        ],
        # catalog → "main", schema → "sales", persona → role text, join_key → blank, objective → blank
        input="main\nsales\na sales analyst who knows revenue data deeply\n\n\n",
        catch_exceptions=False,
        env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
    )
    assert result.exit_code == 0, result.output
    agent_src = (tmp_path / "my_coworker" / "agent.py").read_text()
    assert "a sales analyst who knows revenue data deeply" in agent_src
    assert "persona=" in agent_src


def test_scaffold_coworker_with_persona_and_objective(tmp_path: Path) -> None:
    """Interactive coworker scaffold bakes persona, join_key, and objective into agent.py."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scaffold", "fraud_agent",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--template", "coworker",
            "--interactive",
            "--no-yaml",
        ],
        # catalog, schema, persona, join_key, objective
        input="main\nfraud\na fraud detection analyst\ntransaction ID\ndetect fraudulent transactions and flag anomalies\n",
        catch_exceptions=False,
        env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
    )
    assert result.exit_code == 0, result.output
    agent_src = (tmp_path / "fraud_agent" / "agent.py").read_text()
    assert "a fraud detection analyst" in agent_src
    assert "transaction ID" in agent_src
    assert "detect fraudulent transactions and flag anomalies" in agent_src
    assert "persona=" in agent_src
    assert "join_key=" in agent_src
    assert "objective=" in agent_src


def test_scaffold_coworker_without_persona_omits_kwarg(tmp_path: Path) -> None:
    """When no persona/objective given, the CoworkerAgent call omits those kwargs."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scaffold", "my_coworker2",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--template", "coworker",
            "--catalog", "main", "--schema", "sales",
            "--no-interactive",
            "--no-yaml",
        ],
        catch_exceptions=False,
        env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
    )
    assert result.exit_code == 0, result.output
    agent_src = (tmp_path / "my_coworker2" / "agent.py").read_text()
    assert "persona=" not in agent_src
    assert "objective=" not in agent_src


def test_scaffold_interactive_prompts_for_catalog_schema_persona(tmp_path: Path) -> None:
    """``--interactive`` prompts for catalog, schema, and persona for coworker.

    Without a real workspace, catalog list is empty → falls to free-text prompts.
    The runner injects "main\\n" for catalog, "sales\\n" for schema, and
    "payroll analyst\\n" for persona.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scaffold", "interactive_agent",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--template", "coworker",
            "--interactive",
            "--no-yaml",
        ],
        # catalog, schema, persona, join_key (blank), objective (blank)
        input="main\nsales\npayroll analyst\n\n\n",
        catch_exceptions=False,
        env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
    )
    assert result.exit_code == 0, result.output
    agent_src = (tmp_path / "interactive_agent" / "agent.py").read_text()
    assert "payroll analyst" in agent_src


def test_scaffold_no_interactive_skips_prompts(tmp_path: Path) -> None:
    """``--no-interactive`` skips prompting even with missing catalog/schema."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scaffold", "silent_agent",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--no-interactive",
            "--no-yaml",
        ],
        catch_exceptions=False,
        env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
    )
    # Falls through to auto-detect (which returns samples.nyctaxi when no ws).
    assert result.exit_code == 0, result.output


def test_start_server_loads_agent_config_for_session(tmp_path: Path) -> None:
    """start_server.py must call _load_agent_config() so [tool.apx.agent.session]
    in pyproject.toml is respected — not just agent-constructor memory=."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scaffold", "sess_agent", "--target", "apps", "--dir", str(tmp_path), "--no-yaml"],
    )
    assert result.exit_code == 0, result.output

    start_server = (tmp_path / "sess_agent" / "agent_server" / "start_server.py").read_text()
    # The config must be loaded from pyproject.toml and passed to resolve_session_store.
    assert "_load_agent_config" in start_server
    assert "resolve_session_store(_agent_config" in start_server

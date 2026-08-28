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
from typing import Any
from unittest.mock import patch

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
    "agent_server/start_host.py",
    "agent_server/start_server.py",
    "scripts/__init__.py",
    "scripts/quickstart.py",
    "tests/test_agent_imports.py",
    ".github/workflows/pr-to-main.yml",
    ".github/workflows/pr-to-release.yml",
    ".github/workflows/release-deploy-prod.yml",
)


# ---------------------------------------------------------------------------
# Test 1: file tree
# ---------------------------------------------------------------------------


def test_scaffold_apps_creates_expected_file_tree(tmp_path: Path) -> None:
    """``apx-agent scaffold my_agent --target apps`` creates every expected file."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
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
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "databricks.yml").read_text()
    parsed = yaml.safe_load(content)

    assert parsed["bundle"]["name"] == "my_agent"
    apps = parsed["resources"]["apps"]
    assert "my_agent" in apps
    assert apps["my_agent"]["name"] == "my-agent"
    # Build artifacts step copies sources into ./.build before deploy so the
    # apx-agent wheel can ride along — see commit 6f84ad24 for the rationale.
    assert apps["my_agent"]["source_code_path"] == "./.build"

    # ADK-style layout: the artifacts script must copy the top-level
    # ``agent.py`` into ``.build/``. Without this line the deployed App
    # container can't import ``from agent import agent`` and falls over at
    # startup. Regression guard for the layout migration.
    build_script = parsed["artifacts"]["default"]["build"]
    assert "cp agent.py" in build_script, build_script
    assert ".build/apx_appkit_host" in build_script, build_script
    assert apps["my_agent"]["config"]["command"] == [
        "python",
        "-m",
        "agent_server.start_host",
    ]
    env = {
        item["name"]: item["value"]
        for item in apps["my_agent"]["config"]["env"]
    }
    assert env["APX_APPS_HOST"] == "python"

    experiments = parsed["resources"]["experiments"]
    assert "my_agent_experiment" in experiments

    # Targets are pre-wired for laptop ``dev`` + CI ``staging`` / ``prod``.
    assert "dev" in parsed["targets"]
    assert "staging" in parsed["targets"]
    assert "prod" in parsed["targets"]
    assert parsed["targets"]["dev"]["default"] is True
    assert (
        parsed["targets"]["staging"]["resources"]["apps"]["my_agent"]["name"]
        == "my-agent-staging"
    )

    # Version correlation (issue #404): the bundle declares the correlation
    # vars and threads them into the app container env, so deploy can inject
    # `--var apx_git_sha=<sha>` and traces carry per-version identity.
    variables = parsed["variables"]
    assert "apx_git_sha" in variables
    assert "apx_model_version" in variables
    assert variables["apx_model_version"]["default"] == "unregistered"
    env_entries = {
        e["name"]: e["value"]
        for e in parsed["resources"]["apps"]["my_agent"]["config"]["env"]
    }
    assert env_entries["APX_AGENT_NAME"] == "my_agent"
    assert env_entries["APX_GIT_SHA"] == "${var.apx_git_sha}"
    assert env_entries["APX_MODEL_VERSION"] == "${var.apx_model_version}"


# ---------------------------------------------------------------------------
# Test 3: pyproject.toml is valid TOML + lists required dependencies
# ---------------------------------------------------------------------------


def test_scaffold_apps_pyproject_is_valid_toml(tmp_path: Path) -> None:
    """``pyproject.toml`` parses and lists apx-agent + mlflow[databricks]."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
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

    agent_cfg = parsed["tool"]["apx"]["agent"]
    assert agent_cfg["catalog"] == "samples"
    assert agent_cfg["schema"] == "tpch"
    assert agent_cfg["registered_model"] == "samples.tpch.my_agent"

    # ``uv run quickstart`` is still the canonical bootstrap entry point.
    # ``start-server`` is gone — the deploy now uses uvicorn against
    # ``agent_server.start_server:app`` directly (see databricks.yml).
    scripts = parsed["project"]["scripts"]
    assert scripts["quickstart"] == "scripts.quickstart:main"
    assert "start-server" not in scripts

    # Dev group powers scaffolded CI (``uv sync --group dev``).
    assert "pytest>=8.0" in parsed["dependency-groups"]["dev"]


def test_scaffold_apps_base_omits_uc_registration_config(tmp_path: Path) -> None:
    """Base Apps scaffolds have no UC data source, so no inferred ledger name."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "base", "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads((tmp_path / "my_agent" / "pyproject.toml").read_text())
    agent_cfg = parsed["tool"]["apx"]["agent"]
    assert "catalog" not in agent_cfg
    assert "schema" not in agent_cfg
    assert "registered_model" not in agent_cfg


# ---------------------------------------------------------------------------
# Test 3b: session backend defaults to Lakebase, --no-lakebase opts out
# ---------------------------------------------------------------------------


def test_scaffold_apps_defaults_to_lakebase_session(tmp_path: Path) -> None:
    """A default scaffold writes an active ``[tool.apx.agent.session]`` lakebase block."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "pyproject.toml").read_text()
    parsed = tomllib.loads(content)
    session = parsed["tool"]["apx"]["agent"]["session"]
    assert session["type"] == "lakebase"
    assert session["host"] == "${LAKEBASE_HOST}"  # endpoint from env
    assert session["database"] == "my_agent"  # per-agent database (name slug)


def test_scaffold_apps_no_lakebase_omits_session_block(tmp_path: Path) -> None:
    """``--no-lakebase`` leaves no session block — falls back to in-memory sessions."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path),
         "--no-lakebase"],
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "my_agent" / "pyproject.toml").read_text()
    parsed = tomllib.loads(content)
    assert "session" not in parsed.get("tool", {}).get("apx", {}).get("agent", {})


def test_scaffold_apps_echoes_lakebase_guidance(tmp_path: Path) -> None:
    """The default scaffold tells the user how to make Lakebase sessions durable."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "durable on Lakebase" in result.output
    assert "LAKEBASE_HOST" in result.output  # the env var to set


def test_scaffold_apps_no_lakebase_skips_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-lakebase`` prints no Lakebase session guidance."""
    monkeypatch.setattr("apx_agent.cli._make_ws_for_scaffold", lambda profile: None)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path),
         "--no-lakebase"],
    )
    assert result.exit_code == 0, result.output
    assert "durable on Lakebase" not in result.output


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
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
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
    start_host_src = (tmp_path / "my_agent" / "agent_server" / "start_host.py").read_text()
    ast.parse(start_host_src)
    assert "APX_APPS_HOST" in start_host_src
    assert "agent_server.start_server:app" in start_host_src
    assert "apx_appkit_host" in start_host_src

    quickstart_src = (tmp_path / "my_agent" / "scripts" / "quickstart.py").read_text()
    ast.parse(quickstart_src)
    assert 'catalog_name="samples"' in quickstart_src
    assert 'schema_name="tpch"' in quickstart_src
    assert 'table_prefix="apx_my_agent"' in quickstart_src
    assert "provision_lakehouse_observability" in quickstart_src


def test_scaffold_apps_writes_apx_timeline_sql(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps",
            "--template", "data", "--catalog", "samples", "--schema", "tpch",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    sql = (tmp_path / "my_agent" / ".apx" / "sql" / "apx_agent_timeline.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS `samples`.`tpch`.`apx_agent_events`" in sql
    assert "CREATE OR REPLACE VIEW `samples`.`tpch`.`apx_agent_timeline`" in sql
    assert "`samples`.`tpch`.`apx_my_agent_trace_unified`" in sql
    assert "COALESCE(CAST(u.tags['apx.session.id'] AS STRING), span.attributes:['apx.session.id']::STRING)" in sql
    assert "span.attributes:['apx.tool.name']::STRING" in sql
    assert "UNION ALL" in sql


# ---------------------------------------------------------------------------
# Test 5: backwards compat — no --target keeps the Model Serving shape
# ---------------------------------------------------------------------------


def test_scaffold_default_target_is_apps(tmp_path: Path) -> None:
    """No ``--target`` flag now emits the Databricks Apps bundle layout."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "scaffold", "my_agent", "--dir", str(tmp_path)],
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
        ["agents", "scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path), "--force"],
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
        ["agents", "scaffold", "my_agent", "--target", "apps", "--dir", str(tmp_path)],
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
    result = runner.invoke(main, ["agents", "scaffold", "myagent"])

    assert result.exit_code == 0, result.output
    assert (framework_python / "myagent" / "pyproject.toml").exists()
    assert not (framework_root / "myagent").exists()
    assert "scaffolding inside the apx-agent repo" in result.output.lower()


def test_scaffold_inside_python_does_not_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect must NOT fire when the user is already in the framework's
    ``python/`` dir — the existing layout is correct and shouldn't move."""
    framework_root = tmp_path / "fakeframework"
    framework_python = _make_fake_framework_checkout(framework_root)

    monkeypatch.chdir(framework_python)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "scaffold", "myagent"])

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
    result = runner.invoke(main, ["agents", "scaffold", "myagent", "--here"])

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
    with patch("apx_agent.cli._gate_workspace_for_scaffold", return_value=None):
        result = runner.invoke(
            main,
            [
                "agents", "scaffold", "my_coworker",
                "--dir", str(tmp_path),
                "--target", "apps",
                "--template", "coworker",
                "--interactive",
            ],
            # catalog → "main", schema → "sales", persona → role text, join_key → blank, objective → blank, instructions → blank
            input="main\nsales\na sales analyst who knows revenue data deeply\n\n\n\n",
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
    with patch("apx_agent.cli._gate_workspace_for_scaffold", return_value=None):
        result = runner.invoke(
            main,
            [
                "agents", "scaffold", "fraud_agent",
                "--dir", str(tmp_path),
                "--target", "apps",
                "--template", "coworker",
                "--interactive",
            ],
            # catalog, schema, persona, join_key, objective, instructions
            input="main\nfraud\na fraud detection analyst\ntransaction ID\ndetect fraudulent transactions and flag anomalies\n\n",
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
            "agents", "scaffold", "my_coworker2",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--template", "coworker",
            "--catalog", "main", "--schema", "sales",
            "--no-interactive",
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

    Without a real workspace (gated to None), catalog list is empty → falls to
    free-text prompts. The runner injects "main\\n" for catalog, "sales\\n" for
    schema, and "payroll analyst\\n" for persona.
    """
    runner = CliRunner()
    with patch("apx_agent.cli._gate_workspace_for_scaffold", return_value=None):
        result = runner.invoke(
            main,
            [
                "agents", "scaffold", "interactive_agent",
                "--dir", str(tmp_path),
                "--target", "apps",
                "--template", "coworker",
                "--interactive",
            ],
            # catalog, schema, persona, join_key (blank), objective (blank), instructions (blank)
            input="main\nsales\npayroll analyst\n\n\n\n",
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
            "agents", "scaffold", "silent_agent",
            "--dir", str(tmp_path),
            "--target", "apps",
            "--no-interactive",
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
        ["agents", "scaffold", "sess_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    start_server = (tmp_path / "sess_agent" / "agent_server" / "start_server.py").read_text()
    # The config must be loaded from pyproject.toml and passed to resolve_conversation_store.
    assert "_load_agent_config" in start_server
    assert "resolve_conversation_store(_agent_config" in start_server
    assert 'os.environ.get("APX_AGENT_NAME")' in start_server


# ---------------------------------------------------------------------------
# #449: explicit --target in a non-TTY produces the documented layout
# ---------------------------------------------------------------------------


def test_scaffold_explicit_target_non_tty_writes_apps_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-interactive ``scaffold NAME --target apps`` must
    produce the documented agent_server/ bundle with exit 0 — not a YAML spec
    + exit 1 — even when the workspace probe is unavailable (#449).

    CliRunner is non-TTY by nature; the probes are stubbed as unreachable.
    """
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_discover_default_data", lambda profile: None)
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["agents", "scaffold", "np_agent", "--target", "apps", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    base = tmp_path / "np_agent"
    for rel in APPS_EXPECTED_FILES:
        assert (base / rel).exists(), f"missing {rel}"
    # The YAML-spec detour must NOT have happened.
    assert not (tmp_path / "np_agent.yaml").exists()
    assert "fill in" not in result.output
    # The unreachable-workspace fallback is announced, not fatal.
    assert "fallback" in result.output


def test_scaffold_explicit_target_non_tty_writes_model_serving_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same routing fix for --target model-serving: the flat layout, exit 0."""
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_discover_default_data", lambda profile: None)
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)

    result = CliRunner().invoke(
        main,
        ["agents", "scaffold", "flat_agent", "--target", "model-serving", "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "flat_agent"
    assert (base / "agent.py").exists()
    assert (base / "app.py").exists()
    assert not (tmp_path / "flat_agent.yaml").exists()


# ---------------------------------------------------------------------------
# _resolve_scaffold_data_source — the extracted resolver (#522). Four branches:
# base (no source), explicit catalog+schema, auto-detect, unreachable fallback.
# ---------------------------------------------------------------------------


def test_resolve_data_source_base_template_has_no_source() -> None:
    import apx_agent.cli as cli

    assert cli._resolve_scaffold_data_source("base", None, None, None) == ("", "", None)


def test_resolve_data_source_explicit_probes_first_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_probe_first_table", lambda c, s, p: "trips")
    assert cli._resolve_scaffold_data_source("data", "main", "sales", None) == (
        "main", "sales", "trips",
    )


def test_probe_first_table_skips_observability_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli

    class Tables:
        @staticmethod
        def list(catalog_name: str, schema_name: str) -> list[Any]:
            assert catalog_name == "main"
            assert schema_name == "sales"
            return [
                type("Table", (), {"name": "apx_agent_events"})(),
                type("Table", (), {"name": "apx_demo_trace_unified"})(),
                type("Table", (), {"name": "customers"})(),
            ]

    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: type("WS", (), {"tables": Tables})())

    assert cli._probe_first_table("main", "sales") == "customers"


def test_resolve_data_source_auto_detects_when_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_discover_default_data", lambda p: ("cat", "sch", "tbl"))
    assert cli._resolve_scaffold_data_source("data", None, None, None) == (
        "cat", "sch", "tbl",
    )


def test_resolve_data_source_falls_back_to_samples_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent.cli as cli

    monkeypatch.setattr(cli, "_discover_default_data", lambda p: None)
    assert cli._resolve_scaffold_data_source("data", None, None, None) == (
        "samples", "nyctaxi", None,
    )


def test_scaffold_apps_ci_none_skips_workflows(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps", "--ci", "none",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"
    assert not (base / ".github").exists()
    assert not (base / ".gitlab-ci.yml").exists()
    assert (base / "tests" / "test_agent_imports.py").exists()


def test_scaffold_apps_ci_gitlab(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "agents", "scaffold", "my_agent", "--target", "apps", "--ci", "gitlab",
            "--dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    base = tmp_path / "my_agent"
    assert (base / ".gitlab-ci.yml").exists()
    assert not (base / ".github").exists()
    assert "bundle-target staging" in (base / ".gitlab-ci.yml").read_text()

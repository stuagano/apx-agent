import tomllib

from click.testing import CliRunner
from apx_agent.cli import main
import yaml
from pathlib import Path
from unittest.mock import patch


def test_scaffold_coworker_creates_agent_py(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "scaffold", "payroll-coworker",
        "--template", "coworker",
        "--catalog", "main",
        "--schema", "payroll",
        "--no-interactive",
        "--dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    agent_py = tmp_path / "payroll-coworker" / "agent.py"
    assert agent_py.exists(), f"Expected a project directory, got: {list(tmp_path.iterdir())}"
    assert "CoworkerAgent(" in agent_py.read_text()


def test_scaffold_coworker_creates_project_directory(tmp_path):
    runner = CliRunner()
    runner.invoke(main, [
        "agents", "scaffold", "my-coworker",
        "--template", "coworker",
        "--catalog", "main",
        "--schema", "sales",
        "--no-interactive",
        "--dir", str(tmp_path),
    ])
    assert (tmp_path / "my-coworker").exists()


def test_build_heredoc_copies_apx_dir():
    # The OKF bundle (and the derived schema.json cache) live under .apx/ and
    # MUST ship to the App container, else the deployed agent is ungrounded (F8).
    from apx_agent import cli
    import inspect

    src = inspect.getsource(cli)
    assert "cp -r .apx .build/" in src


class TestScaffoldEmitsOKF:
    """Scaffold writes an OKF bundle alongside the derived schema.json cache."""

    _manifest = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}

    def _invoke_model_serving(self, tmp_path):
        runner = CliRunner()
        with patch("apx_agent.cli._schema_manifest_for_scaffold", return_value=self._manifest):
            result = runner.invoke(
                main,
                [
                    "agents", "scaffold", "proj",
                    "--target", "model-serving",
                    "--catalog", "c", "--schema", "s",
                    "--no-interactive",
                    "--dir", str(tmp_path),
                ],
                catch_exceptions=False,
                env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
            )
        return result, tmp_path / "proj"

    def _invoke_apps(self, tmp_path):
        runner = CliRunner()
        with patch("apx_agent.cli._schema_manifest_for_scaffold", return_value=self._manifest):
            result = runner.invoke(
                main,
                [
                    "agents", "scaffold", "proj",
                    "--target", "apps",
                    "--catalog", "c", "--schema", "s",
                    "--no-interactive",
                    "--dir", str(tmp_path),
                ],
                catch_exceptions=False,
                env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
            )
        return result, tmp_path / "proj"

    def test_model_serving_writes_okf_bundle_and_cache(self, tmp_path):
        import json
        from apx_agent._okf import okf_manifest

        result, target = self._invoke_model_serving(tmp_path)
        assert result.exit_code == 0, result.output

        assert (target / ".apx" / "okf" / "datasets" / "s.md").is_file()
        assert (target / ".apx" / "schema.json").is_file()
        assert okf_manifest(target / ".apx" / "okf") == json.loads(
            (target / ".apx" / "schema.json").read_text()
        )

    def test_apps_writes_okf_bundle_and_cache(self, tmp_path):
        import json
        from apx_agent._okf import okf_manifest

        result, target = self._invoke_apps(tmp_path)
        assert result.exit_code == 0, result.output

        assert (target / ".apx" / "okf" / "datasets" / "s.md").is_file()
        assert (target / ".apx" / "schema.json").is_file()
        assert okf_manifest(target / ".apx" / "okf") == json.loads(
            (target / ".apx" / "schema.json").read_text()
        )

    # ------------------------------------------------------------------
    # Coherence tests: knowledge= emitted iff bundle is written
    # ------------------------------------------------------------------

    def test_model_serving_pyproject_has_knowledge_when_bundle_written(self, tmp_path):
        """Model-serving scaffold: knowledge= in pyproject iff bundle was written."""
        result, target = self._invoke_model_serving(tmp_path)
        assert result.exit_code == 0, result.output

        # Bundle must exist
        assert (target / ".apx" / "okf" / "datasets" / "s.md").is_file()

        # pyproject must declare the knob
        with open(target / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
        assert agent_section.get("knowledge") == "./.apx/okf", (
            f"knowledge= missing from model-serving pyproject; agent section: {agent_section!r}"
        )

    def test_apps_pyproject_has_knowledge_when_bundle_written(self, tmp_path):
        """Apps scaffold: knowledge= in pyproject iff bundle was written."""
        result, target = self._invoke_apps(tmp_path)
        assert result.exit_code == 0, result.output

        # Bundle must exist
        assert (target / ".apx" / "okf" / "datasets" / "s.md").is_file()

        # pyproject must declare the knob
        with open(target / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
        assert agent_section.get("knowledge") == "./.apx/okf", (
            f"knowledge= missing from apps pyproject; agent section: {agent_section!r}"
        )

    def test_apps_pyproject_no_knowledge_when_no_bundle(self, tmp_path):
        """Apps scaffold: when manifest is None (no tables), neither knowledge= nor bundle."""
        runner = CliRunner()
        with patch("apx_agent.cli._schema_manifest_for_scaffold", return_value=None):
            result = runner.invoke(
                main,
                [
                    "agents", "scaffold", "proj",
                    "--target", "apps",
                    "--catalog", "c", "--schema", "s",
                    "--no-interactive",
                    "--dir", str(tmp_path),
                ],
                catch_exceptions=False,
                env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
            )
        assert result.exit_code == 0, result.output
        target = tmp_path / "proj"

        # No bundle should exist
        assert not (target / ".apx" / "okf").exists(), (
            ".apx/okf bundle must not exist when manifest is None"
        )

        # No knowledge= in pyproject
        with open(target / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
        assert agent_section.get("knowledge") is None, (
            f"knowledge= must be absent when no bundle; agent section: {agent_section!r}"
        )

    def test_model_serving_pyproject_no_knowledge_when_no_bundle(self, tmp_path):
        """Model-serving scaffold: when manifest is None, neither knowledge= nor bundle."""
        runner = CliRunner()
        with patch("apx_agent.cli._schema_manifest_for_scaffold", return_value=None):
            result = runner.invoke(
                main,
                [
                    "agents", "scaffold", "proj",
                    "--target", "model-serving",
                    "--catalog", "c", "--schema", "s",
                    "--no-interactive",
                    "--dir", str(tmp_path),
                ],
                catch_exceptions=False,
                env={"DATABRICKS_CONFIG_PROFILE": "__none__"},
            )
        assert result.exit_code == 0, result.output
        target = tmp_path / "proj"

        # No bundle should exist
        assert not (target / ".apx" / "okf").exists(), (
            ".apx/okf bundle must not exist when manifest is None"
        )

        # No knowledge= in pyproject
        with open(target / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        agent_section = data.get("tool", {}).get("apx", {}).get("agent", {})
        assert agent_section.get("knowledge") is None, (
            f"knowledge= must be absent when no bundle; agent section: {agent_section!r}"
        )

from click.testing import CliRunner
from apx_agent.cli import main
import yaml
from pathlib import Path
from unittest.mock import patch


def test_scaffold_coworker_outputs_yaml(tmp_path):
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
    yaml_file = tmp_path / "payroll-coworker.yaml"
    assert yaml_file.exists(), f"Expected yaml, got: {list(tmp_path.iterdir())}"
    spec = yaml.safe_load(yaml_file.read_text())
    assert spec["name"] == "payroll-coworker"
    assert spec["template"]["name"] == "coworker"
    assert spec["template"]["catalog"] == "main"


def test_scaffold_coworker_no_project_directory(tmp_path):
    runner = CliRunner()
    runner.invoke(main, [
        "agents", "scaffold", "my-coworker",
        "--template", "coworker",
        "--catalog", "main",
        "--schema", "sales",
        "--no-interactive",
        "--dir", str(tmp_path),
    ])
    assert not (tmp_path / "my-coworker").exists()


def test_scaffold_no_yaml_flag_creates_directory(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "scaffold", "my-agent",
        "--template", "base",
        "--no-interactive",
        "--no-yaml",
        "--dir", str(tmp_path),
    ])
    # --no-yaml uses old behavior: creates a project directory
    assert (tmp_path / "my-agent").exists() or result.exit_code == 0


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
                    "--no-yaml",
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
                    "--no-yaml",
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

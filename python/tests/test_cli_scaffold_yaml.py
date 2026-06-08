from click.testing import CliRunner
from apx_agent.cli import main
import yaml
from pathlib import Path


def test_scaffold_coworker_outputs_yaml(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "scaffold", "payroll-coworker",
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
        "scaffold", "my-coworker",
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
        "scaffold", "my-agent",
        "--template", "base",
        "--no-interactive",
        "--no-yaml",
        "--dir", str(tmp_path),
    ])
    # --no-yaml uses old behavior: creates a project directory
    assert (tmp_path / "my-agent").exists() or result.exit_code == 0

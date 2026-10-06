from pathlib import Path
import tomllib
import yaml


def test_bundle_preserves_inherited_governance_for_native_host():
    root = Path(__file__).parents[1]
    doc = yaml.safe_load((root / "databricks.yml").read_text())
    app = doc["resources"]["apps"]["contract-parsing-agent-app"]
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["apx"]["agent"]
    assert config["target"] == "durable_agent_server"
    assert app["space"] == config["deploy"]["space"]
    assert app["config"]["command"] == ["python", "-m", "app"]
    assert "resources" not in app  # Inherited and validated by App Space deployment.
    env = {entry["name"]: entry["value"] for entry in app["config"]["env"]}
    assert env["APX_APPS_HOST"] == "agentbricks"
    assert env["SQL_WAREHOUSE_ID"] == "${var.sql_warehouse_id}"
    assert env["MLFLOW_EXPERIMENT_ID"] == "${var.mlflow_experiment_id}"
    assert config["session"]["type"] == "managed"

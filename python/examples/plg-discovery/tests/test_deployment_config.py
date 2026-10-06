from pathlib import Path
import tomllib
import yaml


def test_bundle_uses_managed_durable_host():
    root = Path(__file__).parents[1]
    config = yaml.safe_load((root / "databricks.yml").read_text())
    app = config["resources"]["apps"]["plg-discovery-app"]
    assert app["config"]["command"] == ["python", "-m", "app"]
    env = {item["name"]: item["value"] for item in app["config"]["env"]}
    assert env["APX_APPS_HOST"] == "agentbricks"
    assert env["MLFLOW_TRACKING_URI"] == "databricks"
    assert "MLFLOW_EXPERIMENT_ID" not in env
    assert "resources" not in app
    declared = tomllib.loads((root / "pyproject.toml").read_text())
    assert declared["tool"]["apx"]["agent"]["target"] == "durable_agent_server"
    assert declared["tool"]["apx"]["agent"]["session"]["type"] == "managed"
    assert "apx-agent[agentbricks]" in declared["project"]["dependencies"]

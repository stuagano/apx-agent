from pathlib import Path

import yaml


def test_bundle_uses_native_appkit_host_and_mlflow_telemetry():
    root = Path(__file__).parents[1]
    config = yaml.safe_load((root / "databricks.yml").read_text())
    app = config["resources"]["apps"]["plg-discovery-app"]

    assert app["config"]["command"] == ["python", "-m", "agent_server.start_host"]
    env = {item["name"]: item["value"] for item in app["config"]["env"]}
    assert env["APX_APPS_HOST"] == "appkit"
    assert env["APX_APPKIT_STATIC_PATH"] == "../client/dist"
    assert env["MLFLOW_TRACKING_URI"] == "databricks"
    assert env["MLFLOW_EXPERIMENT_ID"] == "${var.mlflow_experiment_id}"
    assert env["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "${var.sql_warehouse_id}"
    assert env["MLFLOW_TRACING_DESTINATION"] == "${var.catalog}.${var.schema}"
    assert env["APX_AGENT_MLFLOW_AUTOLOG"] == "1"
    assert "APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK" not in env

    resources = {item["name"]: item for item in app["resources"]}
    assert resources["experiment"]["experiment"]["permission"] == "CAN_MANAGE"
    assert resources["sql-warehouse"]["sql_warehouse"]["permission"] == "CAN_USE"
    assert resources["llm-endpoint"]["serving_endpoint"]["permission"] == "CAN_QUERY"

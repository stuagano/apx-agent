from pathlib import Path

import yaml


def test_bundle_declares_runtime_resources_and_mlflow_tracing():
    config = yaml.safe_load(
        (Path(__file__).parents[1] / "databricks.yml").read_text()
    )
    app = config["resources"]["apps"]["contract-parsing-agent-app"]

    resources = {item["name"]: item for item in app["resources"]}
    assert resources["experiment"]["experiment"]["experiment_id"] == (
        "${var.mlflow_experiment_id}"
    )
    assert resources["sql-warehouse"]["sql_warehouse"]["id"] == (
        "${var.sql_warehouse_id}"
    )
    assert resources["llm-endpoint"]["serving_endpoint"]["name"] == (
        "${var.llm_endpoint_name}"
    )

    env = {item["name"]: item["value"] for item in app["config"]["env"]}
    assert env["MLFLOW_TRACKING_URI"] == "databricks"
    assert env["MLFLOW_EXPERIMENT_ID"] == "${var.mlflow_experiment_id}"
    assert env["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "${var.sql_warehouse_id}"
    assert env["MLFLOW_TRACING_DESTINATION"] == "${var.catalog}.${var.schema}"
    assert env["APX_AGENT_MLFLOW_AUTOLOG"] == "1"

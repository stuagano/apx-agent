"""Opt-in LIVE reality proof: declared data governance → UC ABAC read-back.

Proves the full loop: a ``DataConfig`` declaration compiles to DDL, applies to
a real workspace, and the resulting governed tags / table tags are readable
back via ``INFORMATION_SCHEMA``.

Skips with an explicit UNVERIFIED reason unless the operator provides:

  APX_ABAC_WORKSPACE_HOST   workspace host (https://...)
  APX_ABAC_TOKEN            personal access token with UC DDL + tag read perms
  APX_ABAC_CATALOG          catalog to create the test schema + table in
  APX_ABAC_WAREHOUSE_ID     (optional) SQL warehouse; auto-discovered if unset

The test creates a temporary schema + table, applies a minimal DataConfig,
reads back the tags, then cleans up (drops the table + schema).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("databricks.sdk")


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _required_env() -> dict[str, str]:
    required = (
        "APX_ABAC_WORKSPACE_HOST",
        "APX_ABAC_TOKEN",
        "APX_ABAC_CATALOG",
    )
    values = {name: _env(name) for name in required}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        pytest.skip(
            "APX-DATA-GOV-PROOF UNVERIFIED (not configured): set "
            + ", ".join(missing)
        )
    return {name: value for name, value in values.items() if value is not None}


def test_declared_data_governance_live() -> None:
    env = _required_env()
    host = env["APX_ABAC_WORKSPACE_HOST"]
    token = env["APX_ABAC_TOKEN"]
    catalog = env["APX_ABAC_CATALOG"]
    warehouse_id = _env("APX_ABAC_WAREHOUSE_ID")

    from databricks.sdk import WorkspaceClient

    from apx_agent._data_governance import (
        apply_data_governance,
        compile_data_governance_plan,
    )
    from apx_agent._models import DataConfig, DataTableConfig, DataTagConfig
    from apx_agent._sql import run_sql

    ws = WorkspaceClient(host=host, token=token)
    schema = f"apx_data_gov_test_{os.getpid()}"
    table = f"{catalog}.{schema}.test_table"

    # Setup: create schema + table
    run_sql(ws, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}", warehouse_id=warehouse_id)
    run_sql(
        ws,
        f"CREATE TABLE IF NOT EXISTS {table} (id INT, region STRING)",
        warehouse_id=warehouse_id,
    )

    try:
        # Declare → compile → apply
        config = DataConfig(
            governed_tags=[
                DataTagConfig(name="apx.agent.env", values=["test"]),
            ],
            tables=[
                DataTableConfig(
                    name=table,
                    tags={"apx.agent.env": "test"},
                ),
            ],
        )
        plan = compile_data_governance_plan(config)
        executed = apply_data_governance(plan, ws, warehouse_id=warehouse_id)
        assert len(executed) == 2  # 1 governed tag + 1 table tag

        # Read back: table tags via INFORMATION_SCHEMA
        rows = run_sql(
            ws,
            f"SELECT tag_name, tag_value FROM {catalog}.information_schema.table_tags "
            f"WHERE schema_name = '{schema}' AND table_name = 'test_table'",
            warehouse_id=warehouse_id,
        )
        tag_map = {r["tag_name"]: r["tag_value"] for r in rows}
        assert tag_map.get("apx.agent.env") == "test", (
            f"APX-DATA-GOV-PROOF FAILED: expected tag apx.agent.env=test, got {tag_map}"
        )

        # Read back: governed tag exists
        rows = run_sql(
            ws,
            f"SELECT tag_name FROM {catalog}.information_schema.governed_tags "
            f"WHERE tag_name = 'apx.agent.env'",
            warehouse_id=warehouse_id,
        )
        assert len(rows) >= 1, (
            "APX-DATA-GOV-PROOF FAILED: governed tag apx.agent.env not found in "
            "INFORMATION_SCHEMA.governed_tags"
        )

    finally:
        # Cleanup
        run_sql(ws, f"DROP TABLE IF EXISTS {table}", warehouse_id=warehouse_id)
        run_sql(ws, f"DROP SCHEMA IF EXISTS {catalog}.{schema}", warehouse_id=warehouse_id)

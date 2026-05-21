"""Shared SQL helpers for tool modules. Mirrors the data-triage agent pattern."""

from __future__ import annotations

from typing import Any

from databricks.sdk.service.sql import StatementState


def get_warehouse_id(ws: Any) -> str:
    for wh in ws.warehouses.list():
        if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
            return wh.id or ""
    for wh in ws.warehouses.list():
        if wh.id:
            return wh.id
    raise RuntimeError("No SQL warehouse available")


def run_sql(ws: Any, sql: str) -> list[dict[str, Any]]:
    """Execute a SELECT, return list of dicts."""
    result = ws.statement_execution.execute_statement(
        warehouse_id=get_warehouse_id(ws),
        statement=sql,
        wait_timeout="30s",
    )
    status = result.status
    if status is None or status.state != StatementState.SUCCEEDED:
        error_msg = status.error if status else "unknown error"
        raise RuntimeError(f"Query failed: {error_msg}")
    if not result.manifest or not result.manifest.schema:
        return []
    cols = [c.name for c in (result.manifest.schema.columns or [])]
    rows = result.result.data_array or [] if result.result else []
    return [dict(zip(cols, r)) for r in rows]

"""SQL execution utilities — warehouse discovery and statement execution.

Eliminates the most common boilerplate in agent tool functions:

    from apx_agent import run_sql, Dependencies

    def get_customers(region: str, ws: Dependencies.Workspace) -> list[dict]:
        \"\"\"List customers by region.\"\"\"
        return run_sql(
            ws,
            "SELECT * FROM customers WHERE region = :region",
            parameters=[{"name": "region", "value": region, "type": "STRING"}],
        )

Or via dependency injection (no explicit ws needed):

    from apx_agent import Dependencies

    def get_customers(region: str, sql: Dependencies.Sql) -> list[dict]:
        \"\"\"List customers by region.\"\"\"
        return sql(
            "SELECT * FROM customers WHERE region = :region",
            parameters=[{"name": "region", "value": region, "type": "STRING"}],
        )

.. warning::

    Avoid interpolating user input directly into SQL strings. Use the
    ``parameters`` argument (Databricks SQL bind parameters) whenever possible.
    If you must interpolate, always escape with ``s.replace("'", "''")`` and
    validate the value against an allowlist.
"""

from __future__ import annotations

import logging
from typing import Any

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def get_warehouse_id(ws: WorkspaceClient, *, prefer_serverless: bool = True) -> str:
    """Find a usable SQL warehouse ID, preferring serverless.

    Raises ``RuntimeError`` if no warehouse is available.
    """
    fallback: str | None = None
    for wh in ws.warehouses.list():
        if not wh.id:
            continue
        if prefer_serverless and wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
            return wh.id
        if fallback is None:
            fallback = wh.id
    if fallback is not None:
        return fallback
    raise RuntimeError("No SQL warehouse available in this workspace")


def run_sql(
    ws: WorkspaceClient,
    sql: str,
    *,
    warehouse_id: str | None = None,
    parameters: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Execute a SQL statement and return rows as list of dicts.

    If ``warehouse_id`` is not provided, auto-discovers one via
    ``get_warehouse_id()``.

    ``parameters`` accepts Databricks SQL bind parameters, e.g.::

        run_sql(ws, "SELECT * FROM t WHERE id = :id",
                parameters=[{"name": "id", "value": "42", "type": "STRING"}])

    Returns an empty list for statements with no result set (DDL, etc.).
    Raises ``RuntimeError`` on query failure.
    """
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem

    wh_id = warehouse_id or get_warehouse_id(ws)
    params = None
    if parameters:
        params = [
            StatementParameterListItem(name=p["name"], value=p["value"], type=p.get("type"))
            for p in parameters
        ]
    result = ws.statement_execution.execute_statement(
        warehouse_id=wh_id,
        statement=sql,
        parameters=params,
        wait_timeout="30s",
    )
    status = result.status
    if status is None or status.state != StatementState.SUCCEEDED:
        error_msg = getattr(status, "error", None) if status else None
        raise RuntimeError(f"Query failed: {error_msg or 'unknown error'}")
    if not result.manifest or not result.manifest.schema:
        return []
    cols = [c.name or "" for c in (result.manifest.schema.columns or [])]
    rows = result.result.data_array or [] if result.result else []
    return [{c: v for c, v in zip(cols, row)} for row in rows]

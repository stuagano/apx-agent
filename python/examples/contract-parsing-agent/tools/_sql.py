"""Shared SQL helpers for tool modules. Mirrors the data-triage agent pattern.

Warehouse selection and query execution are delegated to databricks-tools-core.
The core functions accept the caller's OBO WorkspaceClient (``ws``) so queries
run as the end user under Unity Catalog governance, exactly as before.
"""

from __future__ import annotations

from typing import Any

from apx_agent import ToolError
from databricks_tools_core.sql import SQLExecutionError, execute_sql


def run_sql(ws: Any, sql: str) -> list[dict[str, Any]]:
    """Execute a SELECT, return list of dicts.

    Delegates to databricks_tools_core.execute_sql, passing the OBO client so
    the query runs as the end user. A failed query is raised as ToolError so
    the runtime contains it as a legible finding instead of a pipeline-fatal
    500 (#562); every tool built on this helper inherits that containment.
    """
    try:
        return execute_sql(sql, client=ws, timeout=30)
    except SQLExecutionError as e:
        raise ToolError(f"Query failed: {e}") from e

"""Shared SQL helpers for tool modules. Mirrors the data-triage agent pattern.

Warehouse selection and query execution are delegated to databricks-tools-core.
The core functions accept the caller's OBO WorkspaceClient (``ws``) so queries
run as the end user under Unity Catalog governance, exactly as before.
"""

from __future__ import annotations

from typing import Any

from databricks_tools_core.sql import SQLExecutionError, execute_sql


def run_sql(ws: Any, sql: str) -> list[dict[str, Any]]:
    """Execute a SELECT, return list of dicts.

    Delegates to databricks_tools_core.execute_sql, passing the OBO client so
    the query runs as the end user. The RuntimeError("Query failed: ...")
    contract is preserved for callers/agents that surface it.
    """
    try:
        return execute_sql(sql, client=ws, timeout=30)
    except SQLExecutionError as e:
        raise RuntimeError(f"Query failed: {e}")

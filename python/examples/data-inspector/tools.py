"""Data Inspector tool functions.

General-purpose Delta table forensics and UC discovery tools. Reusable by
any agent via A2A (sub_agents URL) or MCP (/mcp/sse).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from databricks.sdk.service.sql import StatementState

from apx_agent import Dependencies
from databricks_tools_core.sql import sql_literal

Workspace = Dependencies.Workspace
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_warehouse_id(ws: Any) -> str:
    for wh in ws.warehouses.list():
        if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
            return wh.id or ""
    for wh in ws.warehouses.list():
        if wh.id:
            return wh.id
    raise RuntimeError("No SQL warehouse available")


def _run_sql(ws: Any, sql: str) -> list[dict[str, Any]]:
    result = ws.statement_execution.execute_statement(
        warehouse_id=_get_warehouse_id(ws),
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


def _get_current_version(ws: Any, table: str) -> int:
    rows = _run_sql(ws, f"DESCRIBE HISTORY {table} LIMIT 1")
    if not rows:
        raise RuntimeError(f"No history found for {table}")
    return int(rows[0]["version"])


# ---------------------------------------------------------------------------
# Data presence tools
# ---------------------------------------------------------------------------

def run_sql_query(sql: str, ws: Workspace) -> dict[str, Any]:
    """Execute a read-only SQL query against Databricks.
    Use to check if data exists, count rows, inspect recent records, or
    verify field values. Results are capped at 50 rows."""
    rows = _run_sql(ws, sql)
    return {"row_count": len(rows), "rows": rows[:50]}


def get_table_info(table_full_name: str, ws: Workspace) -> dict[str, Any]:
    """Get schema, row count, and data freshness for a Unity Catalog table.
    Use this first to confirm the table exists and understand its structure.
    table_full_name format: catalog.schema.table"""
    table = ws.tables.get(table_full_name)
    columns = [
        {"name": c.name, "type": str(c.type_name)}
        for c in (table.columns or [])
    ]
    try:
        stats = _run_sql(ws, f"SELECT COUNT(*) AS row_count FROM {table_full_name}")
        row_count = stats[0]["row_count"] if stats else "unknown"
    except Exception:
        row_count = "unknown"
    return {
        "table": table_full_name,
        "catalog": table.catalog_name,
        "schema": table.schema_name,
        "columns": columns,
        "row_count": row_count,
        "comment": table.comment,
    }


# ---------------------------------------------------------------------------
# Delta forensics tools
# ---------------------------------------------------------------------------

def delta_bisect(
    table: str,
    condition: str,
    version_lo: int = -1,
    version_hi: int = -1,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Binary search across Delta table versions to find when a row appeared
    or disappeared. Pinpoints the exact version where a condition changed.

    table: fully qualified table name (catalog.schema.table)
    condition: WHERE clause that matches the row(s) of interest
               (e.g. "account_id = '12345'")
    version_lo: start of search range (default: 0)
    version_hi: end of search range (default: current version)

    Returns the transition version — the first version where the condition
    result differs from version_lo. Also returns the DESCRIBE HISTORY entry
    for that version so you can see who/what changed it."""
    current = _get_current_version(ws, table)

    lo = version_lo if version_lo >= 0 else 0
    hi = version_hi if version_hi >= 0 else current

    if hi > current:
        hi = current

    def _check(v: int) -> bool:
        rows = _run_sql(ws, f"SELECT 1 FROM {table} VERSION AS OF {v} WHERE {condition} LIMIT 1")
        return len(rows) > 0

    present_at_lo = _check(lo)
    present_at_hi = _check(hi)

    if present_at_lo == present_at_hi:
        return {
            "table": table,
            "condition": condition,
            "version_lo": lo,
            "version_hi": hi,
            "present_at_lo": present_at_lo,
            "present_at_hi": present_at_hi,
            "result": "no_change",
            "message": f"Condition is {'true' if present_at_lo else 'false'} at both v{lo} and v{hi}. "
                       "Try widening the range or adjusting the condition.",
        }

    steps = 0
    max_steps = int(math.log2(hi - lo + 1)) + 2
    while lo < hi and steps < max_steps:
        mid = (lo + hi) // 2
        if _check(mid) == present_at_lo:
            lo = mid + 1
        else:
            hi = mid
        steps += 1

    transition_version = hi

    history = _run_sql(ws, f"DESCRIBE HISTORY {table} LIMIT {transition_version + 1}")
    version_info = None
    for row in history:
        if int(row.get("version", -1)) == transition_version:
            version_info = row
            break

    return {
        "table": table,
        "condition": condition,
        "transition_version": transition_version,
        "present_before": present_at_lo if version_lo >= 0 else _check(max(transition_version - 1, 0)),
        "present_after": not present_at_lo if version_lo >= 0 else _check(transition_version),
        "bisect_steps": steps,
        "version_info": version_info,
    }


def delta_bisect_column(
    table: str,
    key_condition: str,
    column: str,
    expected_value: str,
    version_lo: int = -1,
    version_hi: int = -1,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Binary search for when a column value changed on a specific row.
    Use when the row still exists but a field value flipped — e.g. a status
    changed from 'active' to 'inactive', or a flag went from true to false.

    table: fully qualified table name (catalog.schema.table)
    key_condition: WHERE clause that identifies the row
                   (e.g. "account_id = '12345'")
    column: the column to track (e.g. "dr_enrolled")
    expected_value: the 'good' value as a string (e.g. "true", "active")
    version_lo: start of search range (default: 0)
    version_hi: end of search range (default: current version)

    Returns the transition version, the before/after column values, and
    the DESCRIBE HISTORY entry for that version."""
    current = _get_current_version(ws, table)

    lo = version_lo if version_lo >= 0 else 0
    hi = version_hi if version_hi >= 0 else current

    if hi > current:
        hi = current

    def _read_value(v: int) -> str | None:
        rows = _run_sql(
            ws,
            f"SELECT CAST({column} AS STRING) AS val "
            f"FROM {table} VERSION AS OF {v} "
            f"WHERE {key_condition} LIMIT 1",
        )
        if not rows:
            return None
        return rows[0].get("val")

    val_at_lo = _read_value(lo)
    val_at_hi = _read_value(hi)
    matches_lo = (val_at_lo == expected_value)
    matches_hi = (val_at_hi == expected_value)

    if matches_lo == matches_hi:
        return {
            "table": table,
            "key_condition": key_condition,
            "column": column,
            "expected_value": expected_value,
            "version_lo": lo,
            "version_hi": hi,
            "value_at_lo": val_at_lo,
            "value_at_hi": val_at_hi,
            "result": "no_change",
            "message": f"Column '{column}' is '{val_at_lo}' at v{lo} and '{val_at_hi}' at v{hi}. "
                       f"Both {'match' if matches_lo else 'differ from'} expected '{expected_value}'. "
                       "Try widening the range.",
        }

    steps = 0
    max_steps = int(math.log2(hi - lo + 1)) + 2
    while lo < hi and steps < max_steps:
        mid = (lo + hi) // 2
        val_at_mid = _read_value(mid)
        if (val_at_mid == expected_value) == matches_lo:
            lo = mid + 1
        else:
            hi = mid
        steps += 1

    transition_version = hi
    value_before = _read_value(max(transition_version - 1, 0))
    value_after = _read_value(transition_version)

    history = _run_sql(ws, f"DESCRIBE HISTORY {table} LIMIT {transition_version + 1}")
    version_info = None
    for row in history:
        if int(row.get("version", -1)) == transition_version:
            version_info = row
            break

    return {
        "table": table,
        "key_condition": key_condition,
        "column": column,
        "expected_value": expected_value,
        "transition_version": transition_version,
        "value_before": value_before,
        "value_after": value_after,
        "bisect_steps": steps,
        "version_info": version_info,
    }


def version_diff(table: str, v_old: int, v_new: int, ws: Workspace) -> dict[str, Any]:
    """Compare two Delta table versions to see what rows changed.
    Returns rows added in v_new (not in v_old) and rows removed (in v_old
    but not v_new). Capped at 20 rows each direction.

    table: fully qualified table name
    v_old: the 'before' version number
    v_new: the 'after' version number"""
    added = _run_sql(ws, f"""
        SELECT * FROM {table} VERSION AS OF {v_new}
        EXCEPT
        SELECT * FROM {table} VERSION AS OF {v_old}
        LIMIT 20
    """)
    removed = _run_sql(ws, f"""
        SELECT * FROM {table} VERSION AS OF {v_old}
        EXCEPT
        SELECT * FROM {table} VERSION AS OF {v_new}
        LIMIT 20
    """)
    return {
        "table": table,
        "v_old": v_old,
        "v_new": v_new,
        "rows_added": added,
        "rows_removed": removed,
        "added_count": len(added),
        "removed_count": len(removed),
    }


def audit_lookup(table: str, version: int = -1, ws: Workspace = None) -> dict[str, Any]:
    """Get DESCRIBE HISTORY metadata for a Delta table — who changed it,
    when, what operation, and operation parameters.

    table: fully qualified table name
    version: specific version to look up (-1 for the 10 most recent versions)"""
    if version >= 0:
        rows = _run_sql(ws, f"DESCRIBE HISTORY {table} LIMIT {version + 1}")
        entry = None
        for row in rows:
            if int(row.get("version", -1)) == version:
                entry = row
                break
        if not entry:
            return {"table": table, "version": version, "error": f"Version {version} not found in history"}
        return {"table": table, "version": version, "history": entry}
    rows = _run_sql(ws, f"DESCRIBE HISTORY {table} LIMIT 10")
    return {"table": table, "recent_history": rows}


# ---------------------------------------------------------------------------
# Discovery tools — "what data is here?"
# ---------------------------------------------------------------------------

def list_catalogs(ws: Workspace) -> dict[str, Any]:
    """List all Unity Catalog catalogs accessible to the caller.
    Use this when the user doesn't know what catalogs exist."""
    catalogs = []
    for c in ws.catalogs.list():
        catalogs.append({
            "name": c.name,
            "comment": c.comment,
            "owner": c.owner,
        })
    return {"catalogs": catalogs, "count": len(catalogs)}


def list_schemas(catalog: str, ws: Workspace) -> dict[str, Any]:
    """List schemas in a Unity Catalog catalog.
    Use after list_catalogs to drill into a specific catalog.
    catalog: catalog name (e.g. 'main', 'hive_metastore')."""
    schemas = []
    for s in ws.schemas.list(catalog_name=catalog):
        schemas.append({
            "name": s.name,
            "full_name": s.full_name,
            "comment": s.comment,
        })
    return {"catalog": catalog, "schemas": schemas, "count": len(schemas)}


def list_tables(catalog: str, schema: str, ws: Workspace) -> dict[str, Any]:
    """List tables in a Unity Catalog schema.
    catalog: catalog name. schema: schema name within that catalog."""
    tables = []
    for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
        tables.append({
            "name": t.name,
            "full_name": t.full_name,
            "table_type": str(t.table_type) if t.table_type else None,
            "comment": t.comment,
        })
    return {"catalog": catalog, "schema": schema, "tables": tables, "count": len(tables)}


def search_tables(query: str, ws: Workspace) -> dict[str, Any]:
    """Find tables whose name or comment matches a substring across all
    catalogs/schemas the caller can see. Use this when the user describes
    what they want ('billing', 'customers', 'usage') but doesn't know the
    catalog/schema.
    query: substring to match against table names and comments (case-insensitive)."""
    safe_query = sql_literal(query)
    sql = f"""
        SELECT table_catalog, table_schema, table_name, table_type, comment
        FROM system.information_schema.tables
        WHERE LOWER(table_name) LIKE LOWER('%{safe_query}%')
           OR LOWER(coalesce(comment, '')) LIKE LOWER('%{safe_query}%')
        ORDER BY table_catalog, table_schema, table_name
        LIMIT 50
    """
    rows = _run_sql(ws, sql)
    return {"query": query, "matches": rows, "count": len(rows)}

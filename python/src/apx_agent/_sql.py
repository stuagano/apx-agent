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
    If you must interpolate, always escape with ``sql_str_literal(value)``
    (which handles backslashes and quotes) and validate against an allowlist.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementResponse

from ._mlflow_tracing import emit_progress, safe_span

logger = logging.getLogger(__name__)


def sql_escape(value: str) -> str:
    """Escape a string for inline SQL (no wrapping quotes).

    Escapes backslash **first**, then single quotes. Spark/Databricks SQL treats
    backslash as a live escape character inside a string literal, so a value
    ending in ``\\`` would otherwise escape the closing quote and break out of
    the literal (SQL injection). Quote-only doubling is not sufficient.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def sql_str_literal(value: str) -> str:
    """Render a Python string as a safe single-quoted SQL literal.

    Prefer bind parameters where the driver supports them; use this only when
    a value must be interpolated directly (e.g. identifiers, or the SQL
    Statements API where binds aren't available).
    """
    return "'" + sql_escape(value) + "'"


def decode_statement(
    response: StatementResponse | None,
    *,
    ws: WorkspaceClient | None = None,
) -> list[dict[str, Any]]:
    """Decode a Databricks ``StatementResponse`` into a list of row dicts.

    Shared by ``run_sql`` and any tool that surfaces results as
    ``StatementResponse`` — notably the Genie attachment query-result API
    (``ws.genie.get_message_query_result_by_attachment(...).statement_response``),
    which returns the same type as direct statement execution.

    INLINE results larger than one chunk are split across chunks linked by
    ``result.next_chunk_index``; ``execute_statement`` returns only the first.
    Pass ``ws`` to follow ``next_chunk_index`` and fetch the remaining chunks
    (via ``get_statement_result_chunk_n``) so large result sets aren't silently
    truncated. Without ``ws`` only the first chunk is decoded — correct for
    callers whose results fit one chunk (e.g. the Genie attachment path).

    Returns an empty list for statements with no result set or no rows.
    """
    if response is None or response.manifest is None or response.manifest.schema is None:
        return []
    cols = [c.name or "" for c in (response.manifest.schema.columns or [])]
    rows = list(response.result.data_array or []) if response.result else []
    if ws is not None and response.result is not None and response.statement_id is not None:
        next_idx = response.result.next_chunk_index
        while next_idx is not None:
            chunk = ws.statement_execution.get_statement_result_chunk_n(
                response.statement_id, next_idx
            )
            rows.extend(chunk.data_array or [])
            next_idx = chunk.next_chunk_index
    return [{c: v for c, v in zip(cols, row)} for row in rows]


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
    workspace_url = getattr(getattr(ws, "config", None), "host", None) or "<your-workspace>"
    raise RuntimeError(
        f"No SQL warehouse available in this workspace. "
        f"Fix: go to {workspace_url}/sql/warehouses → Create Warehouse (choose Serverless). "
        f"Then set warehouse_id in your agent config or re-deploy."
    )


def _ensure_warehouse_running(
    ws: WorkspaceClient, warehouse_id: str, *, timeout_s: int = 60
) -> None:
    """Best-effort: kick off the warehouse if it's STOPPED and wait until RUNNING.

    Serverless warehouses auto-stop after their idle window; the next query
    against a stopped warehouse would otherwise queue inside ``execute_statement``
    and return with a non-SUCCEEDED status after ``wait_timeout`` — surfacing
    as "Query failed" with no signal *why*. This converts the cold-start into a
    pre-warm with a logged status so the cold-start is visible, not silent.
    Idempotent: a RUNNING warehouse is a no-op. Failures are logged + swallowed
    so we never block the query on the warmup check itself.
    """
    try:
        from databricks.sdk.service.sql import State
    except Exception:
        return  # SDK without State enum — skip the warmup check.

    try:
        wh = ws.warehouses.get(warehouse_id)
    except Exception as exc:
        logger.debug("warehouse warmup check failed (%s); proceeding to execute", exc)
        return
    if wh.state == State.RUNNING:
        return

    # Wrap the start+wait in a child span so the cold-start is a visible, timed
    # step in the trace (otherwise the trace goes silent for ~20-30s).
    with safe_span(
        "SQL warehouse cold-start",
        attributes={"warehouse_id": warehouse_id},
    ):
        logger.warning(
            "SQL warehouse %s is %s — starting it. Serverless cold-start "
            "typically takes 20-30s; the query will run once it's warm.",
            warehouse_id, wh.state,
        )
        emit_progress(
            "Starting SQL warehouse — serverless cold-start, ~20-30s",
            warehouse_id=warehouse_id,
        )
        try:
            ws.warehouses.start(warehouse_id)
        except Exception as exc:
            # Start can race (already starting) or fail; either way we'll still
            # try execute_statement below.
            logger.debug("warehouses.start raised (continuing): %s", exc)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                wh = ws.warehouses.get(warehouse_id)
            except Exception:
                continue
            if wh.state == State.RUNNING:
                logger.info("warehouse %s is now RUNNING", warehouse_id)
                return
        logger.warning(
            "warehouse %s did not reach RUNNING in %ds; proceeding anyway "
            "(execute_statement will queue)",
            warehouse_id, timeout_s,
        )


def run_sql(
    ws: WorkspaceClient,
    sql: str,
    *,
    warehouse_id: str | None = None,
    parameters: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Execute a SQL statement and return rows as list of dicts.

    If ``warehouse_id`` is not provided, auto-discovers one via
    ``get_warehouse_id()``. If the resolved warehouse is stopped, it's started
    + polled to RUNNING first (with a logged status) so the cold-start is
    visible rather than hanging silently in ``execute_statement``.

    ``parameters`` accepts Databricks SQL bind parameters, e.g.::

        run_sql(ws, "SELECT * FROM t WHERE id = :id",
                parameters=[{"name": "id", "value": "42", "type": "STRING"}])

    Large INLINE result sets span multiple chunks; this follows
    ``next_chunk_index`` to return every row, not just the first chunk.

    Returns an empty list for statements with no result set (DDL, etc.).
    Raises ``RuntimeError`` on query failure.
    """
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem

    wh_id = warehouse_id or get_warehouse_id(ws)
    _ensure_warehouse_running(ws, wh_id)
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
    return decode_statement(result, ws=ws)

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
# Canonical inline-SQL escapers live in the dependency-free _sql_escape module
# (so SDK-light callers can import them without pulling databricks.sdk).
# Re-exported here for back-compat with `from ._sql import sql_escape`.
from ._sql_escape import sql_escape, sql_str_literal  # noqa: F401

logger = logging.getLogger(__name__)


def _reauthorize_hint(exc: Exception) -> RuntimeError | None:
    """Translate an OBO scope-denial 403 into a re-authorize instruction.

    Databricks Apps OBO tokens carry only the scopes the user consented to.
    When a bundle *adds* a ``user_api_scopes`` entry (e.g. ``sql``) but the
    caller's token predates that grant, the SQL APIs 403 with
    ``Invalid scope, required scopes: sql`` — even though the bundle declares
    it. The fix is operational, not code: the user must re-authorize the app
    (revisit its consent screen) to mint a token carrying the new scope. A raw
    ``PermissionDenied`` gives no hint of that, so we re-raise with guidance.

    Returns the actionable error to raise, or ``None`` if ``exc`` isn't a
    scope-denial (caller re-raises the original).
    """
    msg = str(exc)
    if "required scopes" not in msg and "Invalid scope" not in msg:
        return None
    return RuntimeError(
        f"{msg}\n\n"
        "This is a stale OBO consent, not a missing bundle scope: the app's "
        "forwarded-user token predates a newly-added user_api_scope. "
        "Re-authorize the app (open its URL in a browser and approve the "
        "permissions prompt) to mint a token that carries the new scope, "
        "then retry."
    )


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


# execute_statement's wait_timeout maxes out at 30s (the API's own cap) - a
# genuinely slow query (a heavy join, not just a cold warehouse - that's
# _ensure_warehouse_running's job) can easily exceed that and come back
# PENDING/RUNNING with no error. These bound how long run_sql polls
# get_statement() past that initial 30s before giving up.
_STATEMENT_POLL_TIMEOUT_S = 120
_STATEMENT_POLL_INTERVAL_S = 2


def _await_statement_completion(
    ws: WorkspaceClient,
    result: StatementResponse,
    *,
    timeout_s: float = _STATEMENT_POLL_TIMEOUT_S,
    poll_interval_s: float = _STATEMENT_POLL_INTERVAL_S,
) -> StatementResponse:
    """Poll a still-PENDING/RUNNING statement to a terminal state.

    A no-op if ``result`` is already terminal (SUCCEEDED/FAILED/CANCELED/
    CLOSED) or has no ``statement_id`` to poll. Returns the last-seen
    ``StatementResponse`` regardless of whether it reached a terminal state
    within ``timeout_s`` — the caller decides what a still-PENDING/RUNNING
    result after the budget means.
    """
    from databricks.sdk.service.sql import StatementState

    in_progress = (StatementState.PENDING, StatementState.RUNNING)
    status = result.status
    if status is None or status.state not in in_progress or not result.statement_id:
        return result

    statement_id = result.statement_id
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)
        result = ws.statement_execution.get_statement(statement_id)
        status = result.status
        if status is None or status.state not in in_progress:
            return result
    return result


def run_sql(
    ws: WorkspaceClient,
    sql: str,
    *,
    warehouse_id: str | None = None,
    parameters: list[dict[str, str]] | None = None,
    poll_timeout_s: float = _STATEMENT_POLL_TIMEOUT_S,
    poll_interval_s: float = _STATEMENT_POLL_INTERVAL_S,
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

    A query that doesn't finish within ``execute_statement``'s 30s
    synchronous wait is polled via ``get_statement()`` for up to
    ``poll_timeout_s`` more seconds (default 120s) before being treated as
    unresolved — a slow-but-successful query returns its rows instead of a
    misleading "unknown error" (the statement's own ``status.error`` is
    ``None`` while it's merely still running, not failed).

    Returns an empty list for statements with no result set (DDL, etc.).
    Raises ``RuntimeError`` on query failure, or if the query is still
    PENDING/RUNNING after the full poll budget (a distinct message from an
    actual failure, since nothing failed - the query may still complete in
    the background).
    """
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem

    try:
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
    except Exception as e:
        hint = _reauthorize_hint(e)
        if hint is not None:
            raise hint from e
        raise
    result = _await_statement_completion(
        ws, result, timeout_s=poll_timeout_s, poll_interval_s=poll_interval_s,
    )
    status = result.status
    if status is not None and status.state == StatementState.SUCCEEDED:
        return decode_statement(result, ws=ws)
    if status is not None and status.state in (StatementState.PENDING, StatementState.RUNNING):
        raise RuntimeError(
            f"Query still running after the {poll_timeout_s}s poll budget "
            f"(statement_id={result.statement_id}) - it has not failed, it "
            f"may complete in the background. Check Query History in the "
            f"workspace, or simplify the query (fewer joins, add filters) "
            f"and retry."
        )
    error_msg = getattr(status, "error", None) if status else None
    raise RuntimeError(f"Query failed: {error_msg or 'unknown error'}")

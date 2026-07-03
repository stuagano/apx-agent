"""Shared Databricks SQL executor for the workflow stores — polls to completion.

``statement_execution.execute_statement`` returns after ``wait_timeout`` even if
the statement is still PENDING/RUNNING. Treating only FAILED as an error (and
returning ``[]`` otherwise) silently loses slow reads and reports slow writes as
success (#372). This polls ``get_statement`` until the statement reaches a
terminal state, raises on any non-SUCCEEDED terminal state, and only returns
``[]`` for a genuinely empty SUCCEEDED result.
"""

from __future__ import annotations

import time
from typing import Any

_POLL_INTERVAL_S = 2.0
# ponytail: 5-minute cap on a single workflow statement; bump if batches run longer.
_POLL_TIMEOUT_S = 300.0


def execute_and_poll(
    ws: Any,
    warehouse_id: str,
    sql: str,
    *,
    poll_timeout_s: float = _POLL_TIMEOUT_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> list[dict]:
    """Execute ``sql`` and poll until it reaches a terminal state; return rows."""
    from databricks.sdk.service.sql import StatementState

    resp = ws.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql.strip(),
        wait_timeout="50s",
    )
    assert resp.status is not None  # execute_statement always populates status

    deadline = time.monotonic() + poll_timeout_s
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Databricks SQL did not finish within {poll_timeout_s:.0f}s "
                f"(state={resp.status.state}); SQL: {sql[:200]}"
            )
        time.sleep(poll_interval_s)
        assert resp.statement_id is not None
        resp = ws.statement_execution.get_statement(resp.statement_id)
        assert resp.status is not None

    if resp.status.state != StatementState.SUCCEEDED:
        err = resp.status.error
        code = err.error_code if err else None
        message = err.message if err else None
        raise RuntimeError(
            f"Databricks SQL {resp.status.state} [{code}]: {message}\nSQL: {sql[:200]}"
        )

    if not resp.result or not resp.result.data_array:
        return []
    assert resp.manifest is not None and resp.manifest.schema is not None
    cols = [c.name for c in (resp.manifest.schema.columns or [])]
    return [dict(zip(cols, row)) for row in resp.result.data_array]

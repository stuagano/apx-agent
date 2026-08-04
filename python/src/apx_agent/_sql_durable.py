"""Restart-safe Databricks SQL statement execution."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    StatementParameterListItem,
    StatementResponse,
    StatementState,
)

from ._mlflow_tracing import emit_progress
from ._sql import (
    _ensure_warehouse_running,
    _reauthorize_hint,
    decode_statement,
    get_warehouse_id,
)

ProgressCallback = Callable[[str, float], None]

_POLL_TIMEOUT_S = 120
_POLL_INTERVAL_S = 2


def _checkpoint_key(
    sql: str,
    warehouse_id: str,
    parameters: list[dict[str, str]] | None,
) -> str:
    payload = json.dumps(
        {
            "parameters": parameters or [],
            "sql": sql,
            "warehouse_id": warehouse_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"SQL checkpoint is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"SQL checkpoint has invalid content: {path}")
    return value


def _write_checkpoint(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    temporary.replace(path)


def _notify(
    callback: ProgressCallback | None,
    state: str,
    started_at: float,
    statement_id: str | None,
) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    if callback is not None:
        callback(state, elapsed)
    emit_progress(
        f"SQL statement {state.lower()} ({elapsed:.0f}s)",
        statement_id=statement_id,
        state=state,
        elapsed_s=elapsed,
    )


def _state_name(response: StatementResponse) -> str:
    status = response.status
    state = status.state if status is not None else None
    return state.value if state is not None else "UNKNOWN"


def run_sql_durable(
    ws: WorkspaceClient,
    sql: str,
    *,
    checkpoint_dir: str | Path,
    warehouse_id: str | None = None,
    parameters: list[dict[str, str]] | None = None,
    poll_timeout_s: float = _POLL_TIMEOUT_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Execute SQL with a disk checkpoint that survives process restarts.

    The checkpoint stores the Databricks ``statement_id`` immediately after
    submission. A later process reattaches with ``get_statement`` rather than
    submitting duplicate work. Successful decoded rows are cached under a key
    derived from SQL, warehouse, and bind parameters. Use a run-scoped
    ``checkpoint_dir`` when the same query must be refreshed; successful
    results in a reused directory remain cached.
    """
    started_at = time.monotonic()
    try:
        resolved_warehouse_id = warehouse_id or get_warehouse_id(ws)
        key = _checkpoint_key(sql, resolved_warehouse_id, parameters)
        checkpoint_path = Path(checkpoint_dir) / f"{key}.json"
        checkpoint = _read_checkpoint(checkpoint_path)

        if checkpoint is not None and checkpoint.get("state") == "SUCCEEDED":
            rows = checkpoint.get("rows")
            if not isinstance(rows, list):
                raise RuntimeError(f"SQL checkpoint has invalid rows: {checkpoint_path}")
            _notify(on_progress, "CACHED", started_at, checkpoint.get("statement_id"))
            return rows

        _ensure_warehouse_running(ws, resolved_warehouse_id)
        checkpoint_statement_id = (
            checkpoint.get("statement_id") if checkpoint is not None else None
        )
        if isinstance(checkpoint_statement_id, str) and checkpoint_statement_id:
            statement_id = checkpoint_statement_id
            response = ws.statement_execution.get_statement(statement_id)
        else:
            bound_parameters = None
            if parameters:
                bound_parameters = [
                    StatementParameterListItem(
                        name=parameter["name"],
                        value=parameter["value"],
                        type=parameter.get("type"),
                    )
                    for parameter in parameters
                ]
            response = ws.statement_execution.execute_statement(
                warehouse_id=resolved_warehouse_id,
                statement=sql,
                parameters=bound_parameters,
                wait_timeout="30s",
            )
            if response.statement_id is None:
                raise RuntimeError("Databricks SQL response has no statement_id")
            statement_id = response.statement_id
            _write_checkpoint(
                checkpoint_path,
                {
                    "state": "SUBMITTED",
                    "statement_id": statement_id,
                    "updated_at": time.time(),
                },
            )
            _notify(on_progress, "SUBMITTED", started_at, statement_id)
    except Exception as exc:
        hint = _reauthorize_hint(exc)
        if hint is not None:
            raise hint from exc
        raise

    in_progress = (StatementState.PENDING, StatementState.RUNNING)
    status = response.status
    state = status.state if status is not None else None
    if state in in_progress:
        state_name = _state_name(response)
        _write_checkpoint(
            checkpoint_path,
            {
                "state": state_name,
                "statement_id": statement_id,
                "updated_at": time.time(),
            },
        )
        _notify(on_progress, state_name, started_at, statement_id)
        deadline = time.monotonic() + poll_timeout_s
        while time.monotonic() < deadline:
            if poll_interval_s > 0:
                time.sleep(poll_interval_s)
            response = ws.statement_execution.get_statement(statement_id)
            state_name = _state_name(response)
            _write_checkpoint(
                checkpoint_path,
                {
                    "state": state_name,
                    "statement_id": statement_id,
                    "updated_at": time.time(),
                },
            )
            _notify(on_progress, state_name, started_at, statement_id)
            status = response.status
            state = status.state if status is not None else None
            if state not in in_progress:
                break

    if state == StatementState.SUCCEEDED:
        rows = decode_statement(response, ws=ws)
        _write_checkpoint(
            checkpoint_path,
            {
                "rows": rows,
                "state": "SUCCEEDED",
                "statement_id": statement_id,
                "updated_at": time.time(),
            },
        )
        return rows
    if state in in_progress:
        raise RuntimeError(
            f"Query still running after the {poll_timeout_s}s poll budget "
            f"(statement_id={statement_id}); restart with the same checkpoint_dir "
            "to resume it without resubmitting."
        )

    checkpoint_path.unlink(missing_ok=True)
    error = status.error if status is not None else None
    raise RuntimeError(f"Query failed: {error or 'unknown error'}")

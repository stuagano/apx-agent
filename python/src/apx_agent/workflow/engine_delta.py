"""
DeltaEngine — durable WorkflowEngine backed by Delta tables via the
Databricks SQL Statements API.

Stores run metadata in `{table_prefix}_runs` and step records in
`{table_prefix}_steps`. Tables are created lazily on first use via
`CREATE TABLE IF NOT EXISTS`. Step outputs are JSON-serialized.

Reuses the same transport shape as `PopulationStore` (databricks-sdk's
`statement_execution.execute_statement`) so OBO / M2M auth flows
resolve identically.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from .engine import (
    RunFilter,
    RunSnapshot,
    RunStatus,
    RunSummary,
    StepFailedError,
    StepRecord,
    WorkflowEngine,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(s: str) -> str:
    """Escape single quotes for inline SQL string values."""
    return s.replace("'", "''")


def _sql_literal(value: Any) -> str:
    """Return a SQL literal for a Python value, using JSON for complex types."""
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        return f"'{_esc(json.dumps(value))}'"
    return f"'{_esc(str(value))}'"


def _parse_json_or_none(s: Any) -> Any:
    if s is None or s == "":
        return None
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s
    return s


class DeltaEngine(WorkflowEngine):
    def __init__(
        self,
        ws: "WorkspaceClient",
        *,
        table_prefix: str,
        warehouse_id: str,
        cache_enabled: bool = True,
    ):
        self._ws = ws
        self._warehouse_id = warehouse_id
        self._runs_table = f"{table_prefix}_runs"
        self._steps_table = f"{table_prefix}_steps"
        self._cache_enabled = cache_enabled
        self._step_cache: dict[tuple[str, str], StepRecord] = {}
        self._bootstrap_lock = asyncio.Lock()
        self._bootstrapped = False

    # ------------------------------------------------------------------
    # WorkflowEngine
    # ------------------------------------------------------------------

    async def start_run(
        self,
        workflow_name: str,
        input: Any,
        run_id: str | None = None,
    ) -> str:
        await self._bootstrap()

        rid = run_id or str(uuid.uuid4())
        input_json = json.dumps(input) if input is not None else "null"
        sql = f"""
            MERGE INTO {self._runs_table} AS target
            USING (SELECT
                '{_esc(rid)}' AS run_id,
                '{_esc(workflow_name)}' AS workflow_name,
                '{_esc(input_json)}' AS input
            ) AS source
            ON target.run_id = source.run_id
            WHEN MATCHED THEN UPDATE SET
                target.status = 'running',
                target.updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (
                run_id, workflow_name, status, input, started_at, updated_at
            ) VALUES (
                source.run_id, source.workflow_name, 'running', source.input,
                current_timestamp(), current_timestamp()
            )
        """
        await self._exec(sql)
        return rid

    async def step(
        self,
        run_id: str,
        step_key: str,
        handler: Callable[[], Awaitable[T]],
    ) -> T:
        await self._bootstrap()

        cached = await self._lookup_step(run_id, step_key)
        if cached is not None:
            if cached.status == "completed":
                return cached.output  # type: ignore[return-value]
            raise StepFailedError(step_key, cached.error or "step failed")

        start = time.monotonic()
        try:
            result = await handler()
        except Exception as err:
            record = StepRecord(
                step_key=step_key,
                status="failed",
                error=str(err),
                duration_ms=int((time.monotonic() - start) * 1000),
                recorded_at=_now(),
            )
            await self._persist_step(run_id, record)
            if self._cache_enabled:
                self._step_cache[(run_id, step_key)] = record
            raise

        record = StepRecord(
            step_key=step_key,
            status="completed",
            output=result,
            duration_ms=int((time.monotonic() - start) * 1000),
            recorded_at=_now(),
        )
        await self._persist_step(run_id, record)
        if self._cache_enabled:
            self._step_cache[(run_id, step_key)] = record
        return result

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        output: Any = None,
    ) -> None:
        await self._bootstrap()

        set_output = (
            ""
            if output is None
            else f", output = '{_esc(json.dumps(output))}'"
        )
        sql = (
            f"UPDATE {self._runs_table} SET status = '{_esc(status)}'"
            f"{set_output}, updated_at = current_timestamp() "
            f"WHERE run_id = '{_esc(run_id)}'"
        )
        await self._exec(sql)

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        await self._bootstrap()

        run_rows = await self._exec(
            f"SELECT run_id, workflow_name, status, input, output, started_at, updated_at "
            f"FROM {self._runs_table} WHERE run_id = '{_esc(run_id)}'"
        )
        if not run_rows:
            return None
        r = run_rows[0]

        step_rows = await self._exec(
            f"SELECT step_key, status, output, error, duration_ms, recorded_at "
            f"FROM {self._steps_table} WHERE run_id = '{_esc(run_id)}'"
        )

        return RunSnapshot(
            run_id=r.get("run_id", run_id),
            workflow_name=r.get("workflow_name", ""),
            status=r.get("status", "running"),
            input=_parse_json_or_none(r.get("input")),
            output=_parse_json_or_none(r.get("output")),
            started_at=str(r.get("started_at", "")),
            updated_at=str(r.get("updated_at", "")),
            steps=[
                StepRecord(
                    step_key=s.get("step_key", ""),
                    status=s.get("status", "completed"),
                    output=_parse_json_or_none(s.get("output")),
                    error=s.get("error"),
                    duration_ms=int(s.get("duration_ms") or 0),
                    recorded_at=str(s.get("recorded_at", "")),
                )
                for s in step_rows
            ],
        )

    async def list_runs(self, filter: RunFilter | None = None) -> list[RunSummary]:
        await self._bootstrap()

        conditions: list[str] = []
        if filter and filter.workflow_name:
            conditions.append(f"workflow_name = '{_esc(filter.workflow_name)}'")
        if filter and filter.status:
            conditions.append(f"status = '{_esc(filter.status)}'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit = (
            f"LIMIT {int(filter.limit)}"
            if filter and filter.limit is not None and filter.limit > 0
            else ""
        )

        rows = await self._exec(
            f"SELECT run_id, workflow_name, status, started_at, updated_at "
            f"FROM {self._runs_table} {where} ORDER BY started_at DESC {limit}"
        )
        return [
            RunSummary(
                run_id=r.get("run_id", ""),
                workflow_name=r.get("workflow_name", ""),
                status=r.get("status", "running"),
                started_at=str(r.get("started_at", "")),
                updated_at=str(r.get("updated_at", "")),
            )
            for r in rows
        ]

    def clear_cache(self) -> None:
        """Drop all in-process caches. Useful for tests."""
        self._step_cache.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        async with self._bootstrap_lock:
            if self._bootstrapped:
                return
            await self._exec(f"""
                CREATE TABLE IF NOT EXISTS {self._runs_table} (
                    run_id        STRING NOT NULL,
                    workflow_name STRING NOT NULL,
                    status        STRING NOT NULL,
                    input         STRING,
                    output        STRING,
                    started_at    TIMESTAMP NOT NULL,
                    updated_at    TIMESTAMP NOT NULL
                ) USING DELTA
            """)
            await self._exec(f"""
                CREATE TABLE IF NOT EXISTS {self._steps_table} (
                    run_id      STRING NOT NULL,
                    step_key    STRING NOT NULL,
                    status      STRING NOT NULL,
                    output      STRING,
                    error       STRING,
                    duration_ms BIGINT,
                    recorded_at TIMESTAMP NOT NULL
                ) USING DELTA
            """)
            self._bootstrapped = True

    async def _lookup_step(self, run_id: str, step_key: str) -> StepRecord | None:
        if self._cache_enabled:
            hit = self._step_cache.get((run_id, step_key))
            if hit is not None:
                return hit

        rows = await self._exec(
            f"SELECT step_key, status, output, error, duration_ms, recorded_at "
            f"FROM {self._steps_table} "
            f"WHERE run_id = '{_esc(run_id)}' AND step_key = '{_esc(step_key)}' LIMIT 1"
        )
        if not rows:
            return None
        r = rows[0]
        record = StepRecord(
            step_key=r.get("step_key", step_key),
            status=r.get("status", "completed"),
            output=_parse_json_or_none(r.get("output")),
            error=r.get("error"),
            duration_ms=int(r.get("duration_ms") or 0),
            recorded_at=str(r.get("recorded_at", "")),
        )
        if self._cache_enabled:
            self._step_cache[(run_id, step_key)] = record
        return record

    async def _persist_step(self, run_id: str, record: StepRecord) -> None:
        output_lit = (
            "NULL"
            if record.output is None
            else f"'{_esc(json.dumps(record.output))}'"
        )
        error_lit = "NULL" if record.error is None else f"'{_esc(record.error)}'"

        sql = f"""
            MERGE INTO {self._steps_table} AS target
            USING (SELECT
                '{_esc(run_id)}' AS run_id,
                '{_esc(record.step_key)}' AS step_key,
                '{_esc(record.status)}' AS status,
                {output_lit} AS output,
                {error_lit} AS error,
                {record.duration_ms} AS duration_ms
            ) AS source
            ON target.run_id = source.run_id AND target.step_key = source.step_key
            WHEN NOT MATCHED THEN INSERT (
                run_id, step_key, status, output, error, duration_ms, recorded_at
            ) VALUES (
                source.run_id, source.step_key, source.status, source.output,
                source.error, source.duration_ms, current_timestamp()
            )
        """
        await self._exec(sql)

    async def _exec(self, sql: str) -> list[dict]:
        """Execute a SQL statement, running the blocking SDK call in a thread."""
        return await asyncio.to_thread(self._exec_sync, sql)

    def _exec_sync(self, sql: str) -> list[dict]:
        from databricks.sdk.service.sql import StatementState

        resp = self._ws.statement_execution.execute_statement(
            warehouse_id=self._warehouse_id,
            statement=sql.strip(),
            wait_timeout="50s",
        )
        if resp.status.state == StatementState.FAILED:
            err = resp.status.error
            raise RuntimeError(
                f"Databricks SQL failed [{err.error_code}]: {err.message}\n"
                f"SQL: {sql[:200]}"
            )
        if not resp.result or not resp.result.data_array:
            return []
        cols = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(cols, row)) for row in resp.result.data_array]

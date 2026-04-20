"""
InMemoryEngine — default WorkflowEngine backend.

Stores runs and step records in a process-local dict. Preserves the
workflow API's step-caching and replay semantics so tests can exercise
resumption without a SQL warehouse, but loses all state on process exit.
Use `DeltaEngine` for real durability.
"""
from __future__ import annotations

import copy
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from .engine import (
    RunFilter,
    RunSnapshot,
    RunStatus,
    RunSummary,
    StepFailedError,
    StepRecord,
    WorkflowEngine,
)

T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MutableRun:
    __slots__ = (
        "run_id",
        "workflow_name",
        "status",
        "input",
        "output",
        "started_at",
        "updated_at",
        "steps",
    )

    def __init__(self, run_id: str, workflow_name: str, input: Any):
        now = _now()
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.status: RunStatus = "running"
        self.input = copy.deepcopy(input)
        self.output: Any = None
        self.started_at = now
        self.updated_at = now
        self.steps: dict[str, StepRecord] = {}


class InMemoryEngine(WorkflowEngine):
    def __init__(self) -> None:
        self._runs: dict[str, _MutableRun] = {}

    async def start_run(
        self,
        workflow_name: str,
        input: Any,
        run_id: str | None = None,
    ) -> str:
        existing = self._runs.get(run_id) if run_id else None
        if existing is not None:
            existing.status = "running"
            existing.updated_at = _now()
            return existing.run_id

        rid = run_id or str(uuid.uuid4())
        self._runs[rid] = _MutableRun(rid, workflow_name, input)
        return rid

    async def step(
        self,
        run_id: str,
        step_key: str,
        handler: Callable[[], Awaitable[T]],
    ) -> T:
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")

        cached = run.steps.get(step_key)
        if cached is not None:
            if cached.status == "completed":
                return copy.deepcopy(cached.output)  # type: ignore[return-value]
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
            run.steps[step_key] = record
            run.updated_at = record.recorded_at
            raise

        record = StepRecord(
            step_key=step_key,
            status="completed",
            output=copy.deepcopy(result),
            duration_ms=int((time.monotonic() - start) * 1000),
            recorded_at=_now(),
        )
        run.steps[step_key] = record
        run.updated_at = record.recorded_at
        return result

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        output: Any = None,
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        run.status = status
        if output is not None:
            run.output = copy.deepcopy(output)
        run.updated_at = _now()

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return RunSnapshot(
            run_id=run.run_id,
            workflow_name=run.workflow_name,
            status=run.status,
            input=copy.deepcopy(run.input),
            output=copy.deepcopy(run.output) if run.output is not None else None,
            started_at=run.started_at,
            updated_at=run.updated_at,
            steps=[
                StepRecord(
                    step_key=s.step_key,
                    status=s.status,
                    output=copy.deepcopy(s.output),
                    error=s.error,
                    duration_ms=s.duration_ms,
                    recorded_at=s.recorded_at,
                )
                for s in run.steps.values()
            ],
        )

    async def list_runs(self, filter: RunFilter | None = None) -> list[RunSummary]:
        results = list(self._runs.values())
        if filter is not None:
            if filter.workflow_name is not None:
                results = [r for r in results if r.workflow_name == filter.workflow_name]
            if filter.status is not None:
                results = [r for r in results if r.status == filter.status]
        results.sort(key=lambda r: r.started_at, reverse=True)
        if filter is not None and filter.limit is not None:
            results = results[: filter.limit]
        return [
            RunSummary(
                run_id=r.run_id,
                workflow_name=r.workflow_name,
                status=r.status,
                started_at=r.started_at,
                updated_at=r.updated_at,
            )
            for r in results
        ]

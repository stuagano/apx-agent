"""
WorkflowEngine — durable execution primitive for workflow agents.

Each step of a workflow is wrapped in `engine.step(run_id, step_key, handler)`.
The engine persists the step's output (or failure) keyed by `(run_id, step_key)`,
so a subsequent call with the same key returns the cached result instead of
re-invoking the handler. This is what lets a workflow resume after a crash,
redeploy, or pause — the completed steps replay from persistence, and the
first uncompleted step runs fresh.

See the TypeScript mirror at `typescript/src/workflows/engine.ts` and the
design spec at `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")

RunStatus = Literal["running", "paused", "completed", "converged", "failed", "cancelled"]


@dataclass
class StepRecord:
    """Persisted record of a single step invocation."""
    step_key: str
    status: Literal["completed", "failed"]
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    recorded_at: str = ""


@dataclass
class RunSnapshot:
    """Full snapshot of a run, including its step log."""
    run_id: str
    workflow_name: str
    status: RunStatus
    input: Any
    started_at: str
    updated_at: str
    output: Any = None
    steps: list[StepRecord] = field(default_factory=list)


@dataclass
class RunSummary:
    """Compact summary returned by list_runs()."""
    run_id: str
    workflow_name: str
    status: RunStatus
    started_at: str
    updated_at: str


@dataclass
class RunFilter:
    workflow_name: str | None = None
    status: RunStatus | None = None
    limit: int | None = None


class StepFailedError(Exception):
    """
    Thrown when a handler raised an error that the engine persisted. Replay of
    a previously failed step re-throws this so callers see the same failure
    they would have seen originally.
    """
    def __init__(self, step_key: str, message: str):
        super().__init__(message)
        self.step_key = step_key


@runtime_checkable
class WorkflowEngine(Protocol):
    """
    Pluggable backend for durable workflow execution.

    Implementations:
    - `InMemoryEngine` — per-process dict, default, used in tests and dev.
    - `DeltaEngine`    — SQL Statements API against a Delta table.
    """

    async def start_run(
        self,
        workflow_name: str,
        input: Any,
        run_id: str | None = None,
    ) -> str:
        """
        Start a new run, or re-open an existing one.

        If `run_id` is provided and an existing run is found, the run is
        re-opened: status is set back to `running` and subsequent `step()`
        calls replay from the persisted log. Otherwise, a new run is created.
        Returns the run's ID.
        """
        ...

    async def step(
        self,
        run_id: str,
        step_key: str,
        handler: Callable[[], Awaitable[T]],
    ) -> T:
        """
        Execute a checkpointed step.

        - On cache hit with `status == 'completed'`: returns the persisted
          output without invoking `handler`.
        - On cache hit with `status == 'failed'`: raises a `StepFailedError`
          without invoking `handler`.
        - On cache miss: invokes `handler`, persists the result (or failure),
          then returns or raises.

        `step_key` must be stable across replays — e.g. `f"mutate-{generation}"`.
        """
        ...

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        output: Any = None,
    ) -> None:
        """Mark a run finished with a terminal or paused status."""
        ...

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        """Read the full snapshot of a run. Returns None if not found."""
        ...

    async def list_runs(self, filter: RunFilter | None = None) -> list[RunSummary]:
        """List runs matching the given filter."""
        ...

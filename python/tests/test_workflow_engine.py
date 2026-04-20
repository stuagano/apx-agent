"""Tests for the Python WorkflowEngine primitives."""
from __future__ import annotations

import pytest

from apx_agent.workflow import (
    InMemoryEngine,
    RunFilter,
    StepFailedError,
)


@pytest.fixture
def engine() -> InMemoryEngine:
    return InMemoryEngine()


# ---------------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------------


class TestStartRun:
    @pytest.mark.asyncio
    async def test_creates_run_and_returns_run_id(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {"seed": 1})
        assert run_id

        snap = await engine.get_run(run_id)
        assert snap is not None
        assert snap.workflow_name == "wf"
        assert snap.status == "running"
        assert snap.input == {"seed": 1}

    @pytest.mark.asyncio
    async def test_reuses_provided_run_id(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {}, run_id="custom-id")
        assert run_id == "custom-id"

    @pytest.mark.asyncio
    async def test_reopens_existing_run(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})
        await engine.finish_run(run_id, "paused")

        resumed = await engine.start_run("wf", {}, run_id=run_id)
        assert resumed == run_id

        snap = await engine.get_run(run_id)
        assert snap.status == "running"


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


class TestStep:
    @pytest.mark.asyncio
    async def test_invokes_handler_on_cache_miss(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})
        invocations = 0

        async def handler():
            nonlocal invocations
            invocations += 1
            return {"value": 42}

        result = await engine.step(run_id, "a", handler)
        assert result == {"value": 42}
        assert invocations == 1

        snap = await engine.get_run(run_id)
        assert len(snap.steps) == 1
        assert snap.steps[0].step_key == "a"
        assert snap.steps[0].status == "completed"
        assert snap.steps[0].output == {"value": 42}

    @pytest.mark.asyncio
    async def test_returns_cached_output_on_replay(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})
        invocations = 0

        async def handler():
            nonlocal invocations
            invocations += 1
            return {"count": invocations}

        first = await engine.step(run_id, "a", handler)
        second = await engine.step(run_id, "a", handler)

        assert first == {"count": 1}
        assert second == {"count": 1}
        assert invocations == 1

    @pytest.mark.asyncio
    async def test_persists_failures_and_raises_step_failed_error_on_replay(
        self, engine: InMemoryEngine
    ):
        run_id = await engine.start_run("wf", {})

        async def failing():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await engine.step(run_id, "a", failing)

        # Second call with the same key: replay the failure without invoking.
        async def never_called():
            raise AssertionError("handler should not be invoked on replay")

        with pytest.raises(StepFailedError):
            await engine.step(run_id, "a", never_called)

    @pytest.mark.asyncio
    async def test_distinguishes_steps_by_key(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})
        a = await engine.step(run_id, "a", _returning(1))
        b = await engine.step(run_id, "b", _returning(2))
        assert (a, b) == (1, 2)

        snap = await engine.get_run(run_id)
        assert sorted(s.step_key for s in snap.steps) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_isolates_runs(self, engine: InMemoryEngine):
        r1 = await engine.start_run("wf", {})
        r2 = await engine.start_run("wf", {})

        await engine.step(r1, "shared", _returning("one"))
        result = await engine.step(r2, "shared", _returning("two"))
        assert result == "two"

    @pytest.mark.asyncio
    async def test_raises_for_unknown_run_id(self, engine: InMemoryEngine):
        with pytest.raises(ValueError, match="Unknown run_id"):
            await engine.step("ghost", "a", _returning(1))

    @pytest.mark.asyncio
    async def test_returns_cloned_output(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})

        async def produce():
            return {"count": 1}

        first = await engine.step(run_id, "a", produce)
        first["count"] = 99  # mutate returned value

        second = await engine.step(run_id, "a", produce)
        assert second == {"count": 1}


# ---------------------------------------------------------------------------
# finish_run / list_runs
# ---------------------------------------------------------------------------


class TestFinishAndList:
    @pytest.mark.asyncio
    async def test_finish_run_updates_status_and_output(self, engine: InMemoryEngine):
        run_id = await engine.start_run("wf", {})
        await engine.finish_run(run_id, "completed", {"final": True})
        snap = await engine.get_run(run_id)
        assert snap.status == "completed"
        assert snap.output == {"final": True}

    @pytest.mark.asyncio
    async def test_list_runs_filters_by_workflow_and_status(self, engine: InMemoryEngine):
        r1 = await engine.start_run("wf-a", {})
        r2 = await engine.start_run("wf-b", {})
        await engine.finish_run(r1, "completed")

        completed = await engine.list_runs(RunFilter(status="completed"))
        assert [r.run_id for r in completed] == [r1]

        wf_b = await engine.list_runs(RunFilter(workflow_name="wf-b"))
        assert [r.run_id for r in wf_b] == [r2]

    @pytest.mark.asyncio
    async def test_list_runs_respects_limit(self, engine: InMemoryEngine):
        for _ in range(5):
            await engine.start_run("wf", {})

        subset = await engine.list_runs(RunFilter(limit=2))
        assert len(subset) == 2

    @pytest.mark.asyncio
    async def test_get_run_returns_none_for_unknown_id(self, engine: InMemoryEngine):
        assert await engine.get_run("ghost") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _returning(value):
    async def inner():
        return value
    return inner

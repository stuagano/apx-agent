"""Tests for the Python WorkflowEngine primitives."""
from __future__ import annotations

import re

import pytest

from apx_agent.workflow import (
    InMemoryEngine,
    RunFilter,
    StepFailedError,
)
from apx_agent.workflow.engine_delta import DeltaEngine, _esc


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


# ---------------------------------------------------------------------------
# DeltaEngine — fake WorkspaceClient over the SQL Statements API
# ---------------------------------------------------------------------------


def _unesc(s: str) -> str:
    """Inverse of engine_delta._esc — undo '' → ' and \\\\ → \\.

    Used by the fake to reconstruct the originally stored value, so the
    round-trip through _esc (escape) and back (this) is genuinely exercised.
    """
    return s.replace("''", "\x00").replace("\\\\", "\\").replace("\x00", "'")


def _extract_literals(select_clause: str) -> list[str]:
    """Pull the inline single-quoted string literals out of a USING SELECT.

    Returns the un-escaped values in source order. Quotes inside a literal are
    represented as '' (SQL doubling), which _unesc collapses; a literal value
    can therefore itself contain escaped quotes/backslashes.
    """
    # Match '...' where an embedded quote is doubled ('') — standard SQL literal.
    raw = re.findall(r"'((?:[^']|'')*)'", select_clause)
    return [_unesc(r) for r in raw]


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    @property
    def data_array(self):
        return [list(r.values()) for r in self._rows] if self._rows else None

    @property
    def _cols(self):
        return list(self._rows[0].keys()) if self._rows else []


class _FakeManifestSchema:
    def __init__(self, cols: list[str]):
        self.columns = [type("Col", (), {"name": c})() for c in cols]


class _FakeManifest:
    def __init__(self, cols: list[str]):
        self.schema = _FakeManifestSchema(cols)


class _FakeStatus:
    def __init__(self):
        from databricks.sdk.service.sql import StatementState
        self.state = StatementState.SUCCEEDED
        self.error = None


class _FakeResponse:
    def __init__(self, rows: list[dict]):
        cols = list(rows[0].keys()) if rows else []
        self.status = _FakeStatus()
        self.result = _FakeResult(rows) if rows else None
        self.manifest = _FakeManifest(cols)


class _FakeStatementExecution:
    """Emulates runs/steps Delta tables backing DeltaEngine, by parsing the
    inline SQL it issues. Stores values via _unesc so that what the engine
    escapes with _esc is recovered exactly on read."""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.steps: dict[tuple[str, str], dict] = {}
        self.executed: list[str] = []

    def execute_statement(self, *, warehouse_id, statement, wait_timeout):
        self.executed.append(statement)
        sql = statement.strip()
        upper = sql.upper()

        if upper.startswith("CREATE TABLE"):
            return _FakeResponse([])

        if "MERGE INTO" in upper and "_RUNS" in upper:
            vals = _extract_literals(sql)
            run_id, workflow_name, input_json = vals[0], vals[1], vals[2]
            existing = self.runs.get(run_id)
            if existing:
                existing["status"] = "running"
            else:
                self.runs[run_id] = {
                    "run_id": run_id,
                    "workflow_name": workflow_name,
                    "status": "running",
                    "input": input_json,
                    "output": None,
                    "started_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            return _FakeResponse([])

        if "MERGE INTO" in upper and "_STEPS" in upper:
            # USING (SELECT 'run_id', 'step_key', 'status', <output>, <error>, <dur>)
            vals = _extract_literals(sql)
            run_id, step_key, status = vals[0], vals[1], vals[2]
            # output / error literals (if present) follow in source order
            output = vals[3] if len(vals) > 3 else None
            # Distinguish output vs error by NULL markers in the statement.
            after = sql.split("AS status,", 1)[1]
            output_is_null = bool(re.match(r"\s*NULL\s+AS output", after))
            self.steps[(run_id, step_key)] = {
                "run_id": run_id,
                "step_key": step_key,
                "status": status,
                "output": None if output_is_null else _step_output_literal(sql),
                "error": _step_error_literal(sql),
                "duration_ms": 0,
                "recorded_at": "2026-01-01T00:00:00Z",
            }
            return _FakeResponse([])

        if upper.startswith("UPDATE") and "_RUNS" in upper:
            vals = _extract_literals(sql)
            # SET status = '<status>' [, output = '<output>'] WHERE run_id = '<id>'
            run_id = vals[-1]
            status = vals[0]
            if run_id in self.runs:
                self.runs[run_id]["status"] = status
                if "output =" in sql:
                    self.runs[run_id]["output"] = vals[1]
            return _FakeResponse([])

        if upper.startswith("SELECT") and "_STEPS" in upper:
            vals = _extract_literals(sql)
            if "STEP_KEY =" in upper:
                run_id, step_key = vals[0], vals[1]
                rec = self.steps.get((run_id, step_key))
                rows = [_step_row(rec)] if rec else []
            else:
                run_id = vals[0]
                rows = [
                    _step_row(r) for (rid, _), r in self.steps.items() if rid == run_id
                ]
            return _FakeResponse(rows)

        if upper.startswith("SELECT") and "_RUNS" in upper:
            vals = _extract_literals(sql)
            run_id = vals[0]
            run = self.runs.get(run_id)
            return _FakeResponse([dict(run)] if run else [])

        return _FakeResponse([])


def _step_output_literal(sql: str) -> str | None:
    m = re.search(r"AS status,\s*'((?:[^']|'')*)'\s*AS output", sql, re.DOTALL)
    return _unesc(m.group(1)) if m else None


def _step_error_literal(sql: str) -> str | None:
    m = re.search(r"AS output,\s*'((?:[^']|'')*)'\s*AS error", sql, re.DOTALL)
    return _unesc(m.group(1)) if m else None


def _step_row(rec: dict) -> dict:
    return {
        "step_key": rec["step_key"],
        "status": rec["status"],
        "output": rec["output"],
        "error": rec["error"],
        "duration_ms": rec["duration_ms"],
        "recorded_at": rec["recorded_at"],
    }


class _FakeWorkspaceClient:
    def __init__(self):
        self.statement_execution = _FakeStatementExecution()


def _delta_engine() -> DeltaEngine:
    # cache_enabled=False so the SQL persist/lookup path is exercised — with the
    # cache on, a replayed step would hit the in-process dict and never touch
    # the (untested) Delta SQL path that this suite exists to cover.
    return DeltaEngine(
        _FakeWorkspaceClient(),
        table_prefix="t.s.wf",
        warehouse_id="wh",
        cache_enabled=False,
    )


class TestDeltaEsc:
    def test_doubles_single_quotes(self):
        assert _esc("o'brien") == "o''brien"

    def test_escapes_backslashes_before_quotes(self):
        # A trailing backslash must be doubled so it can't escape the closing
        # quote of the inline literal.
        assert _esc("path\\") == "path\\\\"

    def test_quote_and_backslash_round_trip(self):
        original = "a'b\\c\\'d"
        assert _unesc(_esc(original)) == original


class TestDeltaEngine:
    @pytest.mark.asyncio
    async def test_completed_step_returns_stored_output_without_rehandler(self):
        engine = _delta_engine()
        run_id = await engine.start_run("wf", {"seed": 1})
        invocations = 0

        async def handler():
            nonlocal invocations
            invocations += 1
            return {"value": 42}

        first = await engine.step(run_id, "a", handler)
        # Fresh engine view of the same Delta tables: cache is off and a new
        # instance has an empty cache anyway, so replay must read from SQL.
        second = await engine.step(run_id, "a", handler)

        assert first == {"value": 42}
        assert second == {"value": 42}
        assert invocations == 1

    @pytest.mark.asyncio
    async def test_failed_step_reraises_step_failed_error_on_replay(self):
        engine = _delta_engine()
        run_id = await engine.start_run("wf", {})

        async def failing():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await engine.step(run_id, "a", failing)

        async def never_called():
            raise AssertionError("handler must not run on replay of a failed step")

        with pytest.raises(StepFailedError):
            await engine.step(run_id, "a", never_called)

    @pytest.mark.asyncio
    async def test_idempotent_run_reopen(self):
        ws = _FakeWorkspaceClient()
        engine = DeltaEngine(ws, table_prefix="t.s.wf", warehouse_id="wh", cache_enabled=False)

        run_id = await engine.start_run("wf", {}, run_id="custom-id")
        assert run_id == "custom-id"
        await engine.finish_run(run_id, "paused")
        assert ws.statement_execution.runs["custom-id"]["status"] == "paused"

        reopened = await engine.start_run("wf", {}, run_id="custom-id")
        assert reopened == "custom-id"

        snap = await engine.get_run("custom-id")
        assert snap is not None
        assert snap.status == "running"
        # Reopen must not create a duplicate run row.
        assert len(ws.statement_execution.runs) == 1

    @pytest.mark.asyncio
    async def test_step_output_round_trips_quotes_and_backslashes(self):
        engine = _delta_engine()
        run_id = await engine.start_run("wf", {})
        payload = {"path": "C:\\users\\o'brien\\", "note": "it's a \\ test"}

        async def handler():
            return payload

        await engine.step(run_id, "tricky", handler)
        # Replay reads the persisted JSON back through _esc / _unesc.
        replayed = await engine.step(run_id, "tricky", lambda: _fail())
        assert replayed == payload

    @pytest.mark.asyncio
    async def test_step_rejects_injection_in_run_id(self):
        engine = _delta_engine()
        with pytest.raises(ValueError, match="run_id"):
            await engine.step("a'; DROP TABLE x; --", "k", _returning(1))

    @pytest.mark.asyncio
    async def test_step_rejects_injection_in_step_key(self):
        engine = _delta_engine()
        run_id = await engine.start_run("wf", {})
        with pytest.raises(ValueError, match="step_key"):
            await engine.step(run_id, "k'; DROP TABLE x; --", _returning(1))


async def _fail():
    raise AssertionError("handler must not run on replay of a completed step")

"""Tests for jobs_tools — the Databricks Jobs introspection toolkit."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from apx_agent.jobs_tools import (
    jobs_for_table_tool,
    jobs_history_tool,
    jobs_logs_tool,
    jobs_source_paths_tool,
    jobs_tools,
)
from apx_agent._inspection import _inspect_tool_fn


# ---------------------------------------------------------------------------
# jobs_tools() convenience toolkit
# ---------------------------------------------------------------------------


class TestJobsToolkit:
    def test_returns_four_callables(self):
        tools = jobs_tools()
        assert len(tools) == 4
        for t in tools:
            assert callable(t)

    def test_inner_tool_names_match_expected(self):
        tools = jobs_tools()
        names = {t.__name__ for t in tools}
        assert names == {
            "find_jobs_for_table",
            "get_job_run_history",
            "get_job_run_logs",
            "get_job_source_paths",
        }

    def test_all_have_non_empty_docstrings(self):
        for t in jobs_tools():
            assert t.__doc__
            assert t.__doc__.strip()

    def test_all_are_coroutines(self):
        for t in jobs_tools():
            assert inspect.iscoroutinefunction(t)

    def test_all_take_workspace_dep(self):
        for t in jobs_tools():
            _, dep_params = _inspect_tool_fn(t)
            assert "ws" in dep_params

    def test_warehouse_id_forwarded_to_lineage_tool(self):
        tools = jobs_tools(warehouse_id="wh-abc")
        # Only the find_jobs_for_table tool attaches a sql_warehouse resource
        find_jobs = next(t for t in tools if t.__name__ == "find_jobs_for_table")
        specs = getattr(find_jobs, "_apx_resources", [])
        kinds = {(s.kind, s.identifier) for s in specs}
        assert ("sql_warehouse", "wh-abc") in kinds


# ---------------------------------------------------------------------------
# jobs_for_table_tool
# ---------------------------------------------------------------------------


class TestJobsForTableToolFactory:
    def test_default_name(self):
        assert jobs_for_table_tool().__name__ == "find_jobs_for_table"

    def test_custom_name(self):
        assert jobs_for_table_tool(name="my_finder").__name__ == "my_finder"

    def test_default_description_is_non_empty(self):
        assert jobs_for_table_tool().__doc__
        assert len(jobs_for_table_tool().__doc__.strip()) > 0

    def test_custom_description(self):
        t = jobs_for_table_tool(description="custom")
        assert t.__doc__ == "custom"

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(jobs_for_table_tool())

    def test_table_name_is_plain_param(self):
        tool = jobs_for_table_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "table_full_name" in plain_params
        assert plain_params["table_full_name"][0] is str
        assert "ws" in dep_params

    def test_attaches_warehouse_resource_when_pinned(self):
        tool = jobs_for_table_tool(warehouse_id="wh-1")
        specs = getattr(tool, "_apx_resources", [])
        assert any(s.kind == "sql_warehouse" and s.identifier == "wh-1" for s in specs)

    def test_no_warehouse_resource_when_auto_discover(self):
        tool = jobs_for_table_tool()
        specs = getattr(tool, "_apx_resources", [])
        assert specs == [] or not any(s.kind == "sql_warehouse" for s in specs)

    def test_smoke_instantiation(self):
        # Both with and without warehouse_id must not raise.
        jobs_for_table_tool()
        jobs_for_table_tool(warehouse_id="wh-x")

    @pytest.mark.asyncio
    async def test_invokes_run_sql_with_bind_parameter(self):
        import importlib

        jt_mod = importlib.import_module("apx_agent.jobs_tools")

        captured: list[dict] = []

        def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
            captured.append(
                {"sql": sql, "warehouse_id": warehouse_id, "parameters": parameters}
            )
            return [
                {
                    "entity_id": 1234,
                    "entity_type": "WORKFLOW_RUN",
                    "created_by": "alice@example.com",
                    "last_write": "2026-05-01",
                }
            ]

        original = jt_mod.run_sql
        jt_mod.run_sql = fake_run_sql
        try:
            tool = jobs_for_table_tool(warehouse_id="wh-1")
            result = await tool(table_full_name="main.gold.orders", ws=MagicMock())
        finally:
            jt_mod.run_sql = original

        assert result["table"] == "main.gold.orders"
        assert result["writers"][0]["entity_id"] == 1234
        # Bind parameter — not f-string interpolation — must be used.
        assert captured[0]["parameters"] == [
            {"name": "table_name", "value": "main.gold.orders", "type": "STRING"}
        ]
        assert ":table_name" in captured[0]["sql"]
        assert captured[0]["warehouse_id"] == "wh-1"

    @pytest.mark.asyncio
    async def test_returns_empty_writers_on_error(self):
        import importlib

        jt_mod = importlib.import_module("apx_agent.jobs_tools")

        def boom(*args, **kwargs):
            raise RuntimeError("permission denied")

        original = jt_mod.run_sql
        jt_mod.run_sql = boom
        try:
            tool = jobs_for_table_tool()
            result = await tool(table_full_name="main.x.y", ws=MagicMock())
        finally:
            jt_mod.run_sql = original

        assert result["writers"] == []
        assert "error" in result


# ---------------------------------------------------------------------------
# jobs_history_tool
# ---------------------------------------------------------------------------


def _make_run(run_id: int, result_state: str | None = "SUCCESS", life_cycle: str = "TERMINATED",
              state_message: str | None = None) -> MagicMock:
    r = MagicMock()
    r.run_id = run_id
    r.start_time = 1000
    r.end_time = 2000
    r.state = MagicMock()
    if result_state is not None:
        rs = MagicMock()
        rs.value = result_state
        r.state.result_state = rs
    else:
        r.state.result_state = None
    lc = MagicMock()
    lc.value = life_cycle
    r.state.life_cycle_state = lc
    r.state.state_message = state_message
    return r


class TestJobsHistoryToolFactory:
    def test_default_name(self):
        assert jobs_history_tool().__name__ == "get_job_run_history"

    def test_custom_name(self):
        assert jobs_history_tool(name="run_history").__name__ == "run_history"

    def test_default_description_is_non_empty(self):
        assert jobs_history_tool().__doc__.strip()

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(jobs_history_tool())

    def test_job_id_is_plain_param(self):
        tool = jobs_history_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "job_id" in plain_params
        assert plain_params["job_id"][0] is int
        assert "ws" in dep_params

    def test_smoke_instantiation(self):
        jobs_history_tool()
        jobs_history_tool(limit=25)

    @pytest.mark.asyncio
    async def test_returns_recent_runs(self):
        ws = MagicMock()
        ws.jobs.list_runs.return_value = [
            _make_run(1, "SUCCESS"),
            _make_run(2, "FAILED", state_message="OOM"),
        ]
        tool = jobs_history_tool()
        result = await tool(job_id=42, ws=ws)
        assert result["job_id"] == 42
        assert len(result["recent_runs"]) == 2
        assert result["recent_runs"][0]["state"] == "SUCCESS"
        assert result["recent_runs"][1]["state"] == "FAILED"
        assert result["recent_runs"][1]["error"] == "OOM"
        ws.jobs.list_runs.assert_called_once_with(job_id=42, limit=10)

    @pytest.mark.asyncio
    async def test_falls_back_to_life_cycle_state(self):
        ws = MagicMock()
        ws.jobs.list_runs.return_value = [_make_run(7, result_state=None, life_cycle="RUNNING")]
        tool = jobs_history_tool()
        result = await tool(job_id=7, ws=ws)
        assert result["recent_runs"][0]["state"] == "RUNNING"

    @pytest.mark.asyncio
    async def test_custom_limit_forwarded(self):
        ws = MagicMock()
        ws.jobs.list_runs.return_value = []
        tool = jobs_history_tool(limit=5)
        await tool(job_id=99, ws=ws)
        ws.jobs.list_runs.assert_called_once_with(job_id=99, limit=5)

    @pytest.mark.asyncio
    async def test_returns_error_on_sdk_failure(self):
        ws = MagicMock()
        ws.jobs.list_runs.side_effect = Exception("forbidden")
        tool = jobs_history_tool()
        result = await tool(job_id=1, ws=ws)
        assert result["recent_runs"] == []
        assert "error" in result


# ---------------------------------------------------------------------------
# jobs_logs_tool
# ---------------------------------------------------------------------------


class TestJobsLogsToolFactory:
    def test_default_name(self):
        assert jobs_logs_tool().__name__ == "get_job_run_logs"

    def test_custom_name(self):
        assert jobs_logs_tool(name="run_logs").__name__ == "run_logs"

    def test_default_description_is_non_empty(self):
        assert jobs_logs_tool().__doc__.strip()

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(jobs_logs_tool())

    def test_run_id_is_plain_param(self):
        tool = jobs_logs_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "run_id" in plain_params
        assert plain_params["run_id"][0] is int
        assert "ws" in dep_params

    def test_smoke_instantiation(self):
        jobs_logs_tool()
        jobs_logs_tool(max_log_chars=1000)

    @pytest.mark.asyncio
    async def test_returns_error_trace_and_truncated_logs(self):
        ws = MagicMock()
        output = MagicMock()
        output.error = "RuntimeError: foo"
        output.error_trace = "traceback…"
        output.logs = "L" * 10000
        ws.jobs.get_run_output.return_value = output
        tool = jobs_logs_tool()
        result = await tool(run_id=555, ws=ws)
        assert result["run_id"] == 555
        assert result["error"] == "RuntimeError: foo"
        assert result["error_trace"] == "traceback…"
        # Default max_log_chars=5000
        assert len(result["logs"]) == 5000
        ws.jobs.get_run_output.assert_called_once_with(run_id=555)

    @pytest.mark.asyncio
    async def test_custom_truncation_limit(self):
        ws = MagicMock()
        output = MagicMock()
        output.error = None
        output.error_trace = None
        output.logs = "x" * 500
        ws.jobs.get_run_output.return_value = output
        tool = jobs_logs_tool(max_log_chars=100)
        result = await tool(run_id=1, ws=ws)
        assert len(result["logs"]) == 100

    @pytest.mark.asyncio
    async def test_handles_none_logs(self):
        ws = MagicMock()
        output = MagicMock()
        output.error = None
        output.error_trace = None
        output.logs = None
        ws.jobs.get_run_output.return_value = output
        tool = jobs_logs_tool()
        result = await tool(run_id=1, ws=ws)
        assert result["logs"] == ""

    @pytest.mark.asyncio
    async def test_returns_error_on_sdk_failure(self):
        ws = MagicMock()
        ws.jobs.get_run_output.side_effect = Exception("not found")
        tool = jobs_logs_tool()
        result = await tool(run_id=999, ws=ws)
        assert result["logs"] == ""
        assert "error" in result and result["error"]


# ---------------------------------------------------------------------------
# jobs_source_paths_tool
# ---------------------------------------------------------------------------


def _make_task(*, notebook_path=None, python_file=None, dbt_dir=None, pipeline_id=None) -> MagicMock:
    t = MagicMock()
    if notebook_path:
        t.notebook_task = MagicMock()
        t.notebook_task.notebook_path = notebook_path
    else:
        t.notebook_task = None
    if python_file:
        t.spark_python_task = MagicMock()
        t.spark_python_task.python_file = python_file
    else:
        t.spark_python_task = None
    if dbt_dir:
        t.dbt_task = MagicMock()
        t.dbt_task.project_directory = dbt_dir
    else:
        t.dbt_task = None
    if pipeline_id:
        t.pipeline_task = MagicMock()
        t.pipeline_task.pipeline_id = pipeline_id
    else:
        t.pipeline_task = None
    return t


class TestJobsSourcePathsToolFactory:
    def test_default_name(self):
        assert jobs_source_paths_tool().__name__ == "get_job_source_paths"

    def test_custom_name(self):
        assert jobs_source_paths_tool(name="sources").__name__ == "sources"

    def test_default_description_is_non_empty(self):
        assert jobs_source_paths_tool().__doc__.strip()

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(jobs_source_paths_tool())

    def test_job_id_is_plain_param(self):
        tool = jobs_source_paths_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "job_id" in plain_params
        assert plain_params["job_id"][0] is int
        assert "ws" in dep_params

    def test_smoke_instantiation(self):
        jobs_source_paths_tool()

    @pytest.mark.asyncio
    async def test_extracts_all_task_types(self):
        ws = MagicMock()
        job = MagicMock()
        job.settings.name = "etl-job"
        job.settings.tasks = [
            _make_task(notebook_path="/Repos/etl/notebook"),
            _make_task(python_file="dbfs:/scripts/main.py"),
            _make_task(dbt_dir="/Repos/dbt/project"),
            _make_task(pipeline_id="pipeline-123"),
        ]
        ws.jobs.get.return_value = job
        tool = jobs_source_paths_tool()
        result = await tool(job_id=10, ws=ws)
        assert result["job_id"] == 10
        assert result["name"] == "etl-job"
        types = [t["type"] for t in result["tasks"]]
        assert types == ["notebook", "python", "dbt", "pipeline"]
        ws.jobs.get.assert_called_once_with(job_id=10)

    @pytest.mark.asyncio
    async def test_handles_no_settings(self):
        ws = MagicMock()
        job = MagicMock()
        job.settings = None
        ws.jobs.get.return_value = job
        tool = jobs_source_paths_tool()
        result = await tool(job_id=10, ws=ws)
        assert result["tasks"] == []
        assert result["name"] is None

    @pytest.mark.asyncio
    async def test_returns_error_on_sdk_failure(self):
        ws = MagicMock()
        ws.jobs.get.side_effect = Exception("not found")
        tool = jobs_source_paths_tool()
        result = await tool(job_id=999, ws=ws)
        assert result["tasks"] == []
        assert "error" in result and result["error"]


# ---------------------------------------------------------------------------
# Public re-export from apx_agent
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_imports_from_apx_agent(self):
        import apx_agent

        assert hasattr(apx_agent, "jobs_tools")
        assert hasattr(apx_agent, "jobs_for_table_tool")
        assert hasattr(apx_agent, "jobs_history_tool")
        assert hasattr(apx_agent, "jobs_logs_tool")
        assert hasattr(apx_agent, "jobs_source_paths_tool")

    def test_in_dunder_all(self):
        import apx_agent

        for name in (
            "jobs_tools",
            "jobs_for_table_tool",
            "jobs_history_tool",
            "jobs_logs_tool",
            "jobs_source_paths_tool",
        ):
            assert name in apx_agent.__all__

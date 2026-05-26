"""jobs_tools — Databricks Jobs introspection tool factories.

A toolkit for agents that triage data freshness / quality issues by tracing
a Unity Catalog table back to the Workflow that writes it and inspecting
that job's recent runs, errors, and source code paths.

Four factories, exported individually and bundled via ``jobs_tools()``:

  * ``jobs_for_table_tool``    — find Workflows / DLT pipelines that wrote
    to a UC table, via ``system.access.table_lineage``.
  * ``jobs_history_tool``      — recent run history for a Job ID
    (``ws.jobs.list_runs``).
  * ``jobs_logs_tool``         — error / error_trace / logs from a single
    run (``ws.jobs.get_run_output``).
  * ``jobs_source_paths_tool`` — extract notebook / python / dbt / pipeline
    paths from a Job's task definitions (``ws.jobs.get``).

Annotations are intentionally NOT deferred (no ``from __future__ import
annotations``) so ``UserClientDependency`` resolves eagerly at function
definition time and ``get_type_hints()`` in ``_inspection.py`` sees the
real ``Annotated[...]`` type, not a string — same pattern as ``sql_tools.py``
and ``catalog.py``.
"""

import logging
from typing import Any

from ._sql import run_sql

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# jobs_for_table_tool
# ---------------------------------------------------------------------------


def jobs_for_table_tool(
    *,
    warehouse_id: str | None = None,
    name: str = "find_jobs_for_table",
    description: str | None = None,
) -> Any:
    """Return a tool that finds Workflows / DLT pipelines writing to a UC table.

    Queries ``system.access.table_lineage`` for ``WORKFLOW_RUN`` and
    ``PIPELINE_UPDATE`` writers — distinct entity IDs the agent can follow
    up on with ``jobs_history_tool``. Requires UC system tables to be
    enabled on the metastore and the calling user to have ``SELECT`` on
    ``system.access.table_lineage``.

    Usage::

        from apx_agent import Agent, jobs_for_table_tool

        agent = Agent(tools=[jobs_for_table_tool(warehouse_id="abc123")])

    Args:
        warehouse_id: Fixed SQL warehouse ID. When set, declared as a
            ``DatabricksSQLWarehouse`` resource at log time. When omitted,
            the warehouse is auto-discovered on each call (dev convenience;
            production deployments should pin a warehouse).
        name: Tool name shown to the LLM. Defaults to ``"find_jobs_for_table"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        "Find Databricks Jobs and DLT pipelines that write to a Unity Catalog "
        "table, via system.access.table_lineage. Returns entity_id values "
        "(Job IDs / Pipeline IDs) you can pass to get_job_run_history or "
        "get_job_source_paths. Pass the full table name as "
        "catalog.schema.table_name."
    )

    async def _find_jobs_for_table(table_full_name: str, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        sql = (
            "SELECT DISTINCT entity_id, entity_type, created_by, "
            "MAX(event_time) AS last_write "
            "FROM system.access.table_lineage "
            "WHERE target_table_full_name = :table_name "
            "AND entity_type IN ('WORKFLOW_RUN', 'PIPELINE_UPDATE') "
            "GROUP BY entity_id, entity_type, created_by "
            "ORDER BY last_write DESC "
            "LIMIT 10"
        )
        try:
            rows = run_sql(
                ws,
                sql,
                warehouse_id=warehouse_id,
                parameters=[{"name": "table_name", "value": table_full_name, "type": "STRING"}],
            )
        except Exception as e:
            logger.warning("find_jobs_for_table query failed for %s: %s", table_full_name, e)
            return {"table": table_full_name, "writers": [], "error": str(e)}
        return {"table": table_full_name, "writers": rows}

    return build_tool(
        _find_jobs_for_table,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)] if warehouse_id else (),
    )


# ---------------------------------------------------------------------------
# jobs_history_tool
# ---------------------------------------------------------------------------


def jobs_history_tool(
    *,
    name: str = "get_job_run_history",
    description: str | None = None,
    limit: int = 10,
) -> Any:
    """Return a tool that fetches recent run history for a Databricks Job.

    Wraps ``ws.jobs.list_runs(job_id, limit=...)`` and reshapes each run
    into ``{run_id, start_time, end_time, state, error}`` for LLM
    consumption — use after ``jobs_for_table_tool`` to check whether the
    Job populating a table has been failing.

    Usage::

        from apx_agent import Agent, jobs_history_tool

        agent = Agent(tools=[jobs_history_tool()])

    Args:
        name: Tool name shown to the LLM. Defaults to ``"get_job_run_history"``.
        description: Tool description shown to the LLM.
        limit: Maximum number of recent runs returned. Default 10.
    """
    from ._defaults import UserClientDependency
    from ._tool_factory import build_tool

    _desc = description or (
        f"Get the {limit} most recent run records for a Databricks Job — "
        "state, duration, and error message per run. Use to check if the "
        "Job writing a target table has been failing. Pass the integer Job ID."
    )

    async def _get_job_run_history(job_id: int, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            runs = list(ws.jobs.list_runs(job_id=job_id, limit=limit))
        except Exception as e:
            logger.warning("list_runs failed for job_id=%s: %s", job_id, e)
            return {"job_id": job_id, "recent_runs": [], "error": str(e)}
        return {
            "job_id": job_id,
            "recent_runs": [
                {
                    "run_id": r.run_id,
                    "start_time": r.start_time,
                    "end_time": r.end_time,
                    "state": (
                        r.state.result_state.value
                        if r.state and r.state.result_state
                        else (
                            r.state.life_cycle_state.value
                            if r.state and r.state.life_cycle_state
                            else "unknown"
                        )
                    ),
                    "error": r.state.state_message if r.state else None,
                }
                for r in runs
            ],
        }

    return build_tool(_get_job_run_history, name=name, description=_desc)


# ---------------------------------------------------------------------------
# jobs_logs_tool
# ---------------------------------------------------------------------------


def jobs_logs_tool(
    *,
    name: str = "get_job_run_logs",
    description: str | None = None,
    max_log_chars: int = 5000,
) -> Any:
    """Return a tool that fetches error output and logs from a Job run.

    Wraps ``ws.jobs.get_run_output(run_id)`` and returns ``error``,
    ``error_trace``, and a truncated ``logs`` string (capped at
    ``max_log_chars`` to stay inside the LLM context window). Use after
    ``jobs_history_tool`` identifies a failing run.

    Usage::

        from apx_agent import Agent, jobs_logs_tool

        agent = Agent(tools=[jobs_logs_tool()])

    Args:
        name: Tool name shown to the LLM. Defaults to ``"get_job_run_logs"``.
        description: Tool description shown to the LLM.
        max_log_chars: Truncation cap for the ``logs`` field. Default 5000.
    """
    from ._defaults import UserClientDependency
    from ._tool_factory import build_tool

    _desc = description or (
        "Get the error output and logs for a specific Databricks Job run. "
        f"Returns error, error_trace, and the first {max_log_chars} characters "
        "of logs. Use after get_job_run_history identifies a failed run_id."
    )

    async def _get_job_run_logs(run_id: int, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            output = ws.jobs.get_run_output(run_id=run_id)
        except Exception as e:
            logger.warning("get_run_output failed for run_id=%s: %s", run_id, e)
            return {"run_id": run_id, "error": str(e), "error_trace": None, "logs": ""}
        return {
            "run_id": run_id,
            "error": output.error,
            "error_trace": output.error_trace,
            "logs": (output.logs or "")[:max_log_chars],
        }

    return build_tool(_get_job_run_logs, name=name, description=_desc)


# ---------------------------------------------------------------------------
# jobs_source_paths_tool
# ---------------------------------------------------------------------------


def jobs_source_paths_tool(
    *,
    name: str = "get_job_source_paths",
    description: str | None = None,
) -> Any:
    """Return a tool that extracts notebook / file paths from a Job's tasks.

    Wraps ``ws.jobs.get(job_id)`` and walks ``settings.tasks``, returning a
    list of ``{type, path}`` records covering ``notebook_task``,
    ``spark_python_task``, ``dbt_task``, and ``pipeline_task``. Use to find
    the source code an agent should inspect for filter / transformation
    logic.

    Usage::

        from apx_agent import Agent, jobs_source_paths_tool

        agent = Agent(tools=[jobs_source_paths_tool()])

    Args:
        name: Tool name shown to the LLM. Defaults to ``"get_job_source_paths"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency
    from ._tool_factory import build_tool

    _desc = description or (
        "Get the notebook, python file, dbt project, or pipeline references "
        "used by each task in a Databricks Job. Returns a list of "
        "{type, path} records. Use to identify which source files implement "
        "the transformation feeding a target table."
    )

    async def _get_job_source_paths(job_id: int, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            job = ws.jobs.get(job_id=job_id)
        except Exception as e:
            logger.warning("jobs.get failed for job_id=%s: %s", job_id, e)
            return {"job_id": job_id, "name": None, "tasks": [], "error": str(e)}
        raw_tasks = job.settings.tasks if job.settings else None
        tasks: list[dict[str, Any]] = []
        for task in (raw_tasks or []):
            if task.notebook_task:
                tasks.append({"type": "notebook", "path": task.notebook_task.notebook_path})
            elif task.spark_python_task:
                tasks.append({"type": "python", "path": task.spark_python_task.python_file})
            elif task.dbt_task:
                tasks.append({"type": "dbt", "project_dir": task.dbt_task.project_directory})
            elif task.pipeline_task:
                tasks.append({"type": "pipeline", "pipeline_id": task.pipeline_task.pipeline_id})
        return {
            "job_id": job_id,
            "name": job.settings.name if job.settings else None,
            "tasks": tasks,
        }

    return build_tool(_get_job_source_paths, name=name, description=_desc)


# ---------------------------------------------------------------------------
# Convenience toolkit
# ---------------------------------------------------------------------------


def jobs_tools(*, warehouse_id: str | None = None) -> list[Any]:
    """Return the full Databricks Jobs introspection toolkit as a list.

    Bundles the four jobs factories in one call — convenient for agents
    that want the whole triage surface (table -> Job -> runs -> logs ->
    source paths) without per-factory wiring.

    Usage::

        from apx_agent import Agent, jobs_tools

        agent = Agent(tools=jobs_tools(warehouse_id="abc123"))

    Args:
        warehouse_id: Optional SQL warehouse pin forwarded to
            ``jobs_for_table_tool`` (the only member that runs SQL). When
            omitted, that tool auto-discovers a warehouse per call.

    Returns:
        A list of four tool callables ready to pass to ``Agent(tools=...)``.
    """
    return [
        jobs_for_table_tool(warehouse_id=warehouse_id),
        jobs_history_tool(),
        jobs_logs_tool(),
        jobs_source_paths_tool(),
    ]

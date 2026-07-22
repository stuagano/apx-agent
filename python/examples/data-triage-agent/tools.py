"""Data triage agent tools.

Lineage, jobs, GitHub (stubbed), and Genie Space query tools. Also a small
SQL helper that's used by both the investigation pipeline and the general
fallback agent.

Data presence checks and Delta forensics are NOT here — those are delegated
to the data-inspector sub-agent via A2A (DATA_INSPECTOR_URL).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from databricks.sdk.service.dashboards import MessageStatus

from apx_agent import Dependencies
from databricks_tools_core.sql import SQLExecutionError, execute_sql

Workspace = Dependencies.Workspace
logger = logging.getLogger(__name__)

DATA_INSPECTOR_URL = os.environ.get("DATA_INSPECTOR_URL", "http://localhost:9000")


# ---------------------------------------------------------------------------
# SQL helpers (local — no sub-agent dependency)
# ---------------------------------------------------------------------------

def _run_sql(ws: Any, sql: str) -> list[dict[str, Any]]:
    """Execute a query via databricks-tools-core, running as the caller's OBO
    identity (client=ws) with the same 30s timeout and error contract as before.
    """
    try:
        return execute_sql(sql, client=ws, timeout=30)
    except SQLExecutionError as e:
        raise RuntimeError(f"Query failed: {e}")


def run_sql_query(sql: str, ws: Workspace) -> dict[str, Any]:
    """Execute a read-only SQL query against any Databricks table.
    Use this to check if specific data exists, count rows, or inspect values.
    sql: a SELECT query (read-only)"""
    # Local because: framework sql_tool pins a warehouse at construction time; here we discover a serverless warehouse from the caller's accessible list at runtime.
    rows = _run_sql(ws, sql)
    return {"row_count": len(rows), "rows": rows[:50]}


def get_table_info(table_full_name: str, ws: Workspace) -> dict[str, Any]:
    """Get schema, row count, and data freshness for a Unity Catalog table.
    table_full_name: catalog.schema.table format"""
    # Local because: framework's table-info path goes through UC REST; here we use a SQL DESCRIBE + COUNT against any reachable warehouse, keeping the same code path as run_sql_query.
    try:
        schema_rows = _run_sql(ws, f"DESCRIBE TABLE {table_full_name}")
    except Exception as e:
        return {"error": f"Table not found or not accessible: {e}"}
    try:
        count_rows = _run_sql(ws, f"SELECT COUNT(*) as cnt FROM {table_full_name}")
        row_count = count_rows[0].get("cnt", "unknown") if count_rows else "unknown"
    except Exception:
        row_count = "unknown"
    return {
        "table": table_full_name,
        "row_count": row_count,
        "columns": schema_rows[:30],
    }


# ---------------------------------------------------------------------------
# Lineage tools
# ---------------------------------------------------------------------------

def get_table_lineage(table_full_name: str, ws: Workspace) -> dict[str, Any]:
    """Get upstream sources that feed into this table via Unity Catalog lineage.
    Use to trace where data comes from when it's missing from a target table."""
    # Local because: framework lineage_tool() uses UC's REST lineage API. We query system.access.table_lineage directly so the result can be joined with jobs/pipelines in find_jobs_for_table — UC REST doesn't expose that join.
    rows = _run_sql(ws, f"""
        SELECT
            source_table_full_name,
            entity_type,
            created_by,
            MAX(event_time) AS last_seen
        FROM system.access.table_lineage
        WHERE target_table_full_name = '{table_full_name}'
        GROUP BY source_table_full_name, entity_type, created_by
        ORDER BY last_seen DESC
        LIMIT 20
    """)
    return {"target": table_full_name, "upstream_sources": rows}


def find_jobs_for_table(table_full_name: str, ws: Workspace) -> dict[str, Any]:
    """Find Databricks jobs that write to a given table via Unity Catalog lineage.
    Returns entity IDs to follow up with get_job_run_history."""
    # Local because: needs the same system.access.table_lineage SQL surface as get_table_lineage to identify WORKFLOW_RUN / PIPELINE_UPDATE writers.
    rows = _run_sql(ws, f"""
        SELECT DISTINCT
            entity_id,
            entity_type,
            created_by,
            MAX(event_time) AS last_write
        FROM system.access.table_lineage
        WHERE target_table_full_name = '{table_full_name}'
          AND entity_type IN ('WORKFLOW_RUN', 'PIPELINE_UPDATE')
        GROUP BY entity_id, entity_type, created_by
        ORDER BY last_write DESC
        LIMIT 10
    """)
    return {"table": table_full_name, "writers": rows}


# ---------------------------------------------------------------------------
# Job tools
# ---------------------------------------------------------------------------

def get_job_run_history(job_id: int, ws: Workspace) -> dict[str, Any]:
    """Get recent run history for a Databricks job — status, duration, errors.
    Use to check if the job populating a table has been failing recently."""
    # Local because: thin wrapper over ws.jobs.list_runs shaped for LLM consumption — the framework toolkit equivalent is a separate planned promotion (see jobs_tools).
    runs = list(ws.jobs.list_runs(job_id=job_id, limit=10))
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
                    else (r.state.life_cycle_state.value if r.state and r.state.life_cycle_state else "unknown")
                ),
                "error": r.state.state_message if r.state else None,
            }
            for r in runs
        ],
    }


def get_job_run_logs(run_id: int, ws: Workspace) -> dict[str, Any]:
    """Get error output and logs from a specific failed job run.
    Use after get_job_run_history identifies a failure."""
    # Local because: thin wrapper over ws.jobs.get_run_output — planned for the jobs_tools toolkit promotion.
    output = ws.jobs.get_run_output(run_id=run_id)
    return {
        "run_id": run_id,
        "error": output.error,
        "error_trace": output.error_trace,
        "logs": (output.logs or "")[:5000],
    }


def get_job_source_paths(job_id: int, ws: Workspace) -> dict[str, Any]:
    """Get the notebook or file paths used by a job's tasks.
    Use to find the source code to inspect for filter or transformation logic."""
    # Local because: extracts notebook/python/dbt/pipeline paths from job tasks — planned for the jobs_tools toolkit promotion.
    job = ws.jobs.get(job_id=job_id)
    raw_tasks = job.settings.tasks if job.settings else None
    tasks = []
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


# ---------------------------------------------------------------------------
# GitHub tools (STUBBED)
# ---------------------------------------------------------------------------

def read_github_file(repo: str, path: str, ws: Workspace) -> dict[str, Any]:
    """Read a source file from a GitHub repository.
    Use to inspect transformation or filter logic in pipeline or API code.
    repo format: 'org/repo-name', e.g. 'my-org/my-repo'"""
    # Local because: example-specific (GitHub MCP); currently stubbed.
    return {"stub": True, "message": f"GitHub not yet configured. Would read {repo}/{path}"}


def search_github_code(repo: str, query: str, ws: Workspace) -> dict[str, Any]:
    """Search for code patterns in a GitHub repository.
    Use to find filter conditions or column names that may be excluding data.
    repo format: 'org/repo-name', query: e.g. 'status filter WHERE active'"""
    # Local because: example-specific (GitHub MCP); currently stubbed.
    return {"stub": True, "message": f"GitHub not yet configured. Would search '{query}' in {repo}"}


# ---------------------------------------------------------------------------
# Genie Space tools
# ---------------------------------------------------------------------------

def list_genie_spaces(ws: Workspace) -> dict[str, Any]:
    """List all available Genie Spaces in the workspace.
    Call this first to discover which Spaces exist and what they cover.
    Returns space IDs, titles, and descriptions — use the space_id with
    query_genie_space to ask questions."""
    # Local because: framework genie_tool(space_id) pins the space at construction. This is the dynamic pattern — the LLM picks a space at runtime by calling list_genie_spaces first, then query_genie_space.
    resp = ws.genie.list_spaces()
    spaces = []
    for s in (resp.spaces or []):
        spaces.append({
            "space_id": s.space_id,
            "title": s.title,
            "description": s.description,
        })
    return {"spaces": spaces, "count": len(spaces)}


def query_genie_space(space_id: str, question: str, ws: Workspace) -> dict[str, Any]:
    """Ask a natural language question to a specific Genie Space.
    The Space answers using its configured tables and instructions.
    Use list_genie_spaces first to find the right space_id.
    space_id: ID from list_genie_spaces
    question: plain English question (e.g. 'What upstream tables feed into gold.dr_accounts?')"""
    # Local because: paired with list_genie_spaces for runtime space selection; the framework's static genie_tool factory can't express this.
    msg = ws.genie.start_conversation_and_wait(space_id=space_id, content=question)

    result: dict[str, Any] = {
        "space_id": space_id,
        "question": question,
        "status": msg.status.value if msg.status else "unknown",
    }

    if msg.status == MessageStatus.FAILED:
        result["error"] = msg.error.message if msg.error else "Unknown error"
        return result

    for attachment in (msg.attachments or []):
        if attachment.text:
            result["answer"] = attachment.text.content
        if attachment.query and attachment.query.query:
            result["generated_sql"] = attachment.query.query
            if attachment.attachment_id:
                try:
                    qr = ws.genie.get_message_attachment_query_result(
                        space_id=space_id,
                        conversation_id=msg.conversation_id,
                        message_id=msg.message_id,
                        attachment_id=attachment.attachment_id,
                    )
                    if qr.statement_response and qr.statement_response.result:
                        cols = [
                            c.name
                            for c in (qr.statement_response.manifest.schema.columns or [])
                        ] if qr.statement_response.manifest and qr.statement_response.manifest.schema else []
                        rows = qr.statement_response.result.data_array or []
                        result["query_rows"] = [dict(zip(cols, r)) for r in rows[:30]]
                except Exception as e:
                    logger.warning("Failed to fetch Genie query result: %s", e)

    if not result.get("answer") and not result.get("query_rows"):
        result["answer"] = msg.content or "(no text response)"

    return result

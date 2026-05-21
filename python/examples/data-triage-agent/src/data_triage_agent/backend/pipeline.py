"""Deterministic investigation pipeline for data triage.

Composes six step agents into a SequentialAgent. Each step has a focused
role and a narrow set of tools. The conversation history accumulates
automatically — each agent's output becomes an assistant message visible
to the next agent in the sequence.

This replaces the single-agent-with-all-tools approach. Instead of hoping
the LLM follows a checklist, each step is *structurally guaranteed* to run.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Request

from apx_agent import Agent, SequentialAgent
from .core import Dependencies
from apx_agent._agents import BaseAgent
from apx_agent._models import Message

logger = logging.getLogger(__name__)

Workspace = Dependencies.Workspace

from .agent_router import (
    _run_sql,
    # Lineage tools
    get_table_lineage,
    find_jobs_for_table,
    # Job tools
    get_job_run_history,
    get_job_run_logs,
    get_job_source_paths,
    # Genie Space tools
    list_genie_spaces,
    query_genie_space,
    # GitHub tools
    read_github_file,
    search_github_code,
)


# ---------------------------------------------------------------------------
# SQL tools (local — no sub-agent dependency)
# ---------------------------------------------------------------------------

def run_sql_query(sql: str, ws: Workspace) -> dict[str, Any]:
    """Execute a read-only SQL query against any Databricks table.
    Use this to check if specific data exists, count rows, or inspect values.
    sql: a SELECT query (read-only)"""
    rows = _run_sql(ws, sql)
    return {"row_count": len(rows), "rows": rows[:50]}


def get_table_info(table_full_name: str, ws: Workspace) -> dict[str, Any]:
    """Get schema, row count, and data freshness for a Unity Catalog table.
    table_full_name: catalog.schema.table format"""
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


def create_investigation_pipeline(data_inspector_url: str) -> SequentialAgent:
    """Build the six-step investigation pipeline.

    Parameters
    ----------
    data_inspector_url:
        Base URL of the data-inspector sub-agent (provides run_sql_query,
        get_table_info, and Delta forensics tools via A2A).

    Returns
    -------
    SequentialAgent
        A composed agent compatible with ``create_app()``.
    """

    # ------------------------------------------------------------------
    # Step 1: Data Presence
    # Confirm what data is missing in the target table.
    # Delegates SQL and table inspection to the data-inspector sub-agent.
    # ------------------------------------------------------------------
    presence_agent = Agent(
        tools=[run_sql_query, get_table_info],
        sub_agents=[data_inspector_url],
        instructions=(
            "You are the Data Presence Investigator.\n\n"
            "Your job: confirm whether the reported data exists in the target "
            "table. Use run_sql_query and get_table_info for SQL queries. "
            "Use the data_inspector tool for Delta forensics (version "
            "history, bisect, diff). Call it with a natural language message.\n\n"
            "Be specific about:\n"
            "- Row counts (total and filtered)\n"
            "- Date ranges present vs expected\n"
            "- Specific IDs or values the user asked about\n"
            "- Table schema and partition columns\n\n"
            "End your response with a clear verdict: "
            "'DATA PRESENT', 'DATA MISSING', or 'PARTIALLY MISSING' "
            "with supporting evidence."
        ),
    )

    # ------------------------------------------------------------------
    # Step 2: Lineage Trace
    # Find upstream sources that feed the target table.
    # ------------------------------------------------------------------
    lineage_agent = Agent(
        tools=[get_table_lineage, find_jobs_for_table],
        instructions=(
            "You are the Lineage Tracer.\n\n"
            "The previous step confirmed a data presence issue. Given that "
            "context (visible in the conversation above), trace the lineage "
            "of the affected table.\n\n"
            "Steps:\n"
            "1. Call get_table_lineage on the target table to find upstream sources\n"
            "2. Call find_jobs_for_table to identify which jobs write to it\n"
            "3. If upstream sources are found, call get_table_lineage on those "
            "too — walk backward until you find where the data *does* exist\n\n"
            "Summarize:\n"
            "- The full lineage chain (source → ... → target)\n"
            "- Which jobs/pipelines are responsible for each hop\n"
            "- Where in the chain the data appears to drop off\n"
            "- The job IDs that the next step should investigate"
        ),
    )

    # ------------------------------------------------------------------
    # Step 3: Pipeline Inspector
    # Check the jobs for failures, errors, and source code paths.
    # ------------------------------------------------------------------
    pipeline_agent = Agent(
        tools=[get_job_run_history, get_job_run_logs, get_job_source_paths],
        instructions=(
            "You are the Pipeline Inspector.\n\n"
            "The previous steps identified the target table, its lineage, and "
            "the jobs that write to it. Using the job IDs from the lineage "
            "findings (visible in the conversation above):\n\n"
            "1. Call get_job_run_history for each relevant job — look for "
            "recent failures or state changes\n"
            "2. For any failed runs, call get_job_run_logs to get error details\n"
            "3. Call get_job_source_paths to identify the notebook or code files "
            "that the next step should inspect\n\n"
            "Summarize:\n"
            "- Job run success/failure pattern over recent runs\n"
            "- Specific error messages from any failures\n"
            "- Source code paths (notebooks, Python files, dbt projects) to inspect\n"
            "- Whether the failures correlate with the data gap timeline"
        ),
    )

    # ------------------------------------------------------------------
    # Step 4: Genie Query
    # Ask domain-specific questions via Genie Spaces.
    # ------------------------------------------------------------------
    genie_agent = Agent(
        tools=[list_genie_spaces, query_genie_space],
        instructions=(
            "You are the Domain Context Analyst.\n\n"
            "The investigation so far has identified a data gap, traced "
            "lineage, and checked pipeline health. Now add domain context "
            "using Genie Spaces.\n\n"
            "1. Call list_genie_spaces to discover available Spaces\n"
            "2. Pick the Space whose description best matches the data domain "
            "in question — do NOT guess space IDs\n"
            "3. Ask 1-2 targeted questions that might explain the data gap, "
            "e.g. 'When was the last time [table] was refreshed?' or "
            "'What business rules filter records from [table]?'\n\n"
            "If no Genie Spaces are relevant, say so and move on.\n\n"
            "Summarize any additional context or business rules discovered."
        ),
    )

    # ------------------------------------------------------------------
    # Step 5: Code Inspector
    # Check filter logic and transformation rules in source code.
    # ------------------------------------------------------------------
    code_agent = Agent(
        tools=[read_github_file, search_github_code],
        instructions=(
            "You are the Code Inspector.\n\n"
            "Using the source code paths from the Pipeline Inspector step and "
            "any column names or filter conditions mentioned earlier:\n\n"
            "1. Search for relevant filter conditions, WHERE clauses, or "
            "transformation rules that might exclude the missing data\n"
            "2. Read specific files identified in the pipeline source paths\n"
            "3. Look for recent changes to filter logic that correlate with "
            "the data gap timeline\n\n"
            "If GitHub is not yet configured (tools return stubs), note what "
            "files and patterns *should* be inspected and move on.\n\n"
            "Summarize:\n"
            "- Any filter conditions that could exclude the data\n"
            "- Transformation logic that might drop or modify records\n"
            "- Code paths that warrant manual review"
        ),
    )

    # ------------------------------------------------------------------
    # Step 6: Synthesis
    # Produce a plain-language root cause summary.
    # ------------------------------------------------------------------
    synthesis_agent = Agent(
        tools=[],
        instructions=(
            "You are the Root Cause Analyst.\n\n"
            "The full investigation is complete. The conversation above "
            "contains findings from five investigation steps:\n"
            "1. Data Presence — what is missing\n"
            "2. Lineage Trace — where the data flows from\n"
            "3. Pipeline Inspector — job health and errors\n"
            "4. Genie Query — domain context\n"
            "5. Code Inspector — filter and transformation logic\n\n"
            "Synthesize everything into a clear, actionable root cause report.\n\n"
            "Structure your response as:\n\n"
            "## What is missing\n"
            "Specific data, row counts, date ranges, IDs.\n\n"
            "## Where it was dropped\n"
            "The exact point in the lineage chain where data disappears.\n\n"
            "## Why\n"
            "Root cause — job failure, filter logic, schema change, etc.\n\n"
            "## Recommended action\n"
            "Concrete next steps to restore the data.\n\n"
            "## Confidence\n"
            "HIGH / MEDIUM / LOW with explanation of what is confirmed vs "
            "hypothesized.\n\n"
            "Use plain language. Cite specific table names, job IDs, "
            "timestamps, and error messages from the investigation."
        ),
    )

    # ------------------------------------------------------------------
    # Compose into the pipeline
    # ------------------------------------------------------------------
    investigation_pipeline = SequentialAgent(
        agents=[
            presence_agent,
            lineage_agent,
            pipeline_agent,
            genie_agent,
            code_agent,
            synthesis_agent,
        ],
        instructions=(
            "You are investigating why data is missing from a Databricks table "
            "or downstream API. Follow the six investigation steps in order. "
            "Each step builds on the findings of the previous steps."
        ),
    )

    # ------------------------------------------------------------------
    # General agent for non-investigation queries
    # ------------------------------------------------------------------
    general_agent = Agent(
        tools=[],
        sub_agents=[data_inspector_url],
        instructions=(
            "You are a data triage assistant for Databricks. You help with "
            "questions about tables, pipelines, jobs, and data quality.\n\n"
            "You have tools — USE THEM. Don't describe what you would do, "
            "actually call the tools and show real results.\n\n"
            "**Discovery questions** (\"what tables are there?\", \"do we have "
            "any billing data?\", \"what catalogs exist?\") — delegate to "
            "data_inspector. It has list_catalogs, list_schemas, list_tables, "
            "and search_tables. Do NOT use list_genie_spaces for this — "
            "Genie Spaces are curated dashboards, not a list of all tables.\n\n"
            "**Table queries** — schemas, row counts, specific record lookups, "
            "Delta forensics (bisect, diff, audit) — also delegate to "
            "data_inspector. Send a natural language message describing what "
            "you need.\n\n"
            "**Lineage and jobs** — use the local tools below.\n\n"
            "Available tools:\n"
            "- data_inspector: Sub-agent for table discovery, queries, schemas,\n"
            "  Delta forensics. Call with a natural-language message like\n"
            "  'List tables in <catalog>.<schema>'\n"
            "  or 'Find tables whose names mention billing' or\n"
            "  'Check if CUST-0011 exists in catalog.schema.table'\n"
            "- get_table_lineage: Upstream sources via Unity Catalog\n"
            "- find_jobs_for_table: Which jobs write to a table\n"
            "- get_job_run_history: Recent job runs, success/failure\n"
            "- get_job_run_logs: Error output from failed runs\n"
            "- get_job_source_paths: Notebook/file paths for a job\n"
            "- list_genie_spaces / query_genie_space: For querying curated\n"
            "  Genie Spaces only. NOT for discovering tables.\n"
            "- read_github_file / search_github_code: Source code inspection\n\n"
            "For general questions (what can you do, hello, etc.) answer directly."
        ),
    )

    # ------------------------------------------------------------------
    # Deterministic router: investigation → pipeline, else → general
    # No LLM call for routing — keyword match is instant.
    # ------------------------------------------------------------------
    return _DeterministicRouter(
        investigation=investigation_pipeline,
        general=general_agent,
    )


_INVESTIGATION_KEYWORDS = [
    # Data absence
    "missing", "not showing", "not in", "disappeared", "dropped",
    "can't find", "cannot find", "where did", "why isn't", "why is not",
    "not appearing", "not updated", "not returning", "not available",
    "not exist", "no data", "empty table", "zero rows", "no rows",
    "data gap", "data loss",
    # Investigation triggers
    "investigate", "triage", "root cause", "debug", "diagnose",
    "what happened", "why did", "why is", "why are", "why was",
    # Pipeline/job failures
    "fail", "failed", "failure", "error", "broke", "broken",
    "pipeline down", "job failed", "not running", "stuck",
    # Freshness/staleness
    "stale", "outdated", "not refreshed", "last updated", "behind",
    "delayed", "late", "hasn't run",
]


class _DeterministicRouter(BaseAgent):
    """Routes queries without an LLM call — keyword match is instant.

    Investigation keywords → full pipeline.
    Everything else → general agent.
    """

    def __init__(self, investigation: BaseAgent, general: BaseAgent) -> None:
        self._investigation = investigation
        self._general = general

    def _select(self, messages: list[Message]) -> BaseAgent:
        text = " ".join(
            m.content.lower()
            for m in messages
            if m.role == "user"
        )
        if any(kw in text for kw in _INVESTIGATION_KEYWORDS):
            logger.info("Router → investigation pipeline")
            return self._investigation
        logger.info("Router → general agent")
        return self._general

    async def run(self, messages: list[Message], request: Request) -> str:
        return await self._select(messages).run(messages, request)

    async def stream(
        self, messages: list[Message], request: Request
    ) -> AsyncGenerator[str, None]:
        async for chunk in self._select(messages).stream(messages, request):
            yield chunk

    def get_tool_routers(self):
        return (
            self._investigation.get_tool_routers()
            + self._general.get_tool_routers()
        )

    def collect_tools(self):
        return (
            self._investigation.collect_tools()
            + self._general.collect_tools()
        )

    async def fetch_remote_tools(self):
        return (
            await self._investigation.fetch_remote_tools()
            + await self._general.fetch_remote_tools()
        )


# Module-level agent. Lives here (not in agent_router.py) because pipeline.py
# already imports from agent_router; placing the construction there created a
# circular import. App.py and other consumers import `agent` from .pipeline.
import os as _os

DATA_INSPECTOR_URL = _os.environ.get("DATA_INSPECTOR_URL", "http://localhost:9000")
agent = create_investigation_pipeline(DATA_INSPECTOR_URL)

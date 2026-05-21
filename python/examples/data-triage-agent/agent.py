"""data-triage-agent: Deterministic 6-step SequentialAgent + general fallback.

Tool functions live in ``tools.py``; this module composes them into a
SequentialAgent investigation pipeline plus a general-purpose fallback,
wired via :class:`apx_agent.KeywordRouter` — substring-based routing with
no LLM round trip. The router compiles cleanly via ``compile_to_*`` for
both Model Serving and Apps targets; ``agent_server/start_server.py`` no
longer needs to two-compile or dispatch at the request layer.
"""
from __future__ import annotations

import os

from apx_agent import Agent, KeywordRouter, SequentialAgent

from prompts import (
    CODE,
    GENERAL,
    GENIE,
    LINEAGE,
    PIPELINE,
    PIPELINE_TOP,
    PRESENCE,
    SYNTHESIS,
)
from tools import (
    find_jobs_for_table,
    get_job_run_history,
    get_job_run_logs,
    get_job_source_paths,
    get_table_info,
    get_table_lineage,
    list_genie_spaces,
    query_genie_space,
    read_github_file,
    run_sql_query,
    search_github_code,
)

def create_investigation_pipeline(data_inspector_url: str) -> KeywordRouter:
    """Build the six-step investigation pipeline + general fallback router."""

    # Step 1: Data Presence — confirm what data is missing.
    # Delegates SQL and Delta forensics to the data-inspector sub-agent.
    presence_agent = Agent(
        instructions=PRESENCE,
        tools=[run_sql_query, get_table_info],
        sub_agents=[data_inspector_url],
    )

    # Step 2: Lineage Trace — find upstream sources.
    lineage_agent = Agent(
        instructions=LINEAGE,
        tools=[get_table_lineage, find_jobs_for_table],
    )

    # Step 3: Pipeline Inspector — check jobs for failures and source paths.
    pipeline_agent = Agent(
        instructions=PIPELINE,
        tools=[get_job_run_history, get_job_run_logs, get_job_source_paths],
    )

    # Step 4: Genie Query — domain context via curated Genie Spaces.
    genie_agent = Agent(
        instructions=GENIE,
        tools=[list_genie_spaces, query_genie_space],
    )

    # Step 5: Code Inspector — filter logic + transformation rules.
    code_agent = Agent(
        instructions=CODE,
        tools=[read_github_file, search_github_code],
    )

    # Step 6: Synthesis — root-cause report.
    synthesis_agent = Agent(
        instructions=SYNTHESIS,
        tools=[],
    )

    investigation_pipeline = SequentialAgent(
        agents=[
            presence_agent,
            lineage_agent,
            pipeline_agent,
            genie_agent,
            code_agent,
            synthesis_agent,
        ],
        instructions=PIPELINE_TOP,
    )

    # General agent for non-investigation queries.
    general_agent = Agent(
        instructions=GENERAL,
        tools=[],
        sub_agents=[data_inspector_url],
    )

    return KeywordRouter(
        branches=[("investigation", investigation_pipeline, INVESTIGATION_KEYWORDS)],
        default=general_agent,
    )


INVESTIGATION_KEYWORDS = [
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


DATA_INSPECTOR_URL = os.environ.get("DATA_INSPECTOR_URL", "http://localhost:9000")
agent = create_investigation_pipeline(DATA_INSPECTOR_URL)

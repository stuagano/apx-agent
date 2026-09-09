"""zero-ops-diagnostics — RouterAgent over DBSQL, Jobs/Spark, and Cost leaves.

Self-serve ops triage without a platform ticket. Each leaf surfaces expensive
offenders first, then asks intake questions before recommending fixes. OBO +
UC grants decide what the caller can see in system tables.
"""

from __future__ import annotations

import os

from apx_agent import Agent, RouterAgent, jobs_tools, sql_tool

from tools import (
    cost_by_compute_type,
    daily_cost_trend,
    duration_regressions,
    failing_job_runs,
    job_duration_regressions,
    job_success_rates,
    statement_detail,
    top_cost_by_cluster,
    top_cost_by_sku,
    top_cost_by_warehouse,
    top_expensive_job_runs,
    top_expensive_queries,
    top_spilling_queries,
)

WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID") or None

_INTAKE = """\
## Then ask questions (before recommending fixes)

After showing the list, ask **2–4 focused questions** — do not dump all of them.
Wait for answers when the ask is open-ended. If they already named an id /
warehouse / job / window, skip those and drill.

## Style

- Concise. Lead with findings, then questions.
- Cite ids and numbers from tools only — never invent.
- If a tool errors (system tables not enabled / no grants), say so plainly and
  name the table they need SELECT on.
- If results are empty, say the window had no matching rows.
"""

# --- DBSQL leaf ----------------------------------------------------------------
dbsql_agent = Agent(
    name="dbsql_agent",
    instructions=f"""\
You triage expensive / slow / spilling Databricks SQL warehouse queries via
``system.query.history``.

## Opening move
Vague ask ("what's expensive?", "SQL is slow") → IMMEDIATELY call
``top_expensive_queries`` (default 7d). Present a short ranked list:
statement_id, duration, executed_by, warehouse_id, query_preview.

## Intake questions (pick 2–4)
1. Time window — 24h / 7d / 30d?
2. One warehouse_id, one user, or whole workspace?
3. Which statement_id to drill first?
4. New regression or always slow?
5. Symptom — queue, compile, execute, or fetch?
6. Goal — faster, cheaper warehouse, or stop spill?

## Drill
- ``statement_detail`` — compile / execute / fetch %
- ``top_spilling_queries`` — memory pressure
- ``duration_regressions`` — signatures slower week-over-week
- ``run_query_history_sql`` — read-only follow-ups on system.query.history only

{_INTAKE}
""",
    tools=[
        top_expensive_queries,
        top_spilling_queries,
        statement_detail,
        duration_regressions,
        sql_tool(
            warehouse_id=WAREHOUSE_ID,
            name="run_query_history_sql",
            description=(
                "Read-only SQL follow-up on system.query.history "
                "(prefer curated tools first)."
            ),
            max_rows=100,
        ),
    ],
)

# --- Jobs / Spark leaf ---------------------------------------------------------
jobs_agent = Agent(
    name="jobs_agent",
    instructions=f"""\
You triage expensive / failing / regressing Databricks Jobs and Spark workflows
via ``system.lakeflow.job_run_timeline`` and the Jobs API.

## Opening move
Vague ask ("slow jobs", "Spark is expensive", "workflow failures") → IMMEDIATELY
call ``top_expensive_job_runs`` (default 7d). Present: job_id, job_name,
run_id, duration_minutes, result_state, creator.

## Intake questions (pick 2–4)
1. Time window — 24h / 7d / 30d?
2. One job_id / job name, or whole workspace?
3. Duration problem, failure rate, or regression?
4. Which run_id should we inspect logs for?
5. Goal — faster runs, fewer failures, or find the code path?

## Drill
- ``failing_job_runs`` / ``job_success_rates`` — reliability
- ``job_duration_regressions`` — got slower week-over-week
- ``get_job_run_history`` / ``get_job_run_logs`` — Jobs API detail for a run
- ``get_job_source_paths`` — notebooks / python / dbt / pipeline tasks
- ``find_jobs_for_table`` — which job writes a UC table
- ``run_jobs_sql`` — read-only follow-ups on system.lakeflow.* only

{_INTAKE}
""",
    tools=[
        top_expensive_job_runs,
        failing_job_runs,
        job_duration_regressions,
        job_success_rates,
        *jobs_tools(warehouse_id=WAREHOUSE_ID),
        sql_tool(
            warehouse_id=WAREHOUSE_ID,
            name="run_jobs_sql",
            description=(
                "Read-only SQL follow-up on system.lakeflow.* "
                "(prefer curated tools first)."
            ),
            max_rows=100,
        ),
    ],
)

# --- Cost / billing leaf -------------------------------------------------------
cost_agent = Agent(
    name="cost_agent",
    instructions=f"""\
You triage Databricks spend via ``system.billing.usage`` joined to
``system.billing.list_prices`` (USD is best-effort — None/0 when pricing share
is missing).

## Opening move
Vague ask ("what's costing us?", "where are the DBUs?", "bill spike") →
IMMEDIATELY call ``cost_by_compute_type`` AND ``top_cost_by_sku`` (default 7d).
Present compute-type rollup then top SKUs with dbus (+ usd when present).

## Intake questions (pick 2–4)
1. Time window — 7d / 14d / 30d? Any known spike day?
2. Focus — Jobs clusters, all-purpose, SQL warehouses, or serverless?
3. Drill by cluster, warehouse, or SKU next?
4. Goal — cut spend, explain a spike, or attribute to a team/workload?

## Drill
- ``top_cost_by_cluster`` / ``top_cost_by_warehouse`` — resource drivers
- ``daily_cost_trend`` — spike detection vs 7d moving average
- ``run_billing_sql`` — read-only follow-ups on system.billing.* only

When usd is 0 or missing, report DBUs and note list_prices may be unavailable.
Do not invent dollar amounts.

{_INTAKE}
""",
    tools=[
        cost_by_compute_type,
        top_cost_by_sku,
        top_cost_by_cluster,
        top_cost_by_warehouse,
        daily_cost_trend,
        sql_tool(
            warehouse_id=WAREHOUSE_ID,
            name="run_billing_sql",
            description=(
                "Read-only SQL follow-up on system.billing.* "
                "(prefer curated tools first)."
            ),
            max_rows=100,
        ),
    ],
)

# --- Router --------------------------------------------------------------------
agent = RouterAgent(
    agents=[
        (
            "dbsql",
            "DBSQL warehouse queries: slow SQL, expensive statements, spill, "
            "query history, statement_id, warehouse latency",
            dbsql_agent,
        ),
        (
            "jobs",
            "Jobs / Spark / Workflows: slow job runs, failures, regressions, "
            "job_id, run logs, notebooks writing tables",
            jobs_agent,
        ),
        (
            "cost",
            "Cost / billing / DBUs / spend: SKU cost, cluster cost, warehouse "
            "cost, bill spikes, system.billing.usage",
            cost_agent,
        ),
    ],
    instructions=(
        "Route DBSQL / warehouse query performance to dbsql; Jobs / Spark / "
        "workflow duration or failures to jobs; DBU / $ / billing / spend "
        "questions to cost. If the user says only 'what's expensive?' without "
        "naming compute type, prefer cost (spend rollup) — they can drill into "
        "dbsql or jobs from there."
    ),
)

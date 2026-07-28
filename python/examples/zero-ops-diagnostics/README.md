# zero-ops-diagnostics

Self-serve **expensive-workload triage** without a platform ops ticket. A
`RouterAgent` fans out to three leaves over system tables (OBO + UC grants):

```
                         ┌─────────────────────────────────────┐
  "SQL is slow"      ───▶│ dbsql  — system.query.history        │
                         │                                      │
  "jobs are slow"    ───▶│ jobs   — system.lakeflow.* + Jobs API│
         RouterAgent ────┤                                      │
  "what's costing us?"──▶│ cost   — system.billing.usage        │
                         └─────────────────────────────────────┘
```

Each leaf **shows top offenders first**, then asks **2–4 intake questions**
before recommending fixes.

## What it demonstrates
- `RouterAgent` over three focused diagnostic leaves
- Curated tools over `system.query.history`, `system.lakeflow.*`, `system.billing.*`
- Jobs API drill (`jobs_tools`) for run history / logs / source paths
- Intake-style prompting: evidence → questions → recommendations
- Apps deploy with `sql` OBO scope

## Prerequisites
- Serverless SQL warehouse (or set `WAREHOUSE_ID`)
- SELECT on the system tables you want to use:
  - `system.query.history` (DBSQL leaf)
  - `system.lakeflow.job_run_timeline` + `system.lakeflow.jobs` (Jobs leaf)
  - `system.billing.usage` (+ optional `system.billing.list_prices` for USD)
- Foundation Model endpoint (default `databricks-claude-sonnet-4-6`)

## Run
```bash
uv sync
uv run quickstart
export WAREHOUSE_ID=<id>   # optional

uv run apx run --reload
# → http://localhost:8000/_apx/agent
# try:
#   "what are the most expensive queries?"
#   "which jobs took longest this week?"
#   "where are our DBUs going?"
#   "show me queries that spill"
#   "which jobs got slower this week?"
#   "top clusters by cost"
```

## Deploy
```bash
apx deploy --target apps
```

## Leaves & tools

| Leaf | Opening tool | Drill |
|------|--------------|-------|
| **dbsql** | `top_expensive_queries` | spill, statement detail, query regressions |
| **jobs** | `top_expensive_job_runs` | failures, success rates, job regressions, `jobs_tools` |
| **cost** | `cost_by_compute_type` + `top_cost_by_sku` | cluster / warehouse cost, daily trend |

Vague **"what's expensive?"** routes to **cost** (spend rollup); name SQL or
Jobs to land on the other leaves.

## Sample intake questions
After surfacing offenders, leaves typically ask a subset of:
1. Time window (24h / 7d / 30d)?
2. One warehouse / job / cluster, or whole workspace?
3. Which id to drill first?
4. New regression or always bad?
5. Goal — faster, cheaper, fewer failures, or explain a spike?

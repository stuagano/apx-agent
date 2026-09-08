# BrickRoad + EE Agent — Design

## Goal

A local apx-agent Stuart can chat with to explore two data sources relevant to
his newly-expanded EE (Emerging Enterprise) scope: BrickRoad customer
blockers, and the EE active-pipeline UCO snapshot + forecast/org data.

## Scope

Local only — `apx-agent run` against the Logfood workspace (profile
`logfood`), no Databricks Apps deployment. Deployment is blocked today
(Logfood: no deploy rights; FE Stable: different UC metastore entirely, so no
data access) and is an explicit follow-up, not part of this change.

## Tools

Two tools, both pre-built SDK factories — no new tool-calling logic:

1. **`ask_brickroad`** — `genie_tool("01f13ee082c41985a98795628dd47d7c")`.
   Narrative answers over BrickRoad issues/blindspots/feature-matching.

2. **`run_sql`** — `sql_tool()`, warehouse auto-discovered, runs as the
   calling user (UC grants apply). Scoped via its description to:
   - `home_stuart_gano.ee_rollups.uco_snapshot` — the corrected, historized
     EE UCO pipeline snapshot (see
     `reference_ee_org_uco_query_pattern` memory), refreshed weekly by the
     `ee-uco-snapshot` DAB job.
   - `main.it_brick_road.*` — BrickRoad's native tables (issues, solutions,
     issue_use_cases). `issue_use_cases` maps BrickRoad issue IDs to UCO IDs
     directly — joins cleanly to `uco_snapshot.UCO_ID` for cross-referencing
     blockers against pipeline.
   - `main.gtm_gold.rpt_individual_obt_gtm_whales` — per-user-per-quarter
     forecast OBT. **Caveat: source dashboard is tagged "UAT Hub 2.0"** —
     treat as possibly pre-production, not a fully-blessed number yet.
   - `main.gtm_silver.individual_hierarchy_salesforce` — org hierarchy
     (`CROminus1..7name/email`) + segment (`Region_Level_1..4`, `AE_segment`).
     Joins to the forecast OBT on `user_id`/email.

## Instructions

System prompt should state the two known join keys explicitly (BrickRoad
issue → UCO via `issue_use_cases`; forecast OBT → org hierarchy via
`user_id`/email) so the agent doesn't have to rediscover them per
conversation, and should flag the UAT caveat on the forecast table.

## Auth

`DATABRICKS_CONFIG_PROFILE=logfood`. No new credentials — same access
already proven this session (Genie space + all four table paths confirmed
readable).

## Testing

No bespoke test suite. Both tools are pre-built, already-tested SDK
factories (`genie.py`, `sql_tools.py`) — this change is wiring, not new
logic. Verification is `make check` staying green (no regression) plus a
manual smoke run (`apx-agent run` + one real question against each tool).

## Discovery workflow extensions

The generic `DiscoveryWorkflow` in `apx_agent.discovery` owns the six
vendor-neutral steps: priorities, value matrices, heat map, wow selection,
discovery guide, and 3 Ws. This example owns the Databricks-specific input and
side branches:

- `DatabricksResearchProvider` gathers current BrickRoad blockers and EE
  pipeline, forecast, and organization context through `ask_brickroad` and
  `run_sql`.
- `wow_to_ucos` maps selected ideas to relevant UCOs.
- `solution_builder` produces a Databricks solution outline.
- `fetch_references` gathers grounded reference material.
- `quantify_value` produces a Guided Value Platform-style value estimate.

These are appended handoffs. They consume the completed generic discovery run
without adding Databricks, UCO, Salesforce, or internal-tool concepts to the
core workflow.

## Explicitly out of scope

- Databricks Apps deployment (needs Logfood deploy rights or Delta Sharing
  to FE Stable — separate follow-up).
- `genie_query_tool` (structured SQL-result variant) — narrative-only to
  start; trivial to add later if raw Genie query rows are ever needed.
- Scheduled/recurring report generation — this is interactive chat only.

# hubspot-complaints-agent

apx-agent project targeting Databricks Apps.

Summarizes customer complaints from a HubSpot Tickets object already synced
into Unity Catalog — chat with it interactively, or let the scheduled job
write a monthly summary row automatically.

## Expected data

A Unity Catalog table (`${APX_CATALOG}.${APX_SCHEMA}.${APX_TICKETS_TABLE}`,
default table name `tickets`) synced from HubSpot's Tickets object, with at
least these columns:

| column | meaning |
|---|---|
| `hs_object_id` | ticket ID |
| `subject` | ticket subject line |
| `content` | ticket body text |
| `hs_createdate` | when the ticket was created — defines which month a complaint belongs to |
| `hs_pipeline_stage` | ticket status/stage |

Set `APX_CATALOG` / `APX_SCHEMA` (and `APX_TICKETS_TABLE` if your table isn't
named `tickets`) before running locally or deploying — see `agent.py`.

## Setup
```bash
uv sync
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
uv run apx-agent run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI (chat, edit,
#   topology, traces, eval, setup wizard). Edit agent.py in your IDE;
#   --reload picks up changes. See docs/getting-started.md for the walkthrough.
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
uv run apx-agent deploy --target apps  # validates, deploys, runs the bundle
```

## Monthly scheduled summary

`databricks.yml` also deploys a Databricks Job, `hubspot-complaint-summary`,
scheduled for 6am UTC on the 1st of each month. It runs
`scripts/monthly_summary.py`, which:

1. Gets the exact ticket count for the previous full calendar month via
   direct SQL (deterministic — not LLM-derived).
2. Gets a qualitative theme summary from the agent via `run_once`.
3. Writes one row to `${APX_CATALOG}.${APX_SCHEMA}.complaint_summaries`
   (`month DATE, ticket_count INT, summary STRING, generated_at TIMESTAMP`),
   creating the table on first run.

Run it manually (e.g. to backfill a specific month) with:

```bash
uv run monthly-summary --month 2026-06
```

Omit `--month` to summarize the previous full calendar month (the default
used by the scheduled job).

## Promoting to another environment
`databricks.yml` ships `dev` (default), `staging`, and `prod` targets. All
three default to the same workspace/catalog/schema until you customize one —
promoting is an explicit edit, not a hidden step.

The `catalog`/`schema` override below only applies to scaffolds with a data
source (`--template data`/`coworker`) — a base `LlmAgent` scaffold has no
`catalog`/`schema` variables to override, so it promotes via
`--bundle-target`/`--profile` alone.

To point `staging` at its own UC catalog/schema, add a `variables:` override
under its target in `databricks.yml`:

```yaml
targets:
  staging:
    mode: production
    variables:
      catalog: <your-staging-catalog>
      schema: <your-staging-schema>
    resources:
      apps:
        hubspot-complaints-agent:
          name: hubspot-complaints-agent
```

Then deploy to the staging workspace by pointing `--profile` at its
Databricks CLI profile:

```bash
uv run apx-agent deploy --target apps --bundle-target staging --profile <staging-profile>
```

Repeat the same recipe for `prod` (its own `variables:` override + its own
`--profile`). `--bundle-target` selects which `databricks.yml` target to
deploy; `--profile` selects which workspace credentials to use — they're
independent, so forgetting to change `--profile` when you add a `staging`
override just redeploys to the same workspace under a different catalog.

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `hubspot-complaints-agent` — Databricks bundle
> resource references like `${resources.experiments.hubspot-complaints-agent_experiment.id}`
> are easier to read with snake_case names.

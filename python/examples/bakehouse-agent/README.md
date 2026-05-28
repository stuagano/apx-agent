# bakehouse-agent

A `RouterAgent` that routes between **sales metrics** and **customer reviews** —
a showcase of `DataAgent` + multi-agent routing over Databricks' built-in
`samples.bakehouse` dataset (a bakery's sales + reviews).

```
                         ┌───────────────────────────────┐
   "how are sales?" ───▶ │ sales_agent   (DataAgent over  │
                         │ the structured sales tables)   │
   RouterAgent ──────────┤                                │
"what do customers say?"▶│ reviews_agent (DataAgent over  │
                         │ the customer-review text)      │
                         └───────────────────────────────┘
```

**Zero setup — runs immediately.** Both leaves use SQL, so on a workspace with
serverless SQL the agent works out of the box: `sql_tool` auto-discovers a
warehouse, with **no Vector Search endpoint, no index, no idle cost**. Every
leaf runs as the **calling user** — Unity Catalog grants apply per request.

## What it demonstrates
- **`DataAgent("samples", "bakehouse")`** — a governed agent over the schema.
  Pass `ws=WorkspaceClient()` to have it introspect at startup: auto-wire the
  tables as `uc_table` resources and ground its instructions in the real columns.
- **`RouterAgent`** — deterministic routing between two focused leaf agents.

## Run
```bash
uv sync
uv run quickstart            # MLflow experiment + .env
export WAREHOUSE_ID=<id>      # optional — auto-discovered if your workspace has serverless SQL

uv run apx run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI; --reload picks up agent.py edits.
# open http://localhost:8000/_apx/agent and try:
#   "total sales by franchise?"        → routed to sales_agent
#   "what do customers love about us?" → routed to reviews_agent
```

## Deploy
```bash
apx deploy --target apps
```

## Upgrade — semantic review search (optional)
The default `reviews_agent` keyword-searches the review text with SQL — zero
infra, but no semantic ranking. For production-grade retrieval, point a Vector
Search agent at the pre-chunked reviews (`samples.bakehouse.media_gold_reviews_chunked`):

1. **One-time:** create a Vector Search index over the chunked reviews (UI or
   SQL — see docs.databricks.com → Vector Search). This provisions a VS
   **endpoint** (ongoing compute) — which is why it's opt-in, not the default.
2. Set `REVIEWS_INDEX=<your.index.name>` and swap in the commented
   `vector_search_tool` version of `reviews_agent` in `agent.py`.

The sales path is unchanged either way.

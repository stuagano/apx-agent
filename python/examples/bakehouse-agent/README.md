# bakehouse-agent

A `RouterAgent` that routes between **structured sales data** and **unstructured
customer reviews** — a showcase of `DataAgent` + Vector Search + multi-agent
composition over Databricks' built-in `samples.bakehouse` dataset (a bakery's
sales + reviews).

```
                         ┌──────────────────────────────┐
   "how are sales?" ───▶ │ sales_agent  (DataAgent over  │
                         │ samples.bakehouse sales tables)│
   RouterAgent ──────────┤                                │
                         │ reviews_agent (Vector Search   │
"what do customers say?"▶│ over the pre-chunked reviews)  │
                         └──────────────────────────────┘
```

Every leaf runs as the **calling user** — Unity Catalog grants apply per request.

## What it demonstrates
- **`DataAgent("samples", "bakehouse")`** — a governed agent over the structured
  tables (transactions, customers, franchises, suppliers). Pass
  `ws=WorkspaceClient()` to have it introspect the schema at startup: auto-wire
  the tables as `uc_table` resources and ground its instructions in the real columns.
- **`vector_search_tool(...)`** — semantic retrieval over
  `samples.bakehouse.media_gold_reviews_chunked` (the reviews come pre-chunked
  for exactly this).
- **`RouterAgent`** — deterministic routing between the two leaf agents.

## Setup
```bash
uv sync
uv run quickstart            # MLflow experiment + .env
```

### Reviews path — one-time Vector Search index
The reviews leaf needs an index over the chunked reviews. Create one (UI or SQL),
then point the agent at it via `REVIEWS_INDEX`:

```sql
-- on a Vector Search endpoint, index the pre-chunked reviews
CREATE VECTOR INDEX main.default.bakehouse_reviews_idx
  ON samples.bakehouse.media_gold_reviews_chunked
  ... ;   -- see docs.databricks.com → Vector Search
```
```bash
export REVIEWS_INDEX=main.default.bakehouse_reviews_idx
export WAREHOUSE_ID=<your-sql-warehouse>   # optional; auto-discovered if unset
```

Don't want to set up an index? The **sales path works on its own**, or uncomment
the SQL-fallback `reviews_agent` in `agent.py` to query the review text directly.

## Run
```bash
uv run uvicorn agent_server.start_server:app --host 127.0.0.1 --port 8000
# open http://localhost:8000/_apx/agent and try:
#   "what were total sales by franchise?"   → routed to sales_agent
#   "what do customers love about us?"       → routed to reviews_agent
```

## Deploy
```bash
apx deploy --target apps
```

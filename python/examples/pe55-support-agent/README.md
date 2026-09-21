# pe55-support-agent

Brickfood **PE55** (BrickReady CUJ: *Build a custom agent that accesses
user-authorized data*) as one declared `Agent`. Reviews + product docs + a
policy file that is re-read every turn. OBO is the default — Task 3 is not a
code change.

## What it does

A support rep asks about customer feedback or product docs. The agent calls
`load_latest_policies` first (UC volume download as the caller), then
`run_sql` on `agent_cuj.customer_support.user_reviews` and
`vector_search` on `agent_cuj.knowledge.product_docs`. UC row-level security
on the reviews table already limits rows by the caller's region — the agent
does not write a region filter. Share the App with coworkers to prove that;
this repo does not automate the two-user ABAC walk (`#755`).

## PE55 hour-map

| CUJ task | Brickfood estimate | Here |
|---|---|---|
| 0. Learn what an agent is | 15 min | Read this README |
| 1. PoC over reviews + docs + policy | 1 hour | This example. Policy reload is the only custom tool — there is no `volume_read_tool` |
| 2. Deploy and share a UI | 15 min | Part 3 |
| 3. Switch to the end user's identity | 30 min | **Deleted.** `sql_tool` / `vector_search_tool` / `Dependencies.UserClient` already run as the caller. Coworker clicks are still people |
| 4. Reason across reviews + docs + policy | 15 min | Same agent, multi-source questions |
| 5. Stretch: Lakebase memory | 30 min | README note only — `[tool.apx.agent.session] type = "lakebase"` is a store swap |

Net vs the CUJ: the identity rewrite and most of the stitching are gone.
What remains custom is "read this volume file every turn."

See [docs/positioning.md](../../../docs/positioning.md) (220 → 15) and
[docs/safety/identity-passthrough.md](../../../docs/safety/identity-passthrough.md).

## Prerequisites

- Databricks workspace with a configured CLI profile (live data: **go/e2dogfood**)
- `SELECT` on `agent_cuj.customer_support.user_reviews`
- Access to Vector Search index `agent_cuj.knowledge.product_docs`
- `READ FILES` on volume `agent_cuj.customer_support.policy_docs`
- A SQL warehouse (serverless auto-discovered when `warehouse_id` is omitted)
- Coworkers on the same workspace if you want to run Task 3 for real

Check your assigned region (helper only — not used as a filter):

```sql
SELECT
  current_user() as user,
  agent_cuj.customer_support.get_user_regions() as allowed_regions;
```

## Part 1: Workspace setup (one-time)

No one-time workspace setup is required in this repo. The table, index, and
volume already exist on go/e2dogfood. Building those objects is out of
scope for this example.

## Part 2: Local development

### Step 1: Install

```bash
uv sync
```

### Step 2: Run the tests

The example's contract lives in the package suite (so it rides existing CI,
no extra job):

```bash
cd ../../ && uv run --frozen pytest tests/test_pe55_support_agent_example.py
```

### Step 3: Run locally

Smoke (no e2dogfood):

```bash
APX_SMOKE_MODE=1 uv run uvicorn app:app --reload
```

Live (CLI profile that can see `agent_cuj`):

```bash
uv run uvicorn app:app --reload
```

Open `http://localhost:8000/_apx/agent`. Try:

- "Show me all customer reviews from the past week"
- "Search the product docs for our refund policy"
- "Show me negative reviews mentioning refund and tell me what our current refund policy says"

## Part 3: Deploy to Databricks Apps

### Step 1: Review `app.yml`

`user_api_scopes` in `databricks.yml` lists `sql`, `files`, and
`serving.serving-endpoints`. The `vector-search` scope is derived
from the `vector_search_index` resource declaration during deploy.
Changing scopes requires users to re-authorize.

`APX_SMOKE_MODE` defaults to `"0"` in the bundle. Flip to `"1"` only if you
want a stub deploy without `agent_cuj`.

### Step 2: Deploy

```bash
databricks bundle deploy --target dev
```

Or `apx-agent agents deploy --target apps` from this directory.

### Step 3: Verify (coworkers — still people)

Share the App URL with 2–3 coworkers on go/e2dogfood. Ask each:

> What’s the average customer rating of my products?

Record in the Brickfood runthrough:

| Coworker | `get_user_regions()` | What they saw |
|---|---|---|
| you | _(run the helper SQL)_ | |
| coworker 1 | | |
| coworker 2 | | |

This example does **not** automate that check. Live two-user ABAC is `#755`.

## Configuration

| Variable | Default | What it controls |
|---|---|---|
| `APX_SMOKE_MODE` | `0` | `1` swaps reviews / docs / policy for in-process stubs |
| `AGENT_MODEL` / `APX_MODEL` | `databricks-meta-llama-3-3-70b-instruct` | LLM serving endpoint |
| `MLFLOW_EXPERIMENT_NAME` | `/Shared/pe55-support-agent` | MLflow experiment for traces |

## Tools

| Tool | Live | What it returns |
|---|---|---|
| `load_latest_policies` | `ws.files.download(...)` as `Dependencies.UserClient` | Current `latest_policies.txt`, or a usable error string |
| `run_sql` | `sql_tool(description=...)` | `{row_count, truncated, rows}` as the caller |
| `vector_search` | `vector_search_tool("agent_cuj.knowledge.product_docs")` | Up to 5 doc hits as the caller |

There is no `sql_tool("table")` factory and no `volume_file_tool`. Policy
must be a `@tool`. See [docs/tools/custom-tools.md](../../../docs/tools/custom-tools.md).

## Stretch: Lakebase (Task 5)

Not implemented. To persist chat across sessions, add a store swap — same
agent object:

```toml
[tool.apx.agent.session]
type = "lakebase"
```

See [docs/running/sessions-and-memory.md](../../../docs/running/sessions-and-memory.md).

## Project structure

```
pe55-support-agent/
├── agent.py          # Agent + policy @tool + sql/vs factories
├── app.py            # Local run entrypoint
├── app.yml           # Databricks Apps entrypoint + env
├── databricks.yml    # Bundle: OBO scopes for sql / files / VS / serving
└── pyproject.toml
```

# Entity Resolution Agent

An `apx-agent` example that shows how to build a **fuzzy-match entity resolution system** on top of Databricks Vector Search. The agent resolves incoming name+address records against a Unity Catalog table even when the input has typos, nicknames, abbreviations, familial accounts, or maiden names — cases where exact SQL search fails.

**Concrete use case:** A utility company's low-income enrollment pipeline. An applicant submits their name and address; the agent finds their utility account record and returns a confidence-scored match decision.

```
Applicant:  "Jon Smyth, 123 Main St"
Account:    "John Smith, 123 Main Street"

SQL exact match → 0 results
Vector Search   → 0.92 similarity → MATCHED ✓
```

---

## Agent Pattern — HandoffAgent (multi-agent, single app)

This example uses a **HandoffAgent**: two LlmAgents inside one Databricks App, coordinated by a handoff protocol.

```
HandoffAgent
├── Supervisor  (LlmAgent) — normalize_record, search_accounts
└── Evaluator   (LlmAgent) — evaluate_candidates, log_decision
```

"Multi-agent" in this codebase means **multiple LlmAgents inside one Databricks App** — not multiple apps. The Supervisor and Evaluator run in the same process, communicate via handoffs, and are deployed as a single unit.

The alternative is a single `LlmAgent` with all five tools — simpler but no separation of concerns between searching and evaluating. The HandoffAgent enables the Evaluator to retry the Supervisor with search hints when confidence is low.

---

## Deployment Topology — Single-app vs Multi-app

The agent supports two deployment topologies, selected by environment variable:

**Single-app (default):** `SEARCH_SERVICE_URL` is unset. The Supervisor runs VS fan-out and SQL locally inside the `entity-resolution-agent` app. One app to deploy and manage.

**Multi-app (optional):** `SEARCH_SERVICE_URL` points to a deployed `account-search-service`. The Supervisor calls the search service via HTTP. Use this when you need to scale the search tier independently from the LLM tier.

```
Single-app:                    Multi-app:
┌────────────────────┐         ┌──────────────────────┐   ┌──────────────────────┐
│ entity-resolution  │         │ entity-resolution     │──▶│ account-search-      │
│ agent              │         │ agent                 │   │ service              │
│ (VS + LLM in one) │         │ (LLM only)            │   │ (VS/SQL, no LLM)     │
└────────────────────┘         └──────────────────────┘   └──────────────────────┘
```

The `afr-enrollment-api` sibling app is a third option for deterministic batch enrollment (no LLM). It also calls `account-search-service` for the candidate search.

"Multi-app" and "multi-agent" are **independent axes** — the HandoffAgent pattern (multi-agent) works identically in single-app and multi-app deployments.

---

## The hosting model — Databricks Apps

A **Databricks App** is managed serverless compute that runs inside your Databricks workspace. Unlike a standalone web service, it shares the workspace's identity boundary, network, and Unity Catalog permissions — so the app can query VS indexes, run SQL statements, and read Delta tables with no external credential plumbing.

Key properties:
- **Identity** — The app runs as a service principal with its own workspace permissions. In production, user requests carry the caller's OAuth token, which `apx-agent` forwards to the Databricks SDK so each API call runs under the caller's identity (not a shared service account).
- **Network** — The app is colocated with UC, Vector Search, and SQL warehouses. No VPN, no cross-region latency.
- **Lifecycle** — Databricks handles HTTPS termination, compute scaling, and restarts. You deploy a directory of files; the platform runs them.

The `app.yml` file at the root of this example defines the startup command and environment variables. `databricks apps deploy` (or `uv run apx-agent deploy`) uploads the source tree and starts the app.

---

## The framework — apx-agent

**apx-agent** is the Databricks Apps development framework this repo provides. It does three things:

### 1. Agent abstractions

```python
from apx_agent import LlmAgent, HandoffAgent, Dependencies

# One agent with a tool list
supervisor = LlmAgent(
    tools=[normalize_record, search_accounts],
    system_prompt=SUPERVISOR_INSTRUCTIONS,
)

# Coordinator that routes between multiple agents
agent = HandoffAgent(
    agents={"supervisor": supervisor, "evaluator": evaluator},
    start="supervisor",
    max_handoffs=4,
)
```

`LlmAgent` wraps Claude with a tool list and system prompt. `HandoffAgent` coordinates multiple `LlmAgent`s using a handoff protocol — the active agent can transfer control to a named sibling agent.

`Dependencies.Workspace` is a type alias for the Databricks SDK `WorkspaceClient`. Tool functions that declare it as a parameter receive a per-request client automatically — no manual token handling.

### 2. Protocol surface

```python
from apx_agent import create_app

app = create_app(agent)
```

`create_app(agent)` wraps any agent in a FastAPI application and registers three routes:

| Route | Purpose |
|-------|---------|
| `POST /responses` | Invoke the agent — [Responses API](https://platform.openai.com/docs/api-reference/responses) compatible, streaming SSE or blocking JSON |
| `GET /.well-known/agent.json` | A2A discovery card — machine-readable metadata for orchestrators |
| `GET /health` | Liveness probe for load balancers |

These routes are present in every apx-agent app, regardless of whether a custom UI is added on top.

### 3. Local development tooling

```bash
uv run apx-agent deploy           # package + deploy to Databricks Apps
```

The `apx` CLI handles packaging the app as a Python wheel, copying static assets, and calling `databricks apps deploy`. During local development, `uv run uvicorn ... --reload` works without any apx-specific tooling.

---

## What you'll learn

- How to design a **gold table** with multiple embedding columns so one Delta table feeds multiple Vector Search indexes
- How to create **Delta Sync VS indexes** via the Databricks SDK, and why you need three permutations for complete coverage
- How to fan out queries across indexes and deduplicate results by record ID
- How to wire a **Supervisor → Evaluator HandoffAgent** for multi-step reasoning with retry
- How to choose between single-app and multi-app deployment topologies

---

## How the agent works

The agent uses a **Supervisor → Evaluator** pattern:

1. **Supervisor** normalizes the input and calls `search_accounts`. If `SEARCH_SERVICE_URL` is configured, this is an HTTP call to `account-search-service`; otherwise it runs VS/SQL locally.

2. **Vector Search fans out across three indexes simultaneously:**
   | Index | Source column | Catches |
   |-------|--------------|---------|
   | `*_full_idx` | `first_name last_name address` | Standard matches |
   | `*_last_addr_idx` | `last_name address` | Familial / spouse accounts |
   | `*_first_email_idx` | `first_name email` | Maiden name changes |

   Results are deduplicated by account ID, keeping the highest similarity score.

3. **Evaluator** receives the candidate shortlist and applies fuzzy reasoning: familial detection, account number exact-match boosting, confidence scoring. If confidence is below threshold it hands back to the Supervisor with a search hint and tries again (up to 4 handoffs).

4. The enrollment decision (`EXACT` / `HIGH_CONFIDENCE` / `LOW_CONFIDENCE` / `NO_MATCH`) is written to the decisions table and returned.

Entry point:
- **`POST /api/chat`** — LLM-powered reasoning for ambiguous edge cases

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | General Databricks Apps development framework — this repo (`python/`) |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog, Vector Search, SQL warehouse (see Part 1) |

---

## Quick start — DEMO_MODE (no Databricks required)

The agent ships with synthetic utility account data so you can run it locally before setting up any infrastructure:

```bash
cd entity-resolution-agent
uv sync
DEMO_MODE=true uv run uvicorn app:app --reload
```

The chat interface opens at `http://localhost:8000`. Try:

> *Match: "Jon Smyth, 123 Maple Ave Denver"*

The agent will normalize the input, call the demo search functions backed by `core/demo_data.py`, and return a confidence-scored decision — no VS index, no SQL warehouse, no Databricks connection needed.

To test the deterministic endpoint in demo mode:

```bash
DEMO_MODE=true uv run uvicorn app:app --reload &

curl -s -X POST http://localhost:8000/api/enroll \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "J. Williams", "address": "55 Oak St"}' \
  | python3 -m json.tool
```

When you're ready to connect to real data, follow Parts 1–3 below.

---

## Part 1: Workspace setup (one-time)

This section creates the data infrastructure the agent queries. Skip to [Part 2](#part-2-local-development) if your workspace already has the gold table and VS indexes.

### Why a gold table?

Vector Search requires a single source column per embedding. Your source data likely lives across multiple tables (account, party, address) with no single text field suitable for embedding. A gold table solves this by joining the source tables and pre-computing the concatenated text columns you want to embed.

See [`docs/gold-table-design.md`](docs/gold-table-design.md) for the full schema and rationale.

### Step 1: Build the gold table with DLT

The gold table joins two silver tables and creates three composite text columns — one per embedding permutation. Create a DLT pipeline in your workspace using this notebook:

```python
import dlt
from pyspark.sql import functions as F

@dlt.table(
    name="utility_account_entities",
    comment="Gold table for entity resolution — one row per account, three embed columns",
    table_properties={"delta.enableChangeDataFeed": "true"},
)
def utility_account_entities():
    acct_loc = dlt.read("prd_silver.account_location")
    party    = dlt.read("prd_silver.party")

    return (
        acct_loc
        .join(party, "account_id", "left")
        .select(
            # Identity / filter columns
            F.col("account_id"),
            F.col("tenant_id"),
            F.col("account_location_end"),
            F.col("zip_code"),
            # Raw fields (returned in search results)
            F.col("last_name"),
            F.col("first_name"),
            F.col("email"),
            F.col("service_address_line1"),
            F.col("account_number"),
            # Embedding permutations — one column per VS index
            F.concat_ws(" ", F.col("first_name"), F.col("last_name"), F.col("service_address_line1")).alias("embed_full"),
            F.concat_ws(" ", F.col("last_name"), F.col("service_address_line1")).alias("embed_last_addr"),
            F.concat_ws(" ", F.col("first_name"), F.col("email")).alias("embed_first_email"),
        )
        .filter(F.col("last_name").isNotNull() | F.col("first_name").isNotNull())
    )
```

> **Adapting to your schema:** The column names (`account_id`, `service_address_line1`, etc.) match this example's silver tables. Verify them against your actual tables and update accordingly. The embedding column names (`embed_full`, `embed_last_addr`, `embed_first_email`) must match the VS index specs in Step 3.

Run the pipeline and confirm `<catalog>.<schema>.utility_account_entities` is populated before continuing.

### Step 2: Verify Change Data Feed is enabled

Vector Search uses Delta Change Data Feed to stay in sync as records are added or updated. The DLT `table_properties` above should enable it automatically. If not, run:

```sql
ALTER TABLE <catalog>.<schema>.utility_account_entities
SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');
```

Verify with:

```sql
DESCRIBE EXTENDED <catalog>.<schema>.utility_account_entities;
-- look for delta.enableChangeDataFeed = true in Table Properties
```

### Step 3: Create a Vector Search endpoint

If your workspace doesn't already have a VS endpoint:

1. In the Databricks UI, go to **Compute → Vector Search**
2. Click **Create endpoint**
3. Give it a name (e.g., `entity-resolution`) — you'll reference this when creating indexes in Step 4
4. Wait for **Online** status (typically a few minutes)

You can also create one via the SDK:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import EndpointType

ws = WorkspaceClient()
ws.vector_search_endpoints.create_endpoint(
    name="entity-resolution",
    endpoint_type=EndpointType.STANDARD,
)
```

### Step 4: Create the three VS indexes

Each index embeds a different column from the gold table. Run this once:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    VectorIndexType,
)

ws = WorkspaceClient()

ENDPOINT     = "entity-resolution"          # your VS endpoint name from Step 3
CATALOG      = "<your-catalog>"
SCHEMA       = "<your-schema>"
SOURCE_TABLE = f"{CATALOG}.{SCHEMA}.utility_account_entities"

for embed_col, suffix in [
    ("embed_full",        "full"),        # full name + address → catches standard matches
    ("embed_last_addr",   "last_addr"),   # last name + address → catches familial accounts
    ("embed_first_email", "first_email"), # first name + email  → catches maiden name changes
]:
    ws.vector_search_indexes.create_index(
        name=f"{CATALOG}.{SCHEMA}.utility_account_entities_{suffix}_idx",
        endpoint_name=ENDPOINT,
        primary_key="account_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE_TABLE,
            pipeline_type="TRIGGERED",
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name=embed_col,
                    embedding_model_endpoint_name="databricks-gte-large-en",
                )
            ],
        ),
    )
    print(f"Created: {CATALOG}.{SCHEMA}.utility_account_entities_{suffix}_idx")
```

The initial embedding job runs in the background. Wait until all three indexes show **Online** in the Vector Search UI before proceeding — this typically takes 10–30 minutes for large tables.

> **Embedding model:** `databricks-gte-large-en` is a general-purpose sentence embedding model available in most Databricks workspaces. Swap for another Foundation Model API endpoint if needed.
>
> **Pipeline type:** `TRIGGERED` syncs on demand. Switch to `CONTINUOUS` for near-real-time freshness.

**Check index status:**

```python
for suffix in ["full", "last_addr", "first_email"]:
    idx = ws.vector_search_indexes.get_index(
        index_name=f"{CATALOG}.{SCHEMA}.utility_account_entities_{suffix}_idx"
    )
    print(f"{suffix}: {idx.status.detailed_state}")
```

---

## Part 2: Local development

### Step 1: Install

```bash
cd entity-resolution-agent
uv sync
```

### Step 2: Configure your Databricks CLI profile

```bash
databricks configure --profile my-workspace
# enter workspace URL and personal access token when prompted

databricks current-user me --profile my-workspace
# should return your user info
```

### Step 3: Create a `.env` file

```env
DATABRICKS_CONFIG_PROFILE=my-workspace
DEMO_MODE=false

# Vector Search indexes (from Part 1 Step 4)
VS_INDEX_FULL=<catalog>.<schema>.utility_account_entities_full_idx
VS_INDEX_LAST_ADDR=<catalog>.<schema>.utility_account_entities_last_addr_idx
VS_INDEX_FIRST_EMAIL=<catalog>.<schema>.utility_account_entities_first_email_idx

# Tables
UTILITY_ACCOUNT_TABLE=<catalog>.<schema>.utility_account_entities
AFR_DECISION_TABLE=<catalog>.<schema>.afr_processing
```

> `.env` is gitignored. Never commit it.

### Step 4: Run the tests

All tests mock Databricks dependencies — no live connection needed:

```bash
uv run pytest tests/ -v
```

Expected:

```
tests/test_agent_wiring.py::test_agent_is_handoff_agent PASSED
tests/test_agent_wiring.py::test_agent_has_supervisor_and_evaluator PASSED
tests/test_agent_wiring.py::test_agent_starts_with_supervisor PASSED
tests/test_agent_wiring.py::test_supervisor_has_two_tools PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_high_confidence PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_no_candidates PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_familial_flag PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_account_number_boosts_score PASSED
tests/test_evaluator_tools.py::test_log_decision_writes_sql PASSED
tests/test_supervisor_tools.py::test_normalize_record_basic PASSED
tests/test_supervisor_tools.py::test_normalize_record_initials_triggers_sql PASSED
tests/test_supervisor_tools.py::test_normalize_record_acronym_triggers_sql PASSED
tests/test_supervisor_tools.py::test_search_accounts_fans_out_across_three_indexes PASSED
tests/test_supervisor_tools.py::test_search_accounts_deduplicates_by_account_id PASSED
tests/test_supervisor_tools.py::test_search_accounts_keeps_highest_score_on_dedup PASSED
tests/test_supervisor_tools.py::test_search_accounts_sql_path_for_initials PASSED
tests/test_supervisor_tools.py::test_search_accounts_demo_mode PASSED
17 passed in 0.8s
```

### Step 5: Run locally against live data

```bash
uv run uvicorn app:app --reload
```

The chat interface opens at `http://localhost:8000`. For batch enrollment, run the sibling `afr-enrollment-api` instead.

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set real values in `app.yml`

Replace the `PLACEHOLDER` values with the names from Part 1:

```yaml
env:
  - name: DEMO_MODE
    value: "false"
  - name: VS_INDEX_FULL
    value: "catalog.schema.utility_account_entities_full_idx"
  - name: VS_INDEX_LAST_ADDR
    value: "catalog.schema.utility_account_entities_last_addr_idx"
  - name: VS_INDEX_FIRST_EMAIL
    value: "catalog.schema.utility_account_entities_first_email_idx"
  - name: UTILITY_ACCOUNT_TABLE
    value: "catalog.schema.utility_account_entities"
  - name: AFR_DECISION_TABLE
    value: "catalog.schema.afr_processing"
```

### Step 2: Deploy

```bash
uv run apx-agent deploy
```

### Step 3: Verify

```bash
databricks apps get entity-resolution-agent --profile my-workspace
# look for "state": "RUNNING"
```

Test the live app is running:

```bash
curl -s https://<your-app-url>/api/version \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)"
```

---

## API reference

### POST /api/chat

LLM-powered reasoning via the Supervisor → Evaluator HandoffAgent. Use for ambiguous edge cases — nicknames (Liz → Elizabeth), maiden names, multi-account households, or when the deterministic `afr-enrollment-api` returns `LOW_CONFIDENCE`.

For deterministic batch enrollment, see [`../afr-enrollment-api/`](../afr-enrollment-api/).

---

## Programmatic invocation — the UI is optional

`create_app(agent)` wires three routes that are always present, regardless of whether a UI is deployed:

| Route | Purpose |
|-------|---------|
| `POST /responses` | Invoke the agent — streaming SSE or blocking JSON |
| `GET /.well-known/agent.json` | A2A discovery card — name, description, tool list |
| `GET /health` | Liveness probe |

Any HTTP client, orchestrator, or agent hub can discover and call this agent without a browser.

### How `.well-known` works

`.well-known` is an [RFC 8615](https://datatracker.ietf.org/doc/html/rfc8615) convention — a reserved URL path for machine-readable metadata. Every apx-agent app exposes `GET /.well-known/agent.json`, a discovery card that describes the agent's capabilities.

apx-agent auto-generates this card from the agent's `name`, `description`, and the union of tools across all `LlmAgent`s in the `HandoffAgent`. An orchestrator or agent hub can fetch this URL to learn what the agent does and how to invoke it — without reading source code or documentation.

**The `POST /responses` route** follows the [Responses API](https://platform.openai.com/docs/api-reference/responses) spec (same as OpenAI's). This means any client that can talk to a standard Responses API endpoint — Claude Desktop, a LangGraph node, a custom orchestrator — can talk to this agent without apx-specific adapters. Responses stream as [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) when `"stream": true`.

### Discover the agent

```bash
curl https://<your-app-url>/.well-known/agent.json \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)"
```

```json
{
  "name": "entity_resolution_agent",
  "display_name": "Entity Resolution",
  "description": "Resolve and deduplicate customer/account entities ...",
  "url": "https://<your-app-url>",
  "tools": [
    {"name": "normalize_record",    "description": "Normalize a raw AFR application record..."},
    {"name": "search_accounts",     "description": "Search for matching utility accounts..."},
    {"name": "evaluate_candidates", "description": "Score and rank candidate matches..."},
    {"name": "log_decision",        "description": "Write the enrollment decision to the AFR table..."}
  ]
}
```

### Invoke directly (streaming SSE)

```bash
curl -s -X POST https://<your-app-url>/responses \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)" \
  -H "Content-Type: application/json" \
  -d '{"input": "Match: Jon Smyth, 123 Maple Ave Denver", "stream": true}'
```

### Invoke from Python

```python
import httpx, subprocess, json

token = subprocess.check_output(
    ["databricks", "auth", "token", "--profile", "my-workspace"]
).decode().strip()

with httpx.stream(
    "POST",
    "https://<your-app-url>/responses",
    headers={"Authorization": f"Bearer {token}"},
    json={"input": "Match: Jon Smyth, 123 Maple Ave Denver", "stream": True},
    timeout=60,
) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            event = json.loads(line[5:])
            if event.get("type") == "response.output_text.delta":
                print(event["delta"], end="", flush=True)
```

### Register with an agent hub

If you're running the [`../agent-hub/`](../agent-hub/) example, register this agent so it appears in the hub's registry and can be invoked from the hub's chat UI:

```bash
curl -s -X POST https://<agent-hub-url>/api/agents/register \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://<your-app-url>"}'
```

The hub crawls `/.well-known/agent.json`, stores the card, and proxies future invocations through its `/api/agents/{id}/invoke` route — no UI or browser required on either end.

---

## Configuration files

Three YAML files serve distinct purposes — don't confuse them:

### `app.yml` — Runtime config (the one you edit)

Tells Databricks Apps how to start the app and what environment variables to inject at runtime:

```yaml
command: ["uvicorn", "app:app", "--workers", "2"]
env:
  - name: VS_INDEX_FULL
    value: "catalog.schema.utility_account_entities_full_idx"
  ...
```

This is the file you fill in before deploying. Environment variables set here are injected into the app process — they're not secrets and are visible in the Databricks Apps UI. For secrets, use Databricks Secrets and reference them as `{{secrets/scope/key}}`.

### `databricks.yml` — Asset Bundle config (the build pipeline)

Defines the Databricks Asset Bundle: how to build the wheel, what to sync to the workspace, and the App resource definition:

```yaml
bundle:
  name: entity-resolution-agent

artifacts:
  default:
    build: uv build --wheel -o .build/ && ...  # builds the Python wheel

resources:
  apps:
    entity-resolution-agent-app:
      source_code_path: ./.build               # what to upload to Databricks
```

`apx-agent deploy` (and `databricks bundle deploy`) reads this file to run the build and deploy the result. You rarely edit this unless you're changing the bundle name or adding new resources (jobs, pipelines, etc.).

### `.build/` — Generated artifact (do not edit)

The `.build/` directory is created by the build step and contains the compiled wheel, a `requirements.txt`, and a copy of `app.yml`. It's gitignored. If you see `.build/app.yml`, it's a stale copy from the last deploy — the source of truth is `app.yml` at the root.

---

## Project structure

```
entity-resolution-agent/       ← this app: LLM HandoffAgent + chat UI
account-search-service/         ← sibling: standalone VS/SQL search API
afr-enrollment-api/             ← sibling: deterministic enrollment pipeline
```

### Backend files

**`app.py` — FastAPI entry point**

The top-level wiring. Calls `create_app(agent)` to get the A2A protocol surface (`POST /responses`, `GET /.well-known/agent.json`, `GET /health`), then attaches additional routers:

```python
app = create_app(agent)          # A2A protocol surface
app.include_router(router)       # /api/version, /api/current-user
app.include_router(build_dev_ui_router())  # /_apx/agent dev UI
```

The optional custom SPA (served at `/`) is mounted at the end — after the protocol routes — so `POST /responses` always takes precedence. If no built client exists, `/` redirects to the APX dev UI.

**`agent_router.py` — HandoffAgent wiring**

Instantiates the two `LlmAgent`s and assembles them into the `HandoffAgent`:

```python
agent = HandoffAgent(
    agents={"supervisor": supervisor, "evaluator": evaluator},
    start="supervisor",
    max_handoffs=4,
)
```

This is the object passed to `create_app`. One file, one responsibility: the agent topology.

**`router.py` — Application-specific routes**

Optional HTTP routes that aren't part of the A2A protocol surface:

- `GET /api/version` — returns the package version (useful for verifying which build is running)
- `GET /api/current-user` — proxies the Databricks `current-user.me()` API using the caller's OAuth token, so the UI can display who's logged in

These use `Dependencies.UserClient` — a per-request `WorkspaceClient` scoped to the *caller's* identity (not the app's service principal). You can add more routes here or remove these entirely without affecting the agent.

**`models.py` — Pydantic models**

Pydantic response schemas for the routes in `router.py`. Currently just `VersionOut`. Add models here when you add routes that need structured response validation.

**`core/supervisor.py`** — `normalize_record` and `search_accounts` tools. Handles VS fan-out, SQL fallback for initials/acronyms, and DEMO_MODE.

**`core/evaluator.py`** — `evaluate_candidates` and `log_decision` tools. Scores candidates, detects familial accounts, writes the decision to the AFR table.

**`core/demo_data.py`** — Synthetic utility accounts returned in DEMO_MODE. No Databricks connection needed.

```
src/entity_resolution_agent/
└── backend/
    ├── app.py          ← entry point: create_app + route assembly
    ├── agent_router.py ← HandoffAgent: supervisor → evaluator
    ├── router.py       ← /api/version, /api/current-user
    ├── models.py       ← Pydantic schemas for router.py
    └── core/
        ├── supervisor.py   ← normalize_record, search_accounts
        ├── evaluator.py    ← evaluate_candidates, log_decision
        └── demo_data.py    ← synthetic accounts for DEMO_MODE
tests/
├── conftest.py              ← shared fixtures, mock VS client
├── test_agent_wiring.py     ← HandoffAgent smoke tests
├── test_supervisor_tools.py ← normalize_record, search_accounts
└── test_evaluator_tools.py  ← evaluate_candidates, log_decision
```

---

## Troubleshooting

**`DEMO_MODE=true` but getting "no such module" errors**
Run `uv sync` first — the package must be installed into the venv before imports resolve.

**Vector Search index stuck in `PROVISIONING`**
The initial embedding job is still running. Check the Vector Search UI and wait for all three indexes to show `Online`. Large tables (millions of rows) can take 30+ minutes.

**`Missing VS index env vars` in search results**
One or more of `VS_INDEX_FULL`, `VS_INDEX_LAST_ADDR`, `VS_INDEX_FIRST_EMAIL` is missing from your `.env` or `app.yml`. The variable names are case-sensitive.

**`SQL failed: ...` from sql_search**
`UTILITY_ACCOUNT_TABLE` isn't accessible from the SQL warehouse. Confirm the table exists (`DESCRIBE TABLE <table>`) and your service principal has `SELECT` permission.

**Low match rates**
`databricks-gte-large-en` is optimized for semantic similarity, not character-level fuzzy matching. Common gaps: nicknames (Bob / Robert), single-character-off typos. To extend, add a nickname expansion map to `evaluate_candidates` — see the `EVALUATOR_INSTRUCTIONS` comment in `core/evaluator.py`.

**Index sync lag after bulk imports**
With `pipeline_type: TRIGGERED`, the index doesn't auto-sync. Trigger manually after large data loads:

```python
from databricks.sdk import WorkspaceClient
ws = WorkspaceClient()
for suffix in ["full", "last_addr", "first_email"]:
    ws.vector_search_indexes.sync_index(
        index_name=f"<catalog>.<schema>.utility_account_entities_{suffix}_idx"
    )
```

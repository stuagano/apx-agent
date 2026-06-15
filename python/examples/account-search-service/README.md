# Account Search Service

A standalone Databricks App that exposes utility account search as an HTTP API. Designed to be deployed independently and called by other services — the AFR enrollment pipeline, the entity-resolution LLM agent, or any other service that needs fuzzy account lookup.

## What it does

`POST /api/search` normalizes a name+address, selects the right search strategy, fans out across three Vector Search indexes, and returns deduplicated candidates.

| Index | Embedding column | Catches |
|-------|-----------------|---------|
| `_full_idx` | `first_name last_name address` | Standard name matches |
| `_last_addr_idx` | `last_name address` | Familial / spouse matches |
| `_first_email_idx` | `first_name email` | Maiden name matches |

Names with initials (J. Smith) or acronyms (ABC LLC) bypass Vector Search and use SQL ILIKE instead — these embed poorly under cosine distance.

---

## Part of a 3-app architecture

This service is one of three apps in the entity resolution system:

```
┌─────────────────────────┐
│   account-search-service │  POST /api/search
│   (VS fan-out + SQL)    │  No LLM — fast, stateless, horizontally scalable
└───────────┬─────────────┘
            │ HTTP
     ┌──────┴──────────────────────────┐
     │                                 │
┌────▼────────────────┐   ┌────────────▼──────────────────────┐
│  afr-enrollment-api  │   │     entity-resolution-agent       │
│  POST /api/enroll    │   │     POST /api/chat                │
│  (deterministic,     │   │     (LLM HandoffAgent — Supervisor │
│   no LLM, batch)    │   │      calls search service via HTTP) │
└─────────────────────┘   └───────────────────────────────────┘
```

**This app** (`account-search-service`) is the shared search layer. Both sibling apps delegate to it. See:

- [`../entity-resolution-agent/`](../entity-resolution-agent/) — LLM agent for ambiguous edge cases
- [`../afr-enrollment-api/`](../afr-enrollment-api/) — deterministic enrollment pipeline

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

The service ships with synthetic utility account data so you can test it before setting up any infrastructure:

```bash
cd account-search-service
uv sync
DEMO_MODE=true uv run uvicorn app:app --reload
```

Then:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jane Smith", "address": "123 Maple Ave"}'
```

Expected response (demo data):

```json
{
  "strategy": "vector",
  "source": "demo",
  "candidates": [
    {
      "account_id": "acct-001",
      "name": "Jane Smith",
      "address": "123 Maple Ave",
      "account_number": "acct-001",
      "score": 0.94
    }
  ]
}
```

When you're ready to connect to real data, follow Parts 1–3 below.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEMO_MODE` | No | `"true"` → synthetic data, no live Databricks needed |
| `VS_INDEX_FULL` | Yes (live) | `catalog.schema.utility_account_entities_full_idx` |
| `VS_INDEX_LAST_ADDR` | Yes (live) | `catalog.schema.utility_account_entities_last_addr_idx` |
| `VS_INDEX_FIRST_EMAIL` | Yes (live) | `catalog.schema.utility_account_entities_first_email_idx` |
| `UTILITY_ACCOUNT_TABLE` | Yes (SQL path) | `catalog.schema.utility_account_entities` |

---

## Part 1: Workspace setup (one-time)

This service uses the same gold table and VS indexes as the `entity-resolution-agent`. Follow [Part 1 in that README](../entity-resolution-agent/README.md#part-1-workspace-setup-one-time) to create the DLT gold table, enable Change Data Feed, create a VS endpoint, and create the three VS indexes. Then return here.

Once the infrastructure is in place, `account-search-service` requires no additional workspace setup beyond a configured Databricks CLI profile.

---

## Part 2: Local development

### Step 1: Install

```bash
cd account-search-service
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

# Vector Search (created in Part 1 of entity-resolution-agent)
VS_INDEX_FULL=<catalog>.<schema>.utility_account_entities_full_idx
VS_INDEX_LAST_ADDR=<catalog>.<schema>.utility_account_entities_last_addr_idx
VS_INDEX_FIRST_EMAIL=<catalog>.<schema>.utility_account_entities_first_email_idx

# Tables
UTILITY_ACCOUNT_TABLE=<catalog>.<schema>.utility_account_entities
```

> `.env` is gitignored. Never commit it.

### Step 4: Run the tests

All tests mock Databricks dependencies — no live connection needed:

```bash
uv run pytest tests/ -v
```

Expected:

```
tests/test_search.py::test_normalize_standard_name PASSED
tests/test_search.py::test_normalize_initials_triggers_sql PASSED
tests/test_search.py::test_normalize_acronym_triggers_sql PASSED
tests/test_search.py::test_vector_search_fans_out_across_three_indexes PASSED
tests/test_search.py::test_vector_search_deduplicates_by_account_id PASSED
tests/test_search.py::test_vector_search_keeps_highest_score_on_dedup PASSED
tests/test_search.py::test_demo_mode_vector_search PASSED
tests/test_search.py::test_demo_mode_sql_search PASSED
tests/test_search.py::test_search_routes_to_vector_for_normal_name PASSED
tests/test_search.py::test_search_routes_to_sql_for_initials PASSED
10 passed in 0.5s
```

### Step 5: Run locally against live data

```bash
uv run uvicorn app:app --reload
```

Test a live search:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jon Smyth", "address": "123 Main St Denver"}' \
  | python3 -m json.tool
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set real values in `app.yml`

Replace the `PLACEHOLDER` values with the names from Part 1:

```yaml
command:
  - uvicorn
  - app:app
  - --workers
  - "2"

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
```

### Step 2: Deploy

```bash
uv run apx-agent agents deploy
```

### Step 3: Verify

```bash
databricks apps get account-search-service --profile my-workspace
# look for "state": "RUNNING"
```

Test the live app:

```bash
curl -s -X POST https://<your-app-url>/api/search \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)" \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jane Smith", "address": "123 Main St"}' \
  | python3 -m json.tool
```

Copy the deployed app URL — you'll need it as `SEARCH_SERVICE_URL` when deploying `afr-enrollment-api` and `entity-resolution-agent`.

---

## API reference

### POST /api/search

**Request:**

```json
{
  "applicant_name": "Jane Smith",
  "address": "123 Main St Denver"
}
```

**Response:**

```json
{
  "strategy": "vector",
  "source": "live",
  "candidates": [
    {
      "account_id": "acct-001",
      "name": "Jane Smith",
      "address": "123 Main Street Denver",
      "account_number": "acct-001",
      "score": 0.92
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `strategy` | `"vector"` or `"sql"` — search path chosen based on input |
| `source` | `"live"` or `"demo"` |
| `candidates` | Deduplicated list, highest score per `account_id`, ordered by score descending |

---

## Project structure

```
account-search-service/
├── app.py                                     # FastAPI app entry point (uvicorn target: app:app)
├── api.py                                     # HTTP route: POST /api/search
├── models.py                                  # Pydantic models: SearchRequest, SearchResponse, Candidate
├── search.py                                  # normalize, search, vector_search, sql_search
├── demo_data.py                               # Synthetic accounts for DEMO_MODE
├── app.yml                                    # Runtime command + env vars
├── pyproject.toml                             # Package config and deps
├── databricks.yml                             # Databricks Asset Bundle config
└── tests/
    ├── conftest.py                            # Shared fixtures and mock helpers
    └── test_search.py                         # normalize, vector_search, sql_search, routing tests
```

---

## Troubleshooting

**`DEMO_MODE=true` but getting import errors**
Run `uv sync` first — the package must be installed into the venv before imports resolve.

**`Missing VS index env vars` in search results**
One or more of `VS_INDEX_FULL`, `VS_INDEX_LAST_ADDR`, `VS_INDEX_FIRST_EMAIL` is missing from `.env` or `app.yml`. The variable names are case-sensitive.

**`SQL failed: ...` from sql_search**
`UTILITY_ACCOUNT_TABLE` isn't accessible from the SQL warehouse. Confirm the table exists (`DESCRIBE TABLE <table>`) and your service principal has `SELECT` permission.

**Low match rates**
`databricks-gte-large-en` is optimized for semantic similarity, not character-level fuzzy matching. Single-character-off typos and nicknames (Bob / Robert) can fall below threshold. To extend coverage, add a SQL ILIKE fallback for low-confidence vector results.

**Index out of sync after bulk data load**
With `pipeline_type: TRIGGERED`, the index doesn't auto-sync. Trigger manually:

```python
from databricks.sdk import WorkspaceClient
ws = WorkspaceClient()
for suffix in ["full", "last_addr", "first_email"]:
    ws.vector_search_indexes.sync_index(
        index_name=f"<catalog>.<schema>.utility_account_entities_{suffix}_idx"
    )
```

# AFR Enrollment API

A standalone Databricks App that provides a deterministic enrollment decision for AFR (Affordable Rate) applications. No LLM — pure normalize → search → evaluate → log pipeline. Designed for batch processing at high throughput.

## What it does

`POST /api/enroll` accepts an AFR application (name, address, email, account number) and returns a structured enrollment decision.

**Decision categories:**

| Category | Confidence | Meaning |
|----------|-----------|---------|
| `EXACT` | ≥ 0.90 | Near-certain match |
| `HIGH_CONFIDENCE` | ≥ 0.75 | Strong match, approve |
| `LOW_CONFIDENCE` | < 0.75 | Review recommended |
| `NO_MATCH` | 0.0 | No candidate found |

---

## Part of a 3-app architecture

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

**This app** (`afr-enrollment-api`) is the deterministic batch layer — use it when throughput matters and confidence thresholds are sufficient. For ambiguous edge cases where this API returns `LOW_CONFIDENCE` (nicknames, maiden names, multi-account households), route to the `entity-resolution-agent` chat interface for LLM-powered reasoning.

See sibling apps:

- [`../account-search-service/`](../account-search-service/) — VS fan-out search as a standalone REST API
- [`../entity-resolution-agent/`](../entity-resolution-agent/) — LLM agent for ambiguous edge cases

---

## Search strategy

By default, the enrollment API runs Vector Search + SQL locally. In production, set `SEARCH_SERVICE_URL` to delegate search to the deployed `account-search-service` app — this lets search scale independently from the enrollment pipeline and avoids duplicate VS connection overhead when both apps are deployed together.

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
cd afr-enrollment-api
uv sync
DEMO_MODE=true uv run uvicorn afr_enrollment_api.backend.app:app --reload
```

Then:

```bash
curl -s -X POST http://localhost:8000/api/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "applicant_name": "Jane Smith",
    "address": "123 Maple Ave",
    "account_number": "DEN-001234"
  }' \
  | python3 -m json.tool
```

Expected response (demo data):

```json
{
  "matched": true,
  "category": "HIGH_CONFIDENCE",
  "confidence": 0.94,
  "account_id": "acct-001",
  "applicant_name": "Jane Smith",
  "address": "123 Maple Ave"
}
```

When you're ready to connect to real data, follow Parts 1–3 below.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEMO_MODE` | No | `"true"` → synthetic data, no live Databricks needed |
| `SEARCH_SERVICE_URL` | No | URL of deployed account-search-service (recommended for production); leave empty to run VS/SQL locally |
| `VS_ENDPOINT` | When no `SEARCH_SERVICE_URL` | Vector Search endpoint name |
| `VS_INDEX_FULL` | When no `SEARCH_SERVICE_URL` | `catalog.schema.utility_account_entities_full_idx` |
| `VS_INDEX_LAST_ADDR` | When no `SEARCH_SERVICE_URL` | `catalog.schema.utility_account_entities_last_addr_idx` |
| `VS_INDEX_FIRST_EMAIL` | When no `SEARCH_SERVICE_URL` | `catalog.schema.utility_account_entities_first_email_idx` |
| `UTILITY_ACCOUNT_TABLE` | When no `SEARCH_SERVICE_URL` | `catalog.schema.utility_account_entities` |
| `AFR_DECISION_TABLE` | Yes (live) | `catalog.schema.afr_processing` — must be created (see Part 1) |

---

## Part 1: Workspace setup (one-time)

### Step 1: Create the VS infrastructure

This service uses the same gold table and VS indexes as `account-search-service` and `entity-resolution-agent`. Follow [Part 1 in the entity-resolution-agent README](../entity-resolution-agent/README.md#part-1-workspace-setup-one-time) to create the DLT gold table, enable Change Data Feed, create a VS endpoint, and create the three VS indexes. Then return here.

### Step 2: Create the AFR decisions table

Before running in live mode, create the table where enrollment decisions will be written:

```sql
CREATE TABLE IF NOT EXISTS <catalog>.<schema>.afr_processing (
  application_id     STRING,
  applicant_name     STRING,
  address            STRING,
  decision           STRING,
  confidence         DOUBLE,
  matched_account_id STRING,
  decided_at         TIMESTAMP
);
```

Replace `<catalog>` and `<schema>` with your target catalog and schema. This becomes your `AFR_DECISION_TABLE` env var.

---

## Part 2: Local development

### Step 1: Install

```bash
cd afr-enrollment-api
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

# Set this to the URL of your deployed account-search-service for production.
# Leave empty to run VS/SQL search locally (useful for development).
SEARCH_SERVICE_URL=

# Required when SEARCH_SERVICE_URL is unset (local search fallback):
VS_ENDPOINT=entity-resolution
VS_INDEX_FULL=<catalog>.<schema>.utility_account_entities_full_idx
VS_INDEX_LAST_ADDR=<catalog>.<schema>.utility_account_entities_last_addr_idx
VS_INDEX_FIRST_EMAIL=<catalog>.<schema>.utility_account_entities_first_email_idx
UTILITY_ACCOUNT_TABLE=<catalog>.<schema>.utility_account_entities

# Always required:
AFR_DECISION_TABLE=<catalog>.<schema>.afr_processing
```

> `.env` is gitignored. Never commit it.
>
> **When to set `SEARCH_SERVICE_URL`:** Leave it empty during local development — the app runs VS/SQL locally which is easier to debug. Set it to the deployed `account-search-service` URL when deploying to production so search can scale independently.

### Step 4: Run the tests

All tests mock Databricks dependencies — no live connection needed:

```bash
uv run pytest tests/ -v
```

Expected:

```
tests/test_enroll.py::test_enroll_happy_path PASSED
tests/test_enroll.py::test_enroll_no_match PASSED
tests/test_enroll.py::test_enroll_missing_name_returns_422 PASSED
tests/test_enroll.py::test_demo_mode_enroll PASSED
4 passed in 0.6s
```

### Step 5: Run locally against live data

```bash
uv run uvicorn afr_enrollment_api.backend.app:app --reload
```

Test an enrollment:

```bash
curl -s -X POST http://localhost:8000/api/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "applicant_name": "Jon Smyth",
    "address": "123 Main St Denver",
    "email": "jon@example.com",
    "account_number": "DEN-001234",
    "tenant_id": "utility_a"
  }' \
  | python3 -m json.tool
```

Expected response:

```json
{
  "matched": true,
  "category": "HIGH_CONFIDENCE",
  "confidence": 0.92,
  "account_id": "acct-001",
  "applicant_name": "Jon Smyth",
  "address": "123 Main St Denver"
}
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set real values in `app.yml`

Replace the `PLACEHOLDER` values. Set `SEARCH_SERVICE_URL` to the URL of your deployed `account-search-service` — this is the recommended production configuration. The VS/SQL env vars are still required as fallback for cases where the search service is unavailable.

```yaml
command:
  - uvicorn
  - afr_enrollment_api.backend.app:app
  - --workers
  - "2"

env:
  - name: DEMO_MODE
    value: "false"
  - name: SEARCH_SERVICE_URL
    value: "https://account-search-service-<workspace>.databricksapps.com"
  - name: VS_ENDPOINT
    value: "entity-resolution"
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

> Deploy `account-search-service` first to get its URL before deploying this app.

### Step 2: Deploy

```bash
uv run apx deploy
```

### Step 3: Verify

```bash
databricks apps get afr-enrollment-api --profile my-workspace
# look for "state": "RUNNING"
```

Test the live app:

```bash
curl -s -X POST https://<your-app-url>/api/enroll \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)" \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jane Smith", "address": "123 Main St"}' \
  | python3 -m json.tool
```

---

## API reference

### POST /api/enroll

**Request:**

```json
{
  "applicant_name": "Jane Smith",
  "address": "123 Main St Denver",
  "email": "jane@example.com",
  "account_number": "DEN-001234",
  "tenant_id": "utility_a"
}
```

All fields except `applicant_name` are optional and improve match accuracy when provided.

**Response:**

```json
{
  "matched": true,
  "category": "HIGH_CONFIDENCE",
  "confidence": 0.92,
  "account_id": "acct-001",
  "applicant_name": "Jane Smith",
  "address": "123 Main St Denver"
}
```

| Field | Description |
|-------|-------------|
| `matched` | `true` if `EXACT` or `HIGH_CONFIDENCE`; `false` otherwise |
| `category` | `EXACT` / `HIGH_CONFIDENCE` / `LOW_CONFIDENCE` / `NO_MATCH` |
| `confidence` | Float 0.0–1.0; score of the best candidate |
| `account_id` | Matched account ID, or `null` if no match |

---

## Project structure

```
afr-enrollment-api/
├── app.yml                                    # Runtime command + env vars
├── pyproject.toml                             # Package config and deps
├── databricks.yml                             # Databricks Asset Bundle config
├── src/afr_enrollment_api/
│   └── backend/
│       ├── app.py                             # FastAPI app entry point
│       ├── router.py                          # HTTP routes: /api/enroll, /version
│       ├── models.py                          # Pydantic models: EnrollRequest, EnrollResponse
│       └── core/
│           ├── search_client.py               # HTTP client for account-search-service (or local fallback)
│           ├── evaluator.py                   # Candidate scoring: confidence, category, familial detection
│           └── demo_data.py                   # Synthetic accounts for DEMO_MODE
└── tests/
    ├── conftest.py                            # Shared fixtures and mock helpers
    └── test_enroll.py                         # POST /api/enroll happy path, no-match, 422, demo mode
```

---

## Troubleshooting

**`DEMO_MODE=true` but getting import errors**
Run `uv sync` first — the package must be installed into the venv before imports resolve.

**`AFR_DECISION_TABLE` write fails**
The table must exist before live mode runs — see Part 1, Step 2 for the `CREATE TABLE` statement. Confirm your service principal has `MODIFY` permission on the table.

**`SEARCH_SERVICE_URL` set but search fails**
Verify the search service is running: `curl https://<search-url>/api/version`. If it returns 401, you need to pass a Databricks bearer token — the enrollment API does this automatically using the workspace SDK credentials, but the URL must be reachable from the app's compute.

**Low match rates**
If too many enrollments land in `LOW_CONFIDENCE`, consider routing them to the `entity-resolution-agent` for LLM-powered evaluation. The agent handles nicknames, maiden names, and multi-account households that fall below the deterministic threshold.

**Decisions not appearing in the table**
The SQL warehouse executes the INSERT. Check that `UTILITY_ACCOUNT_TABLE` and `AFR_DECISION_TABLE` are in the same catalog accessible to the warehouse, and that the warehouse is running (not terminated due to auto-stop).

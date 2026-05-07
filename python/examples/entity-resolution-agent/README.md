# Entity Resolution Agent (enroLLMent)

An AI agent that replaces Uplight's deterministic SQL + manual review process for utility account matching in the AFR (Affordable Rate) enrollment pipeline. A new AFR application comes in with a name and address; the agent finds the matching utility account record even when the applicant's information has typos, nicknames, abbreviations, or edge cases like maiden names and familial accounts.

**Example:**

*An applicant submits: "Jon Smyth, 123 Main St"*
*The utility record is: "John Smith, 123 Main Street"*

An exact SQL search returns 0 results. This agent's Vector Search returns the correct record at 0.92 similarity, the Evaluator confirms it, and the enrollment is approved — automatically, without a manual review queue.

---

## How it works

The agent uses a **Supervisor → Evaluator** pattern:

1. **Supervisor** normalizes the application, detects whether the name contains initials or acronyms (which embed poorly), and searches for candidates. For standard names it uses Vector Search; for initials/acronyms it falls back to SQL ILIKE.
2. Vector Search fans out across three indexes simultaneously: full name+address, last name+address (for familial/spouse matches), and first name+email (for maiden name matches). Results are deduplicated by account ID, keeping the highest similarity score.
3. **Evaluator** receives the candidate shortlist and applies fuzzy reasoning — familial detection, account number exact-match boosting, confidence scoring. If confidence is below threshold, it hands back to the Supervisor with a search hint and tries again (up to 4 handoffs).
4. The enrollment decision is written to `afr_processing` and returned.

There are two entry points:
- **`POST /api/enroll`** — deterministic, no LLM, fast. Suitable for batch AFR queue processing.
- **`POST /api/chat`** — LLM-powered reasoning. Use for ambiguous edge cases that need nuanced judgment.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11 or higher |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| APX | Uplight's Databricks App framework — ask Stuart if you don't have it |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | See workspace requirements below |

### Workspace requirements

Before the agent can run against real data, your workspace needs:

1. **Foundation Model API** — A serving endpoint for the LLM. The agent is configured for `databricks-claude-sonnet-4-6`. If your workspace uses a different endpoint, update `model` in `pyproject.toml` under `[tool.apx.agent]`.
2. **Unity Catalog** — The utility account data must live in UC. The gold table and VS indexes are all UC objects.
3. **SQL warehouse** — The agent auto-discovers the first available serverless warehouse. Any active SQL warehouse works.
4. **Vector Search endpoint** — A VS endpoint must exist in your workspace. Either use an existing one or create one (see below).
5. **Delta Live Tables** — To build and keep the gold table current. Requires DLT enabled in your workspace.

---

## Part 1: Workspace setup (one-time, done by data engineer)

This part creates the infrastructure the agent queries. It only needs to be done once per workspace. If your workspace already has the gold table and VS indexes configured, skip to [Part 2](#part-2-local-development-setup).

### Step 1: Understand the data model

The agent matches AFR applications against utility account records stored in Unity Catalog. The silver tables (`prd_silver.account_location` and `prd_silver.party`) contain the raw records, but Vector Search requires a single concatenated text column per embedding. A gold table solves this.

See [`docs/gold-table-design.md`](docs/gold-table-design.md) for the full schema, rationale, and all table/index specs.

### Step 2: Create the gold table with DLT

The gold table joins the two silver tables and creates three composite text columns — one per embedding permutation. Create a new DLT pipeline in your workspace and paste this as the source notebook:

```python
import dlt
from pyspark.sql import functions as F

@dlt.table(
    name="utility_account_entities",
    comment="Gold table for entity resolution VS indexes — joins account_location + party",
    table_properties={"delta.enableChangeDataFeed": "true"},
)
def utility_account_entities():
    acct_loc = dlt.read("prd_silver.account_location")
    party = dlt.read("prd_silver.party")

    return (
        acct_loc
        .join(party, "account_id", "left")
        .select(
            # Identity / filter columns
            F.col("account_id"),
            F.col("tenant_id"),
            F.col("account_location_end"),
            F.col("zip_code"),
            # Raw entity fields (returned in search results)
            F.col("last_name"),
            F.col("first_name"),
            F.col("email"),
            F.col("service_address_line1"),
            F.col("account_number"),
            # Embedding permutations — one per VS index
            F.concat_ws(" ", F.col("first_name"), F.col("last_name"), F.col("service_address_line1")).alias("embed_full"),
            F.concat_ws(" ", F.col("last_name"), F.col("service_address_line1")).alias("embed_last_addr"),
            F.concat_ws(" ", F.col("first_name"), F.col("email")).alias("embed_first_email"),
        )
        .filter(F.col("last_name").isNotNull() | F.col("first_name").isNotNull())
    )
```

> **Note on column names:** The column names above (`account_id`, `tenant_id`, `account_location_end`, etc.) are expected values. Verify them against your actual silver table schemas before running. If your workspace uses different names, update the DLT notebook and the `UTILITY_ACCOUNT_TABLE` env var format.

Run the pipeline. Confirm the table `<catalog>.<schema>.utility_account_entities` was created and populated before continuing.

### Step 3: Enable Change Data Feed

The VS index uses Delta Change Data Feed to stay in sync as accounts are added or modified. If the DLT table properties above didn't enable it automatically, run:

```sql
ALTER TABLE <catalog>.<schema>.utility_account_entities
SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');
```

### Step 4: Create the Vector Search endpoint

If a VS endpoint doesn't already exist in your workspace:

1. In the Databricks UI, go to **Compute → Vector Search**
2. Click **Create endpoint**
3. Name it (e.g., `uplight-entity-resolution`) — this becomes your `VS_ENDPOINT` env var
4. Wait for it to reach **Online** status (a few minutes)

### Step 5: Create the three VS indexes

Run this once. Each index corresponds to one embedding permutation:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    VectorIndexType,
)

ws = WorkspaceClient()

ENDPOINT     = "<your-vs-endpoint-name>"    # from Step 4
CATALOG      = "<your-catalog>"
SCHEMA       = "<your-schema>"
SOURCE_TABLE = f"{CATALOG}.{SCHEMA}.utility_account_entities"

for col, suffix in [
    ("embed_full",        "full"),
    ("embed_last_addr",   "last_addr"),
    ("embed_first_email", "first_email"),
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
                    name=col,
                    embedding_model_endpoint_name="databricks-gte-large-en",
                )
            ],
        ),
    )
    print(f"Created index for column '{col}'")
```

The embedding step runs in the background — wait until all three indexes show **Online** in the Vector Search UI before using the agent. This typically takes 10–30 minutes for large tables.

> **Pipeline type:** `TRIGGERED` means the index syncs on demand (call `sync_index()` or trigger manually). Switch to `CONTINUOUS` if you need near-real-time freshness.

---

## Part 2: Local development setup

### Step 1: Clone and install

```bash
cd entity-resolution-agent
uv sync
```

This installs all Python dependencies into a local `.venv`.

### Step 2: Configure your Databricks CLI profile

If you haven't already, create a profile that points to your workspace:

```bash
databricks configure --profile uplight-dev
```

Enter your workspace URL and a personal access token when prompted. Verify it works:

```bash
databricks current-user me --profile uplight-dev
```

### Step 3: Set environment variables

Create a `.env` file at the project root:

```env
DATABRICKS_CONFIG_PROFILE=uplight-dev

# Vector Search
VS_ENDPOINT=<your-vs-endpoint-name>
VS_INDEX_FULL=<catalog>.<schema>.utility_account_entities_full_idx
VS_INDEX_LAST_ADDR=<catalog>.<schema>.utility_account_entities_last_addr_idx
VS_INDEX_FIRST_EMAIL=<catalog>.<schema>.utility_account_entities_first_email_idx

# SQL fallback and decision write-back
UTILITY_ACCOUNT_TABLE=<catalog>.<schema>.utility_account_entities
AFR_DECISION_TABLE=<catalog>.<schema>.afr_processing
```

Replace the placeholders with the actual catalog, schema, and names from Part 1. The `app.yml` file has placeholder comments for each variable as a reminder.

> **`.env` is gitignored.** Never commit this file.

### Step 4: Run the tests

The test suite mocks all Databricks dependencies — no live workspace connection needed:

```bash
uv run pytest tests/ -v
```

Expected output:

```
tests/test_agent_wiring.py::test_agent_is_handoff_agent PASSED
tests/test_agent_wiring.py::test_agent_has_supervisor_and_evaluator PASSED
tests/test_agent_wiring.py::test_agent_starts_with_supervisor PASSED
tests/test_enroll_endpoint.py::test_enroll_happy_path PASSED
tests/test_enroll_endpoint.py::test_enroll_no_match PASSED
tests/test_enroll_endpoint.py::test_enroll_sql_fallback_for_initials PASSED
tests/test_enroll_endpoint.py::test_enroll_returns_422_for_missing_name PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_high_confidence PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_no_candidates PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_familial_flag PASSED
tests/test_evaluator_tools.py::test_evaluate_candidates_account_number_boosts_score PASSED
tests/test_evaluator_tools.py::test_log_decision_writes_sql PASSED
tests/test_supervisor_tools.py::test_normalize_record_basic PASSED
tests/test_supervisor_tools.py::test_normalize_record_initials_triggers_sql PASSED
tests/test_supervisor_tools.py::test_normalize_record_acronym_triggers_sql PASSED
tests/test_supervisor_tools.py::test_vector_search_fans_out_across_three_indexes PASSED
tests/test_supervisor_tools.py::test_vector_search_deduplicates_by_account_id PASSED
tests/test_supervisor_tools.py::test_vector_search_keeps_highest_score_on_dedup PASSED
tests/test_supervisor_tools.py::test_sql_search_returns_candidates PASSED
19 passed in 2.6s
```

### Step 5: Run locally

```bash
apx dev
```

The agent UI opens at `http://localhost:8000`. From here you can use the chat interface to match accounts interactively. The VS indexes and tables configured in `.env` are queried live.

To test the deterministic enrollment endpoint directly:

```bash
curl -s -X POST http://localhost:8000/api/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "applicant_name": "Jane Smith",
    "address": "123 Main St",
    "email": "jane@example.com",
    "account_number": "12345",
    "tenant_id": "your_tenant_id"
  }' | python3 -m json.tool
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Update app.yml with real values

Open `app.yml` and replace the `PLACEHOLDER` values with the actual VS endpoint and table names from Part 1:

```yaml
env:
  - name: VS_ENDPOINT
    value: "uplight-entity-resolution"
  - name: VS_INDEX_FULL
    value: "catalog.schema.utility_account_entities_full_idx"
  # ... etc.
```

### Step 2: Deploy

```bash
apx deploy
```

APX builds the package, uploads it, and starts the app on Databricks. The first deploy takes 2–3 minutes. Subsequent deploys are faster.

### Step 3: Verify

After deploy, confirm the app is running:

```bash
databricks apps get entity-resolution-agent --profile uplight-dev
```

Look for `"state": "RUNNING"`. Then test the enrollment endpoint against the live app:

```bash
curl -s -X POST https://<your-app-url>/api/enroll \
  -H "Authorization: Bearer $(databricks auth token --profile uplight-dev)" \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jane Smith", "address": "123 Main St"}'
```

---

## API reference

### POST /api/enroll

Deterministic enrollment decision. No LLM call — fast, suitable for batch processing.

**Request:**

```json
{
  "applicant_name": "Jane Smith",       // required
  "address": "123 Main St",             // optional but strongly recommended
  "email": "jane@example.com",          // optional — used for maiden name matching
  "account_number": "12345",            // optional — exact match boosts confidence
  "tenant_id": "utility_a"             // optional — filters search to one tenant
}
```

**Response:**

```json
{
  "matched": true,
  "account_id": "acct-001",
  "category": "HIGH_CONFIDENCE",
  "rationale": "Best candidate 'Jane Smith' scored 0.92. Account number exact match.",
  "confidence": 0.92,
  "candidates_reviewed": 5
}
```

**Decision categories:**

| Category | Confidence | Meaning |
|----------|-----------|---------|
| `EXACT` | ≥ 0.90 | Near-certain match |
| `HIGH_CONFIDENCE` | ≥ 0.75 | Strong match, approve |
| `LOW_CONFIDENCE` | < 0.75, matched=true | Review recommended |
| `NO_MATCH` | 0.0, matched=false | No candidate found |

### POST /api/chat

LLM-powered chat interface. Use for ambiguous cases, bulk investigations, or when the deterministic path returns `LOW_CONFIDENCE`. The LLM can reason about nicknames (Liz → Elizabeth), maiden names, edge cases, and multi-account scenarios.

---

## Project structure

```
entity-resolution-agent/
├── pyproject.toml                    # Package config: name, deps, model endpoint
├── databricks.yml                    # Databricks Asset Bundle config
├── app.yml                           # App runtime command + env var stubs
├── README.md                         # This file
├── docs/
│   └── gold-table-design.md          # Gold table schema, DLT pipeline, VS index specs
├── src/entity_resolution_agent/
│   └── backend/
│       ├── app.py                    # FastAPI app entry point
│       ├── agent_router.py           # HandoffAgent: supervisor → evaluator
│       ├── router.py                 # HTTP routes: /version, /current-user, /enroll
│       ├── models.py                 # Pydantic models: AfrApplication, EnrollmentDecision
│       └── core/
│           ├── supervisor.py         # normalize_record, vector_search, sql_search
│           └── evaluator.py          # evaluate_candidates, log_decision
└── tests/
    ├── conftest.py                   # Shared fixtures and mock helpers
    ├── test_agent_wiring.py          # HandoffAgent instantiation smoke tests
    ├── test_supervisor_tools.py      # normalize, vector_search, sql_search unit tests
    ├── test_evaluator_tools.py       # evaluate_candidates, log_decision unit tests
    └── test_enroll_endpoint.py       # POST /api/enroll integration tests
```

---

## Troubleshooting

**`Missing VS index env vars` in search results**
One or more of `VS_INDEX_FULL`, `VS_INDEX_LAST_ADDR`, `VS_INDEX_FIRST_EMAIL` is not set. Check your `.env` file and confirm the variable names match exactly.

**Vector Search index shows `PROVISIONING` status**
The initial embedding job is still running. Check the Vector Search UI and wait until all three indexes show `Online` before using the agent.

**`SQL failed: ...` from sql_search**
The `UTILITY_ACCOUNT_TABLE` is not accessible from the workspace or warehouse. Confirm the table exists with `DESCRIBE TABLE <table>` and that your user has `SELECT` permission.

**Low match rates in testing**
The embedding model (`databricks-gte-large-en`) is optimized for semantic similarity, not character-level fuzzy matching. If you're seeing misses on obvious cases (e.g., "Bob" not matching "Robert"), consider adding a nickname expansion lookup to `evaluate_candidates` — see the `EVALUATOR_INSTRUCTIONS` comment in `core/evaluator.py` for the known gap.

**Index sync lag**
With `pipeline_type: TRIGGERED`, the VS index does not auto-sync. Trigger a sync manually after large account imports:

```python
from databricks.sdk import WorkspaceClient
ws = WorkspaceClient()
for suffix in ["full", "last_addr", "first_email"]:
    ws.vector_search_indexes.sync_index(
        index_name=f"<catalog>.<schema>.utility_account_entities_{suffix}_idx"
    )
```

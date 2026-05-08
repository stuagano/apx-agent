# Eligibility Agent

An AI agent that assesses program eligibility from uploaded documents — W-2s, paystubs, and residency proof — using Federal Poverty Level thresholds and an auditable reasoning trail.

Demonstrates a single `LlmAgent` driving a six-tool pipeline: document ingestion from UC Volumes via multimodal vision, income aggregation, residency verification, FPL-tier decision, and audit output.

---

## How it works

The agent runs six tools in order:

1. **`get_household`** — looks up the household record (size, names, residence address) from Unity Catalog
2. **`parse_documents`** — downloads each application PDF from a UC Volume, renders page 1, and extracts structured fields via Claude vision (W-2 wages, paystub gross pay, residency address/date)
3. **`compute_income`** — aggregates annual household income; prefers W-2 over paystub annualisation; flags discrepancies >5%
4. **`check_residency`** — verifies the residency document against the household record (address match, state, recency window)
5. **`assess_eligibility`** — applies FPL thresholds: ≤185% = eligible / priority, ≤400% = eligible / standard, >400% = ineligible
6. **`build_reasoning_trail`** — produces a markdown audit document listing every input, rule applied, and threshold checked

All configuration is environment-variable-driven — no customer names or workspace IDs are hardcoded.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | General Databricks Apps development framework — this repo (`python/`) |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog (tables + volume), SQL warehouse |

---

## Part 1: Workspace setup (one-time)

This section creates the Unity Catalog schema, tables, and volume the agent reads from. Skip to [Part 2](#part-2-local-development) if your workspace already has these resources.

### Step 1: Create the schema and tables

Run this in a Databricks notebook or the SQL editor (swap `main` and `eligibility_demo` for your catalog and schema):

```sql
CREATE SCHEMA IF NOT EXISTS main.eligibility_demo;

CREATE TABLE IF NOT EXISTS main.eligibility_demo.households (
  household_id          STRING,
  primary_filer_name    STRING,
  secondary_filer_name  STRING,
  household_size        INT,
  residence_address     STRING,
  residence_city        STRING,
  residence_state       STRING,
  residence_zip         STRING
);

CREATE TABLE IF NOT EXISTS main.eligibility_demo.applicants (
  applicant_id  STRING,
  household_id  STRING
);

CREATE TABLE IF NOT EXISTS main.eligibility_demo.applications (
  application_id  STRING,
  applicant_id    STRING,
  submitted_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS main.eligibility_demo.documents (
  document_id       STRING,
  application_id    STRING,
  document_type     STRING,
  volume_path       STRING,
  ocr_quality_hint  STRING
);
```

`document_type` values: `"w2"` | `"paystub"` | `"residency"` | `"enrollment_letter"`

### Step 2: Create the documents volume

The agent downloads PDFs from a UC Volume. Create the volume and note its path:

```sql
CREATE VOLUME IF NOT EXISTS main.eligibility_demo.documents;
```

PDFs you upload to `/Volumes/main/eligibility_demo/documents/` are referenced by `documents.volume_path`.

### Step 3: Seed test data

Insert a sample household and application so you can exercise the full pipeline locally:

```python
catalog = "main"
schema  = "eligibility_demo"

spark.sql(f"""
INSERT INTO {catalog}.{schema}.households VALUES
  ('HH001', 'Jane Smith', 'Bob Smith', 3, '123 Oak St', 'Sacramento', 'CA', '95814')
""")
spark.sql(f"""
INSERT INTO {catalog}.{schema}.applicants VALUES ('AP001', 'HH001')
""")
spark.sql(f"""
INSERT INTO {catalog}.{schema}.applications VALUES
  ('APP001', 'AP001', current_timestamp())
""")
```

Then upload a test PDF (any document) to `/Volumes/main/eligibility_demo/documents/` and insert a matching `documents` row:

```python
spark.sql(f"""
INSERT INTO {catalog}.{schema}.documents VALUES
  ('DOC001', 'APP001', 'w2', '/Volumes/main/eligibility_demo/documents/w2_jane.pdf', 'good')
""")
```

### Step 4: Grant access to the app service principal

When deployed to Databricks Apps, the app runs under a service principal. Grant it access:

```sql
GRANT USE CATALOG ON CATALOG main TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA main.eligibility_demo TO `<service-principal-name>`;
GRANT SELECT ON TABLE main.eligibility_demo.households TO `<service-principal-name>`;
GRANT SELECT ON TABLE main.eligibility_demo.applicants TO `<service-principal-name>`;
GRANT SELECT ON TABLE main.eligibility_demo.applications TO `<service-principal-name>`;
GRANT SELECT ON TABLE main.eligibility_demo.documents TO `<service-principal-name>`;
GRANT READ VOLUME ON VOLUME main.eligibility_demo.documents TO `<service-principal-name>`;
```

> Find the service principal name under **Apps → eligibility-agent → Permissions** after deploying in Part 3.

---

## Part 2: Local development

### Step 1: Install

```bash
cd eligibility-agent
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
CATALOG=main
SCHEMA=eligibility_demo
STATE_CODE=CA
PROGRAM_NAME=Community Assistance Program
RESIDENCY_RECENCY_DAYS=60
```

> `.env` is gitignored. Never commit it.

### Step 4: Run the tests

All tests mock Databricks dependencies — no live workspace connection needed:

```bash
uv run pytest tests/ -v
```

Expected output (abridged):

```
tests/test_get_household.py::test_returns_household_record PASSED
tests/test_compute_income.py::test_prefers_w2_over_paystub PASSED
tests/test_compute_income.py::test_flags_discrepancy PASSED
tests/test_check_residency.py::test_address_match PASSED
tests/test_check_residency.py::test_stale_document_rejected PASSED
tests/test_assess_eligibility.py::test_priority_tier PASSED
tests/test_assess_eligibility.py::test_standard_tier PASSED
tests/test_assess_eligibility.py::test_ineligible PASSED
tests/test_reasoning_trail.py::test_trail_contains_all_inputs PASSED
```

### Step 5: Run locally

```bash
uv run uvicorn eligibility_agent.app:app --reload
```

The agent is available at `http://localhost:8000`. Open `/_apx/agent` to chat. Try:

```
Assess eligibility for application APP001
Show me the reasoning trail for APP001
Is household HH001 eligible for the program?
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Build and deploy

The bundle builds a wheel, writes a `requirements.txt`, and deploys the app:

```bash
databricks bundle deploy
databricks bundle run eligibility-agent-app
```

### Step 2: Set environment variables in the Apps UI

The app reads all configuration from environment variables. After the first deploy, open **Apps → eligibility-agent → Environment variables** in your workspace and set:

| Variable | Value |
|----------|-------|
| `CATALOG` | Your UC catalog name |
| `SCHEMA` | Your UC schema name |
| `STATE_CODE` | Two-letter state code (e.g., `CA`) |
| `PROGRAM_NAME` | Your program name |
| `RESIDENCY_RECENCY_DAYS` | Max age of residency doc in days (default `60`) |

Then redeploy to pick up the new values:

```bash
databricks bundle deploy
```

### Step 3: Verify

```bash
databricks apps get eligibility-agent -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

Wait for `State: RUNNING`. Then confirm the app is reachable:

```bash
curl -s https://<your-app-url>/version \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)"
```

---

## Configuration

All values come from environment variables (or `.env` locally):

| Variable | Default | Description |
|----------|---------|-------------|
| `CATALOG` | `main` | UC catalog for all tables and volumes |
| `SCHEMA` | `eligibility_demo` | UC schema for all tables and volumes |
| `STATE_CODE` | `CA` | Two-letter state for residency verification |
| `PROGRAM_NAME` | `Community Assistance Program` | Appears in the reasoning trail header |
| `RESIDENCY_RECENCY_DAYS` | `60` | Max age of residency document in days |

> `DATABRICKS_HOST` and `DATABRICKS_TOKEN` are only needed if you are not using `DATABRICKS_CONFIG_PROFILE`. On Databricks Apps, the SDK picks up credentials automatically from the runtime environment.

---

## Data model

The agent reads from four Unity Catalog tables and a Volume for PDF storage.

### Tables

```
CATALOG.SCHEMA.applications
  application_id  STRING
  applicant_id    STRING
  submitted_at    TIMESTAMP

CATALOG.SCHEMA.applicants
  applicant_id    STRING
  household_id    STRING

CATALOG.SCHEMA.households
  household_id            STRING
  primary_filer_name      STRING
  secondary_filer_name    STRING   -- nullable
  household_size          INT
  residence_address       STRING
  residence_city          STRING
  residence_state         STRING
  residence_zip           STRING

CATALOG.SCHEMA.documents
  document_id         STRING
  application_id      STRING
  document_type       STRING   -- "w2" | "paystub" | "residency" | "enrollment_letter"
  volume_path         STRING   -- /Volumes/CATALOG/SCHEMA/documents/<filename>.pdf
  ocr_quality_hint    STRING   -- "good" | "fair" | "poor"
```

### Volume

```
/Volumes/CATALOG/SCHEMA/documents/   -- PDFs referenced by documents.volume_path
```

---

## FPL thresholds

Uses 2025 HHS poverty guidelines for the contiguous states:

| Household size | 100% FPL | 185% FPL (priority) | 400% FPL (cutoff) |
|---------------|----------|---------------------|--------------------|
| 1 | $15,650 | $28,953 | $62,600 |
| 2 | $21,150 | $39,128 | $84,600 |
| 3 | $26,650 | $49,303 | $106,600 |
| 4 | $32,150 | $59,478 | $128,600 |

A production deployment should read thresholds from an authoritative benefits-rule service rather than the inline constants in `assess_eligibility.py`.

---

## Project structure

```
eligibility-agent/
├── app.yml                              # Databricks Apps runtime config
├── databricks.yml                       # Asset Bundle — build, deploy, app resource
├── src/eligibility_agent/
│   ├── app.py                           # FastAPI app entry point
│   ├── config.py                        # Settings (catalog, schema, state, program)
│   ├── prompts.py                       # LLM system prompt
│   └── tools/
│       ├── get_household.py             # UC lookup: household + applicant records
│       ├── parse_documents.py           # PDF vision extraction
│       ├── compute_income.py            # Annual income aggregation + discrepancy flag
│       ├── check_residency.py           # Address match + recency check
│       ├── assess_eligibility.py        # FPL-tier decision
│       └── reasoning_trail.py          # Markdown audit output
└── tests/
    ├── test_get_household.py
    ├── test_parse_documents.py
    ├── test_compute_income.py
    ├── test_check_residency.py
    ├── test_assess_eligibility.py
    ├── test_reasoning_trail.py
    └── test_golden.py
```

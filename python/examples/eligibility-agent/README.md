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

### Demo seed script

Create the schema and insert test records in a notebook:

```python
catalog = "main"
schema  = "eligibility_demo"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

# Tables
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.households (
  household_id STRING, primary_filer_name STRING, secondary_filer_name STRING,
  household_size INT, residence_address STRING, residence_city STRING,
  residence_state STRING, residence_zip STRING
)
""")
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.applicants (
  applicant_id STRING, household_id STRING
)
""")
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.applications (
  application_id STRING, applicant_id STRING, submitted_at TIMESTAMP
)
""")
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.documents (
  document_id STRING, application_id STRING, document_type STRING,
  volume_path STRING, ocr_quality_hint STRING
)
""")

# Volume
spark.sql(f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.documents")

# Seed row
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

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog (tables + volume), SQL warehouse |

---

## Local setup

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/eligibility-agent

cp .env.example .env
# Edit .env with your workspace URL, token, catalog, schema
```

Run the tests (no workspace required):

```bash
uv run pytest tests/ -q
```

Start the agent locally:

```bash
uv run uvicorn eligibility_agent.app:app --reload
```

The agent is now available at `http://localhost:8000`. Open `/_apx/agent` to chat.

---

## Deploy to Databricks Apps

```bash
databricks bundle deploy
databricks bundle run eligibility-agent-app
```

The bundle config (`databricks.yml`) builds a wheel, writes a `requirements.txt`, and deploys to Databricks Apps. The app reads config from Databricks Apps environment variables — set them under **Apps → eligibility-agent → Environment variables** in the workspace UI.

---

## Configuration

All values come from environment variables (or `.env` locally):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABRICKS_HOST` | — | Workspace URL |
| `DATABRICKS_TOKEN` | — | PAT or OAuth token |
| `CATALOG` | `main` | UC catalog for all tables and volumes |
| `SCHEMA` | `eligibility_demo` | UC schema for all tables and volumes |
| `STATE_CODE` | `CA` | Two-letter state for residency verification |
| `PROGRAM_NAME` | `Community Assistance Program` | Appears in the reasoning trail header |
| `RESIDENCY_RECENCY_DAYS` | `60` | Max age of residency document in days |

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

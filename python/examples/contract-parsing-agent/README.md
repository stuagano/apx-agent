# Contract Parsing Agent

Extract structured data from contracts using GenAI — pricing terms, service periods, SLAs, and regulatory clauses — **one `Agent`, under 300 lines of Python**.

Maintains a searchable contract portfolio in Unity Catalog. Upload a PDF or text file and the agent extracts a structured schema, stores it, and makes it queryable. Find contracts expiring soon, compare terms across counterparties, or pull a full structured summary of any contract.

---

## What makes this simple

One `Agent` with four tools — portfolio management handled entirely by the LLM:

```python
agent = Agent(
    tools=[
        query_portfolio,
        summarize_contract,
        find_contracts_expiring,
        extract_new_contract,
    ],
    sub_agents=_resolve_sub_agents(),   # optional data-inspector for SQL
    instructions=SYSTEM_PROMPT,
)
```

Each tool writes to Unity Catalog using the injected workspace client:

```python
def extract_new_contract(volume_path: str, ws: Workspace = None) -> dict:
    """Extract structured fields from a contract file and store in UC.
    volume_path: full path to the contract in a UC volume"""
    raw = ws.files.download(volume_path).contents.read()
    ...
```

Upload via the `/upload` REST endpoint — the agent handles extraction automatically.

---

## Pipeline

```
POST /upload  →  raw file → UC volume
                          ↓
                  extract_new_contract
                          ↓
               GenAI extraction → structured schema
                          ↓
                  write to UC table
                          ↓
               queryable via chat or query_portfolio
```

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | General Databricks Apps development framework — this repo (`python/`) |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog tables and volumes (see Part 1) |

---

## Part 1: Workspace setup (one-time)

This section creates the Unity Catalog resources the agent reads from and writes to. Skip to [Part 2](#part-2-local-development) if your workspace already has the tables and volumes.

### Step 1: Create the schema and volumes

Run this in a Databricks notebook or the SQL editor:

```sql
CREATE SCHEMA IF NOT EXISTS my_catalog.contracts;

CREATE VOLUME IF NOT EXISTS my_catalog.contracts.raw_contracts;
CREATE VOLUME IF NOT EXISTS my_catalog.contracts.uploaded_contracts;
```

- `raw_contracts` — the agent reads source PDFs from here
- `uploaded_contracts` — files uploaded via `/upload` land here before extraction

### Step 2: Create the contracts portfolio table

This is the main output table. One row per contract, populated by the `extract_new_contract` tool:

```sql
CREATE TABLE IF NOT EXISTS my_catalog.contracts.contracts (
  contract_id        STRING,
  counterparty       STRING,
  contract_type      STRING,
  effective_date     DATE,
  expiration_date    DATE,
  value_usd          DOUBLE,
  sla_terms          STRING,
  payment_terms      STRING,
  regulatory_clauses STRING,
  raw_text           STRING,
  volume_path        STRING,
  extracted_at       TIMESTAMP
);
```

### Step 3: Create the ground truth table

Used by the evaluation tooling to score extraction quality:

```sql
CREATE TABLE IF NOT EXISTS my_catalog.contracts.contracts_ground_truth (
  contract_id STRING,
  field       STRING,
  expected    STRING
);
```

### Step 4: Grant access to the app service principal

When deployed to Databricks Apps, the app runs under a service principal. Grant it access to read and write the resources you just created:

```sql
GRANT USE CATALOG ON CATALOG my_catalog TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA my_catalog.contracts TO `<service-principal-name>`;
GRANT SELECT, MODIFY ON TABLE my_catalog.contracts.contracts TO `<service-principal-name>`;
GRANT SELECT, MODIFY ON TABLE my_catalog.contracts.contracts_ground_truth TO `<service-principal-name>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME my_catalog.contracts.raw_contracts TO `<service-principal-name>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME my_catalog.contracts.uploaded_contracts TO `<service-principal-name>`;
```

> Find the service principal name in **Apps → contract-parsing-agent → Permissions** after you deploy in Part 3.

---

## Part 2: Local development

### Step 1: Install

```bash
cd contract-parsing-agent
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
CATALOG=my_catalog
SCHEMA=contracts
VOLUMES_RAW=/Volumes/my_catalog/contracts/raw_contracts
VOLUMES_UPLOADS=/Volumes/my_catalog/contracts/uploaded_contracts
```

> `.env` is gitignored. Never commit it.

Optional — connect to a data-inspector sub-agent for SQL exploration:

```env
SUB_AGENTS=https://my-data-inspector-app.databricks.com
```

### Step 4: Run the tests

All tests mock Databricks dependencies — no live connection needed:

```bash
uv run pytest tests/ -v
```

Expected output (abridged):

```
tests/test_config.py::test_env_overrides_yaml PASSED
tests/test_extraction.py::test_extraction_schema_valid PASSED
tests/test_router_contracts.py::test_query_portfolio_returns_list PASSED
tests/test_router_upload.py::test_upload_stores_file PASSED
tests/test_tools_extract_new_contract.py::test_extract_writes_row PASSED
tests/test_tools_find_contracts_expiring.py::test_expiring_filters_by_days PASSED
tests/test_tools_query_portfolio.py::test_query_filters_by_counterparty PASSED
tests/test_tools_summarize_contract.py::test_summarize_returns_dict PASSED
```

### Step 5: Run locally

```bash
uv run uvicorn app:app --reload
```

The chat interface opens at `http://localhost:8000`. Try:

```
Show me all contracts expiring in the next 90 days
Summarize contract ID abc-123
What SLA commitments do we have with Acme Corp?
Extract the contract at /Volumes/my_catalog/contracts/uploads/new_agreement.pdf
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set real values in `databricks.yml`

The bundle uses variables for all deployment-specific values. Pass them on the command line:

```bash
databricks bundle deploy \
  --var="catalog=my_catalog" \
  --var="schema=contracts" \
  --var="volumes_raw=/Volumes/my_catalog/contracts/raw_contracts" \
  --var="volumes_uploads=/Volumes/my_catalog/contracts/uploaded_contracts"
```

Or set them permanently in the `targets.dev.variables` block in `databricks.yml`:

```yaml
targets:
  dev:
    mode: development
    default: true
    variables:
      catalog: my_catalog
      schema: contracts
      volumes_raw: /Volumes/my_catalog/contracts/raw_contracts
      volumes_uploads: /Volumes/my_catalog/contracts/uploaded_contracts
```

### Step 2: Deploy

```bash
databricks bundle deploy
```

This builds a wheel, writes a `requirements.txt`, and deploys the app to Databricks Apps.

### Step 3: Verify

```bash
databricks apps get contract-parsing-agent -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

Wait for `State: RUNNING` before testing. Then confirm the app is reachable:

```bash
curl -s https://<your-app-url>/version \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)"
```

### Redeploy after changes

```bash
databricks bundle deploy
```

---

## Configuration

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `CATALOG` | Yes | — | Unity Catalog catalog name |
| `SCHEMA` | Yes | — | Unity Catalog schema name |
| `VOLUMES_RAW` | Yes | — | UC volume path for raw contract files |
| `VOLUMES_UPLOADS` | Yes | — | UC volume path for uploaded contracts |
| `SUB_AGENTS` | No | — | Comma-separated URLs of sub-agents (e.g., data-inspector) |

---

## Tools

| Tool | What it does |
|------|--------------|
| `query_portfolio` | Filter and list contracts by counterparty, type, or date range |
| `summarize_contract` | Structured summary of a specific contract by ID |
| `find_contracts_expiring` | Contracts expiring within N days |
| `extract_new_contract` | Extract fields from a file in UC volumes and store in the portfolio |

---

## Project structure

```
contract-parsing-agent/
├── agent.py                             # Root agent — wires tools + system prompt
├── agent.config.yaml                    # System prompt, extraction schema, demo questions
├── app.py                               # FastAPI app (dev mode) + dev UI + SPA
├── app.yml                              # Databricks Apps runtime config
├── api.py                               # /api/* routes (version, current-user, upload, etc.)
├── config.py                            # Settings (catalog, schema, volume paths)
├── extraction.py                        # Shared PDF -> structured extraction logic
├── models.py                            # Pydantic models for API responses
├── databricks.yml                       # Asset Bundle — build, deploy, app resource + variables
├── tools/                               # Agent tool implementations
│   ├── query_portfolio.py
│   ├── summarize_contract.py
│   ├── find_contracts_expiring.py
│   ├── extract_new_contract.py
│   └── _sql.py                          # Shared SQL helpers
├── scripts/                             # Demo + setup scripts
│   ├── generate_synthetic_contracts.py  # Make synthetic PDFs for the demo
│   ├── provision_uc.py                  # Provision UC catalog/schema/volumes
│   ├── setup_portfolio.py               # Batch-extract contracts into the portfolio table
│   └── smoke_demo.py                    # End-to-end smoke test
├── client/                              # React + Vite SPA
└── tests/                               # pytest suite
```

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

## Before you start

This agent needs Unity Catalog tables and volumes provisioned. Run the setup notebook in `notebooks/` against your catalog and schema before deploying.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog tables and volumes (run setup notebook first — see above) |

---

## Run locally

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/contract-parsing-agent

uv sync

CATALOG=my_catalog SCHEMA=contracts \
VOLUMES_RAW=/Volumes/my_catalog/contracts/raw \
VOLUMES_UPLOADS=/Volumes/my_catalog/contracts/uploads \
uv run uvicorn contract_parsing_agent.backend.app:app --port 8001
```

Try it:

```
Show me all contracts expiring in the next 90 days
Summarize contract ID abc-123
What SLA commitments do we have with Acme Corp?
Extract the contract at /Volumes/my_catalog/contracts/uploads/new_agreement.pdf
```

---

## Deploy to Databricks Apps

### Prerequisites

- **Databricks CLI** — [install](https://docs.databricks.com/dev-tools/cli/databricks-cli.html)
- **uv** — `pip install uv`
- A Databricks workspace with [Apps enabled](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)
- **UC tables and volumes provisioned** — run `notebooks/setup.py` first

### 1. Authenticate

```bash
databricks auth login --host https://<your-workspace>.azuredatabricks.net
databricks current-user me
```

### 2. Get the code

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/contract-parsing-agent
```

### 3. Provision Unity Catalog resources

Run the setup notebook in your workspace to create the required tables and volumes, then note the catalog, schema, and volume paths.

### 4. Configure `databricks.yml`

Set the variables for your environment:

```bash
databricks bundle deploy \
  --var="catalog=my_catalog" \
  --var="schema=contracts" \
  --var="volumes_raw=/Volumes/my_catalog/contracts/raw" \
  --var="volumes_uploads=/Volumes/my_catalog/contracts/uploads"
```

Or set them in a `databricks.yml` `targets` block:

```yaml
targets:
  dev:
    mode: development
    default: true
    variables:
      catalog: my_catalog
      schema: contracts
      volumes_raw: /Volumes/my_catalog/contracts/raw
      volumes_uploads: /Volumes/my_catalog/contracts/uploads
```

### 5. Build

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
```

### 6. Deploy

```bash
databricks bundle deploy
```

Check status:

```bash
databricks apps get contract-parsing-agent -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

### Redeploy after changes

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
databricks bundle deploy
```

---

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `CATALOG` | Yes | Unity Catalog catalog name |
| `SCHEMA` | Yes | Unity Catalog schema name |
| `VOLUMES_RAW` | Yes | UC volume path for raw contract files |
| `VOLUMES_UPLOADS` | Yes | UC volume path for uploaded contracts |
| `AGENT_HUB_URL` | No | Register with an Agent Hub on startup |

---

## Tools

| Tool | What it does |
|------|--------------|
| `query_portfolio` | Filter and list contracts by counterparty, type, or date range |
| `summarize_contract` | Structured summary of a specific contract by ID |
| `find_contracts_expiring` | Contracts expiring within N days |
| `extract_new_contract` | Extract fields from a file in UC volumes and store in the portfolio |

---

## Project Structure

```
contract-parsing-agent/
├── app.yml                              # Databricks Apps runtime config
├── databricks.yml                       # Asset Bundle — build, deploy, app resource + variables
├── notebooks/                           # UC provisioning notebook
└── src/contract_parsing_agent/backend/
    ├── agent_router.py                  # Agent wiring
    ├── app.py                           # FastAPI app + /upload endpoint
    ├── config.py                        # Settings (catalog, schema, volume paths)
    └── tools/
        ├── query_portfolio.py
        ├── summarize_contract.py
        ├── find_contracts_expiring.py
        └── extract_new_contract.py
```

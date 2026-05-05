# Contract Parsing Agent

Extract structured data from contracts using GenAI — identifies pricing terms, service periods, SLAs, and regulatory clauses.

## What it does

Maintains a portfolio of parsed contracts in Unity Catalog. Supports uploading new contracts (PDF or text) via a `/upload` endpoint, which triggers GenAI extraction into a structured schema. Provides tools to query the portfolio, summarize individual contracts, and search semantically across the corpus using vector search.

## Required env vars

| Variable | Description |
|---|---|
| `CATALOG` | Unity Catalog catalog name |
| `SCHEMA` | Unity Catalog schema name |
| `VOLUMES_RAW` | Full UC volume path for raw contract files (e.g. `/Volumes/<catalog>/<schema>/raw`) |
| `VOLUMES_UPLOADS` | Full UC volume path for uploaded contracts |
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
# 1. Provision the Unity Catalog tables and volumes
# Run the provisioning notebook in notebooks/

# 2. Build and deploy
uv build --wheel
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `query_portfolio` | Filter and list contracts by counterparty, type, date range |
| `get_contract_summary` | Structured summary of a specific contract |
| `extract_pricing_terms` | Pricing tiers, demand charges, escalation clauses |
| `search_contracts` | Semantic search across contracts via vector search |
| `find_contracts_expiring` | Contracts expiring within N days |
| `extract_new_contract` | Extract and store a new contract from a volume path |

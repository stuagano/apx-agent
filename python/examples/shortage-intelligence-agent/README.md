# Shortage Intelligence Agent

Detects shortage signals in demand data, validates them against historical patterns and market reports, checks live vendor pricing, and delivers dual actionable reports to sourcing and sales teams.

## What it does

```
"Run the daily shortage scan"
"Are there any confirmed shortage signals this week?"
"What's the DigiKey availability on NXP components flagged yesterday?"
```

The agent runs a five-step sequential investigation:

1. **Demand Cluster Detection** — scans internal orders for components requested by multiple customers within 48 hours
2. **Historical Pattern Analysis** — looks up prior shortage events, price deltas, and average duration
3. **Market Signal Validation** — queries a Knowledge Assistant to confirm signals against industry news
4. **Vendor Pricing** — checks DigiKey for live price, stock quantity, and lead time; finds spec-matched alternative parts
5. **Dual Report Generation** — synthesizes findings into separate reports for the sourcing and sales teams

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog, SQL warehouse, Genie space, Knowledge Assistant endpoint |

## Architecture

```
User / Scheduled Job
  └── APX app (FastAPI)
        ├── /invocations      — chat endpoint (MLflow-compatible)
        ├── /mcp              — stateless MCP
        └── SequentialAgent
              ├── Step 1: detection_agent    (scan_demand_clusters)
              ├── Step 2: historical_agent   (find_historical_patterns)
              ├── Step 3: market_agent       (validate_against_market_news)
              ├── Step 4: vendor_agent       (check_vendor_availability, find_alternative_parts)
              └── Step 5: report_agent       (synthesis — no tools)
```

Each step's output accumulates in conversation history, so the report agent sees all prior findings without extra wiring.

### Why SequentialAgent

The five steps are strictly ordered and each builds on prior output:
- Step 3 (market validation) needs component IDs from Step 1
- Step 4 (vendor pricing) only runs for signals confirmed in Step 3
- Step 5 (reporting) needs all upstream data

`SequentialAgent` enforces this order structurally — no custom orchestration needed.

## Data sources

| Source | Purpose | Config |
|--------|---------|--------|
| Unity Catalog demand orders table | Step 1 (primary) | `DEMAND_ORDERS_TABLE` |
| Databricks Genie space | Step 1 (fallback if no table) | `DEMAND_GENIE_SPACE_ID` |
| Unity Catalog historical table | Step 2 | `HISTORICAL_DEMAND_TABLE` |
| Knowledge Assistant endpoint | Step 3 | `KA_ENDPOINT` |
| DigiKey API | Step 4 | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` |
| Unity Catalog parts catalog | Step 4 (alternatives) | `PARTS_CATALOG_TABLE` |

All data sources are optional — unconfigured sources return a structured stub so the agent can narrate what's missing and continue.

## Required Unity Catalog table schemas

### `DEMAND_ORDERS_TABLE`
```sql
component_id     STRING,
component_name   STRING,
customer_id      STRING,
quantity_requested BIGINT,
requested_at     TIMESTAMP
```

### `HISTORICAL_DEMAND_TABLE`
```sql
component_id           STRING,
event_date             DATE,
price_before_usd       DOUBLE,
price_peak_usd         DOUBLE,
shortage_duration_days INT,
resolution_notes       STRING
```

### `PARTS_CATALOG_TABLE`
```sql
part_number        STRING,
manufacturer       STRING,
package_type       STRING,
voltage_rating_v   DOUBLE,
current_rating_a   DOUBLE,
temperature_range  STRING,
in_stock           BOOLEAN
```

## Required env vars

| Variable | Description |
|----------|-------------|
| `AGENT_MODEL` | Databricks model serving endpoint name (default: `databricks-claude-sonnet-4-6`) |
| `DEMAND_ORDERS_TABLE` | UC table for demand orders (`catalog.schema.table`) |
| `DEMAND_GENIE_SPACE_ID` | Genie space ID — used if `DEMAND_ORDERS_TABLE` is not set |
| `HISTORICAL_DEMAND_TABLE` | UC table for historical shortage events |
| `KA_ENDPOINT` | Knowledge Assistant endpoint URL |
| `PARTS_CATALOG_TABLE` | UC table for parts catalog and alternative lookup |
| `DIGIKEY_CLIENT_ID` | DigiKey OAuth2 client ID |
| `DIGIKEY_CLIENT_SECRET` | DigiKey OAuth2 client secret |
| `SLACK_WEBHOOK_SOURCING` | Slack incoming webhook for sourcing team reports |
| `SLACK_WEBHOOK_SALES` | Slack incoming webhook for sales team reports |

## Scheduled morning scan

A Databricks Job (`shortage-morning-scan` in `databricks.yml`) runs the pipeline daily at 7 AM and delivers both reports to Slack. If Slack webhooks are not configured, reports are logged to stdout instead.

## Development

```bash
git clone https://github.com/stuagano/apx-agent
cd apx-agent/python/examples/shortage-intelligence-agent
uv sync
uv run uvicorn shortage_intelligence_agent.backend.app:app --reload --port 8000
```

## Deploy to Databricks Apps

```bash
databricks bundle deploy
databricks bundle run shortage-intelligence-app
```

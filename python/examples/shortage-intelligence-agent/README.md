# Shortage Intelligence Agent

Detects shortage signals in demand data, validates them against historical patterns and market reports, checks live vendor pricing, and delivers dual actionable reports to sourcing and sales teams.

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

---

## What you'll learn

- How to build a **SequentialAgent** where each step's output accumulates in conversation history so later steps see all prior findings without extra wiring
- How to wire optional data sources that return structured stubs when unconfigured — the agent narrates what's missing and continues
- How to call external OAuth2 APIs (DigiKey) from an agent tool
- How to post structured reports to **Slack incoming webhooks** from an agent step
- How to schedule an agent as a **Databricks Job** that runs on a cron and delivers reports automatically

---

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
              └── Step 5: report_agent       (synthesis — no tools, posts to Slack)
```

### Why SequentialAgent

The five steps are strictly ordered and each builds on prior output:
- Step 3 (market validation) needs component IDs from Step 1
- Step 4 (vendor pricing) only runs for signals confirmed in Step 3
- Step 5 (reporting) needs all upstream data

`SequentialAgent` enforces this order structurally — no custom orchestration needed.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | General Databricks Apps development framework — this repo (`python/`) |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog, SQL warehouse (see Part 1) |

---

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

---

## Part 1: Workspace setup (one-time)

This section creates the Unity Catalog tables and optional integrations the agent queries. Skip steps you've already completed or that aren't relevant to your setup.

### Step 1: Create the Unity Catalog tables

Run the following DDL in a Databricks notebook or SQL editor. Substitute your catalog and schema names.

**Demand orders** (Step 1 source — primary signal):
```sql
CREATE TABLE IF NOT EXISTS catalog.schema.demand_orders (
  component_id       STRING,
  component_name     STRING,
  customer_id        STRING,
  quantity_requested BIGINT,
  requested_at       TIMESTAMP
);
```

**Historical shortage events** (Step 2 source):
```sql
CREATE TABLE IF NOT EXISTS catalog.schema.shortage_history (
  component_id           STRING,
  event_date             DATE,
  price_before_usd       DOUBLE,
  price_peak_usd         DOUBLE,
  shortage_duration_days INT,
  resolution_notes       STRING
);
```

**Parts catalog** (Step 4 source — alternative part lookup):
```sql
CREATE TABLE IF NOT EXISTS catalog.schema.parts_catalog (
  part_number        STRING,
  manufacturer       STRING,
  package_type       STRING,
  voltage_rating_v   DOUBLE,
  current_rating_a   DOUBLE,
  temperature_range  STRING,
  in_stock           BOOLEAN
);
```

Populate each table from your existing data sources before running the agent.

### Step 2: (Optional) Set up a Genie space for demand data

If you don't have a structured `demand_orders` table yet, a Genie space backed by your existing demand data is a useful fallback.

1. In the Databricks UI, go to **AI/BI → Genie**
2. Create a new Genie space connected to your demand orders data
3. Copy the space ID from the URL — it looks like `01ef...` in `https://<workspace>/genie/spaces/01ef...`
4. Set `DEMAND_GENIE_SPACE_ID` to this value

> If both `DEMAND_ORDERS_TABLE` and `DEMAND_GENIE_SPACE_ID` are set, the table takes precedence.

### Step 3: (Optional) Set up a Knowledge Assistant endpoint

The market signal validation step (Step 3) queries a Knowledge Assistant loaded with industry reports, supplier bulletins, or internal market intelligence documents.

1. In the Databricks UI, go to **AI/BI → Knowledge Assistants**
2. Create a new endpoint and upload your document corpus
3. Copy the endpoint URL
4. Set `KA_ENDPOINT` to this URL

If `KA_ENDPOINT` is not configured, Step 3 returns a stub and the agent continues with Steps 4 and 5.

### Step 4: (Optional) Get DigiKey API credentials

The vendor pricing step (Step 4) calls the DigiKey product search API for live price, stock, and lead time data.

1. Go to [developer.digikey.com](https://developer.digikey.com) and create a developer account
2. Create a new app to get a client ID and client secret
3. Set `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`

If these are not configured, Step 4 returns a stub with a note that live vendor data is unavailable.

### Step 5: (Optional) Create Slack incoming webhooks

The report agent (Step 5) posts dual reports — one to the sourcing team, one to the sales team — via Slack incoming webhooks.

1. Go to [api.slack.com/messaging/webhooks](https://api.slack.com/messaging/webhooks)
2. Create an app for your workspace (or use an existing one)
3. Enable **Incoming Webhooks** and add two webhooks — one per target channel
4. Set `SLACK_WEBHOOK_SOURCING` and `SLACK_WEBHOOK_SALES` to the respective webhook URLs

If webhooks are not configured, reports are logged to stdout instead.

---

## Part 2: Local development

### Step 1: Install

```bash
cd shortage-intelligence-agent
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
AGENT_MODEL=databricks-claude-sonnet-4-6

# Unity Catalog tables (from Step 1)
DEMAND_ORDERS_TABLE=catalog.schema.demand_orders
HISTORICAL_DEMAND_TABLE=catalog.schema.shortage_history
PARTS_CATALOG_TABLE=catalog.schema.parts_catalog

# Optional integrations
# DEMAND_GENIE_SPACE_ID=01ef...
# KA_ENDPOINT=https://<workspace>.databricks.com/api/2.0/knowledge-assistants/endpoints/<name>/query
# DIGIKEY_CLIENT_ID=...
# DIGIKEY_CLIENT_SECRET=...
# SLACK_WEBHOOK_SOURCING=https://hooks.slack.com/services/...
# SLACK_WEBHOOK_SALES=https://hooks.slack.com/services/...
```

> `.env` is gitignored. Never commit it.

### Step 4: Run the tests

This example does not currently include a test suite. To verify the agent loads correctly:

```bash
uv run python -c "from shortage_intelligence_agent.backend.app import app; print('OK')"
```

### Step 5: Run locally

```bash
uv run uvicorn shortage_intelligence_agent.backend.app:app --reload --port 8000
```

The agent UI opens at `http://localhost:8000`. Try:

> *Run the daily shortage scan*

> *Are there any confirmed shortage signals this week?*

> *What's the DigiKey availability on NXP components flagged yesterday?*

With no data configured, the agent will respond with stubs for each step and explain what data sources are missing. With real tables populated, it runs the full five-step investigation.

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set real values in `app.yml`

Replace the empty strings with the names from Part 1:

```yaml
env:
  - name: AGENT_MODEL
    value: "databricks-claude-sonnet-4-6"
  - name: DEMAND_ORDERS_TABLE
    value: "catalog.schema.demand_orders"
  - name: HISTORICAL_DEMAND_TABLE
    value: "catalog.schema.shortage_history"
  - name: PARTS_CATALOG_TABLE
    value: "catalog.schema.parts_catalog"
  # Optional — leave empty to skip that step
  - name: DEMAND_GENIE_SPACE_ID
    value: ""
  - name: KA_ENDPOINT
    value: ""
  - name: DIGIKEY_CLIENT_ID
    value: ""
  - name: DIGIKEY_CLIENT_SECRET
    value: ""
  - name: SLACK_WEBHOOK_SOURCING
    value: ""
  - name: SLACK_WEBHOOK_SALES
    value: ""
```

### Step 2: Deploy

```bash
uv run apx deploy
```

### Step 3: Verify

```bash
databricks apps get shortage-intelligence-agent --profile my-workspace
# look for "state": "RUNNING"
```

Test the live app:

```bash
curl -s https://<your-app-url>/api/version \
  -H "Authorization: Bearer $(databricks auth token --profile my-workspace)"
```

### Scheduled morning scan

`databricks.yml` includes a job named `shortage-morning-scan` that runs the pipeline daily at 7 AM and delivers both reports to Slack. After deploying, enable the job in the Databricks UI under **Workflows** or run it on demand:

```bash
databricks bundle run shortage-morning-scan --profile my-workspace
```

If Slack webhooks are not configured, reports are logged to the job run output instead.

---

## Env var reference

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

---

## Project structure

```
shortage-intelligence-agent/
├── app.yml                                    # Runtime command + env vars
├── pyproject.toml                             # Package config and deps
├── databricks.yml                             # Asset Bundle — app + morning-scan job
└── src/shortage_intelligence_agent/
    └── backend/
        ├── app.py                             # FastAPI app entry point
        ├── agent_router.py                    # SequentialAgent: 5-step pipeline
        ├── pipeline.py                        # Step agent definitions
        ├── config.py                          # Settings from env vars
        ├── models.py                          # Pydantic models
        ├── router.py                          # HTTP routes: /version, /current-user
        └── core/                              # Tool implementations per step
```

---

## Troubleshooting

**Agent returns stubs for every step**
No data sources are configured. Set at least `DEMAND_ORDERS_TABLE` in your `.env` and populate it with rows. The agent reports which sources are missing in its response.

**DigiKey API returns 401**
The client credentials flow requires the credentials to be valid and the DigiKey sandbox/production URL to match. Check `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`. DigiKey sandbox credentials don't work against the production endpoint.

**Slack reports not arriving**
Verify the webhook URL is active by testing it directly:
```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"test"}' \
  https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

**Morning scan job fails immediately**
The job must be able to reach the deployed app URL. Confirm the app is `RUNNING` before enabling the scheduled job.

---

## Migration to the 2026-05 primitives

See [`MIGRATION.md`](./MIGRATION.md) for the dry-run analysis. Quick summary:

| Move | Status |
|---|---|
| `deploy.py` using `log_agent` + `set_uc_tags_for_agent` + `databricks.agents.deploy` | ✅ shipped |
| `evalset.jsonl` for `apx eval-chain` | ✅ shipped |
| Extract `classify_shortage_severity` as `@tool(uc=...)` (worked UC-function example) | ✅ shipped |
| Wire `DeltaSessionStore` (multi-turn) | documented, depends on UX decision |
| Wire `WatchdogGuard` + local guards | documented, depends on watchdog availability |
| Move data-fetching tools to `@tool(uc=...)` | ❌ blocked — needs user-scoped OBO |

### Deploy via the canonical flow

```bash
export REGISTERED_MODEL_NAME=main.agents.shortage_intelligence
export SERVING_MODEL_ENDPOINT=databricks-claude-sonnet-4-6
export APX_EXPERIMENT=/Users/me@company.com/agents/shortage_intelligence

python deploy.py             # log → register → deploy → set UC tags
# add --no-deploy to log + register only
```

### Operate via the apx CLI

```bash
apx info  --module shortage_intelligence_agent.backend.agent_router:agent
apx logs  --endpoint shortage_intelligence
apx trace --agent shortage_intelligence --limit 20
apx cost  --agent shortage_intelligence --hours 24
apx topology --format mermaid > shortage_topology.mmd
apx list
```

### Evaluate the chain

```bash
apx eval-chain evalset.jsonl \
    --module shortage_intelligence_agent.backend.agent_router:agent \
    --model "$SERVING_MODEL_ENDPOINT" \
    --experiment "$APX_EXPERIMENT"
```

Reports which tools fired per prompt by walking MLflow traces and pairing them to the evalset rows via `apx.tool.name` / `apx.subagent.endpoint` span attributes.

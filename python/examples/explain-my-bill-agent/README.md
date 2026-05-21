# explain-my-bill-agent

Energy billing Q&A agent. Given a question like *"Why was CUST-0001's March bill higher than February?"*, the agent looks up the account, queries AMI smart-meter data, billing history, and rate schedules from Unity Catalog, then explains the change in plain language.

Demonstrates:

- **OBO governance.** `get_session_context` runs first on every interaction and reports the calling user + auth method (OBO token, Databricks Apps SSO, or local CLI).
- **Tools that bind to the user's workspace identity** via `Dependencies.Client` — Unity Catalog grants govern access to billing/AMI tables.
- **UC function catalog pattern.** `catalog/register_agent.py` wraps the deployed App as a UC function so any workspace with `EXECUTE` permission can call the agent from SQL — no MCP setup needed.

## Tools

| Tool | What it returns |
|---|---|
| `get_session_context` | Calling user + auth method + governance scope |
| `get_customer_profile` | Customer record by ID or partial name |
| `query_ami_readings` | Daily kWh totals between two dates |
| `get_billing_summary` | Recent bills with tier breakdown + payment status |
| `get_rate_schedule` | Tier thresholds + per-kWh rates for a rate plan |
| `compare_months` | Side-by-side billing + AMI + computed deltas |

## Setup

Expected Unity Catalog tables under `${DEMO_CATALOG}.${DEMO_SCHEMA}`:

- `customers` — account profiles, rate plan, linked AMI device
- `ami_hourly_rollups` — daily energy usage (kWh totals, min/max/avg)
- `billing_history` — monthly bills with tier breakdowns, taxes, payments
- `rate_schedules` — rate plans (tier thresholds, per-kWh rates, fixed charges)

Set environment variables (see `app.yml` placeholders):

```bash
export DEMO_CATALOG=<your_catalog>
export DEMO_SCHEMA=<your_schema>
export WAREHOUSE_ID=<your_warehouse_id>
```

## Run locally

```bash
uv sync
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/_apx/agent` for the dev UI, or POST to `/responses`.

## Deploy

```bash
databricks bundle deploy
databricks bundle run mcp-explain-my-bill
```

## Register as a UC function

After deploying:

```bash
python catalog/register_agent.py
```

Then any workspace user with `EXECUTE` on the function can call the agent from SQL:

```sql
SELECT my_catalog.agents.ask_explain_my_bill(
  'Why was CUST-0001''s March bill higher than February?'
);
```

See `catalog/register_agent.py` for prerequisites (service principal token, secret scope).

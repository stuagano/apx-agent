# Explain My Bill — Energy Billing Q&A Agent

A Databricks App that answers customer questions about energy bills. Deployed as `mcp-explain-my-bill`, it surfaces automatically in Genie Code as a custom MCP server.

## What it does

```
"Why was Jane Doe's March bill $129?"
"How does CUST-0042's usage compare to last month?"
"What are the tier rates on the RP-TOU plan?"
```

The agent calls the right tools, queries live Unity Catalog data, and synthesizes a clear answer.

## Architecture

```
Genie Code (Agent mode)
  └── MCP (POST /mcp, stateless HTTP)       ← auto-discovered: apps named mcp-* appear
        └── APX app (FastAPI)                  in Genie Code → Settings → Custom MCP Servers
              ├── /invocations      — chat endpoint (MLflow-compatible)
              ├── /mcp              — stateless MCP for Genie Code / AI Playground
              ├── /mcp/sse          — SSE MCP for Claude Desktop / Cursor
              └── /api/tools/*      — individual tool routes
```

Tools dispatch SQL via the **Statement Execution API** against Unity Catalog tables.

## The pattern in 5 lines

Tools are plain Python functions. Type hints define the schema. The docstring is the description.
FastAPI-typed parameters (`ws: Client`) are injected and excluded from the tool schema.

```python
from .core import Dependencies
from .core.agent import Agent

def get_billing_summary(customer_id: str, months: int, ws: Dependencies.Client) -> dict:
    """Get a customer's recent billing history with tier breakdown and payment status."""
    return _run_sql(ws, f"SELECT * FROM billing_history WHERE customer_id = '{customer_id}' LIMIT {months}")

agent = Agent(tools=[get_billing_summary])
```

That's it. APX handles the MCP server, `/invocations` loop, tool routing, and MLflow traces.

## Tools

| Tool | Description |
|------|-------------|
| `get_session_context` | Current user identity and governance context. Call first. |
| `get_customer_profile` | Look up a customer by ID or name. |
| `query_ami_readings` | Daily kWh from AMI smart meter readings for a date range. |
| `get_billing_summary` | Billing history with per-tier kWh/charges, taxes, payment status. |
| `get_rate_schedule` | Tier thresholds and per-kWh rates for a rate plan. |
| `compare_months` | Side-by-side billing + AMI comparison between two months with deltas. |

## Genie Code integration

Apps whose name starts with `mcp-` appear automatically in Genie Code → Settings → Custom MCP Servers. No registration needed — just deploy with the right name.

The `/mcp` endpoint uses `StreamableHTTPSessionManager(stateless=True)` from the MCP Python SDK. Each Genie Code request is a self-contained HTTP round-trip.

## Cross-workspace agent catalog

To share this agent across workspaces, register it as a Unity Catalog function:

```sql
-- Any workspace with EXECUTE permission can call the agent
SELECT serverless_stable_s0v155_catalog.explain_my_bill.ask(
  'Why was CUST-0001''s March bill higher than February?'
);

-- Grant access
GRANT EXECUTE ON FUNCTION serverless_stable_s0v155_catalog.explain_my_bill.ask
  TO `data-analysts@company.com`;
```

See [`catalog/register_agent.py`](catalog/register_agent.py) for the full registration script.

## Development

```bash
# Start local dev server (APX)
mcp-cli call apx/start '{}'

# Or directly
uv run uvicorn explain_my_bill_agent.backend.app:app --reload --port 8000
```

## Deploy

```bash
databricks bundle deploy --profile fe-stable
databricks bundle run mcp-explain-my-bill --profile fe-stable
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `AGENT_MODEL` | Serving endpoint for the LLM |
| `WAREHOUSE_ID` | SQL warehouse for Statement Execution API |
| `DEMO_CATALOG` | Unity Catalog catalog name |
| `DEMO_SCHEMA` | Unity Catalog schema name |
| `MLFLOW_EXPERIMENT_NAME` | Experiment path for traces |

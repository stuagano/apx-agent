# Data Triage Agent — Demo Script

## Setup
Open: https://mcp-data-triage-7474652869938903.aws.databricksapps.com/_apx/agent

## Demo 1: General query (tools fire, no pipeline)
**Ask:** "Show me the schema and row count for serverless_stable_qh44kx_catalog.explain_my_bill.customers"

**Expected:** ⚡ `get_table_info` and ⚡ `run_sql_query` pills appear. Agent returns real data (10 rows, 8 columns).

## Demo 2: Investigation pipeline (6-step streaming)
**Ask:** "CUST-0011 is missing from serverless_stable_qh44kx_catalog.explain_my_bill.customers. Investigate why."

**Expected:** Step 1/6 through Step 6/6 stream in. Multiple ⚡ tool pills. Final synthesis with verdict: DATA MISSING — never ingested.

## Demo 3: Sub-agent call (🔗 data-inspector)
**Ask:** "Ask the data_inspector to do a delta_bisect on serverless_stable_qh44kx_catalog.explain_my_bill.customers to find when CUST-0005 first appeared. Use the data_inspector tool, not your local tools."

**Expected:** 🔗 `data_inspector` pill appears. The data-inspector sub-agent runs its own LLM loop with delta forensics tools and returns a reasoned answer about version history.

## Demo 4: Pipeline failure investigation
**Ask:** "Why did the pipeline that writes to serverless_stable_qh44kx_catalog.explain_my_bill.billing_history fail?"

**Expected:** Routes to investigation pipeline. Steps trace lineage, check job history, inspect source paths.

## Key points to highlight
1. **Deterministic routing** — "investigate" → pipeline, general → direct agent. No LLM call for routing.
2. **Streaming progress** — Step 1/6, 2/6... keeps connection alive, user sees progress.
3. **Tool pills in real-time** — ⚡ for local tools, 🔗 for sub-agent calls.
4. **Real data** — SQL queries against Unity Catalog, not mocked.
5. **Sub-agent composition** — triage agent calls data-inspector, which runs its own investigation.

## Access
- Dev UI: `/_apx/agent`
- MCP: `/mcp` (for Claude Desktop, Cursor, Genie Code)
- API: `POST /responses` (Responses API format)
- Agent card: `/.well-known/agent.json`

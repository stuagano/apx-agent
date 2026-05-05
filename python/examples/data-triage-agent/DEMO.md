# Data Triage Agent — Demo Script (fe-stable)

**Live URLs**
- Agent UI: https://mcp-data-triage-7474652869938903.aws.databricksapps.com
- Sub-agent: https://mcp-data-inspector-7474652869938903.aws.databricksapps.com

**Fixture data**
- Catalog: `serverless_stable_qh44kx_catalog.explain_my_bill`
- Tables: `customers` (10 rows), `billing_history` (March 2026 deliberately missing), `ami_hourly_rollups` (data is stale — last update Feb 2026)

## Demo flow (10 minutes)

The demo walks the audience through the agent's two execution paths: a **general agent** for table queries, and a **6-step investigation pipeline** for "something is broken" questions.

### Setup (30 sec)

Open the agent UI. Mention:
- This is a Databricks App built on `apx-agent` (the framework powering Self-Service MCP and other agent work)
- Auth flows on-behalf-of the logged-in user — the agent queries Databricks as you, not as a service principal
- It's two apps: a triage agent (this UI) + a data-inspector sub-agent (specialized in Delta forensics, reused via A2A)

### Act 1 — General queries (3 min)

Show the general agent + sub-agent flow. Each query routes to the data_inspector sub-agent.

**Query 1 — Schema lookup**
```
Show me the schema for serverless_stable_qh44kx_catalog.explain_my_bill.customers
```
*Expected: data_inspector sub-agent returns column names, types, row count of 10.*

Talking point: "It went to a sub-agent. The triage agent didn't have to know how to inspect Delta tables — it delegated to a specialist."

**Query 2 — Specific record**
```
Look up customer CUST-0003 in serverless_stable_qh44kx_catalog.explain_my_bill.customers
```
*Expected: returns Priya Patel, Green Energy Plan, 88 Sunset Blvd.*

Talking point: "Same shape as your CES API today — REST endpoint queries a database. Difference is the agent knows how to construct the query and explain the result."

**Query 3 — Lineage**
```
What upstream sources feed into serverless_stable_qh44kx_catalog.explain_my_bill.customers?
```
*Expected: get_table_lineage tool fires, returns notebook entity.*

Talking point: "This uses Unity Catalog system tables. Nothing custom — it's just `system.access.table_lineage`."

### Act 2 — The investigation pipeline (5 min)

This is the headline. Run the missing-customer scenario.

**Query**
```
CUST-0011 is missing from serverless_stable_qh44kx_catalog.explain_my_bill.customers. Investigate why.
```

The router detects "missing" + "investigate" and routes to the 6-step pipeline. Watch the steps stream:

1. **Data Presence** — confirms CUST-0011 isn't in the table (max ID is CUST-0010). Verdict: DATA MISSING.
2. **Lineage Trace** — finds upstream sources (or notes there are none)
3. **Pipeline Inspector** — checks for failed jobs (will report no jobs found in fe-stable)
4. **Genie Query** — asks Genie Spaces for domain context (will note no relevant Spaces)
5. **Code Inspector** — would search GitHub (currently stubbed — explain that the framework supports it, you wire your repo in production)
6. **Synthesis** — produces a root cause report

Talking point: "Each step is a separate Agent with its own tools and instructions. The SequentialAgent passes conversation history forward, so each step sees what the previous one found. This is *structurally* deterministic — we're not hoping the LLM follows a checklist, the framework guarantees the order."

Talking point: "If the agent had access to your support ticketing system, you could plug this into the ticket-create webhook. The agent does the first triage pass before a human looks at it."

### Act 3 — The data gap variant (2 min)

Show that the same pattern handles different problem shapes.

**Query**
```
There's a data gap in serverless_stable_qh44kx_catalog.explain_my_bill.billing_history for March 2026. Investigate.
```
*Expected: routes to investigation pipeline (hits "data gap" keyword). Step 1 queries for March records, finds zero. Verdict: DATA MISSING for March 2026.*

Talking point: "The router is keyword-based right now — fast and cheap. You can swap it for an LLM router if you need ambiguity handling. The point is it's pluggable."

## Architectural takeaways for Drew

These are the things to call out as you walk through the demo. They're the questions the apx-agent platform forces you to answer at design time:

1. **Single agent vs orchestrated pipeline.** This agent does both — a general agent for ad-hoc, a deterministic pipeline for investigations. You pick per use case.
2. **Sub-agents as A2A boundary.** Data-inspector is reusable across other agents (contract parsing, entity resolution, etc.). The HTTP boundary is the contract. This is how you build platform tools without coupling agents to each other.
3. **Tool definitions are just typed Python functions.** No special class, no annotation magic — just `Annotated` typing. That makes them trivial to test.
4. **Workspace dependency injection.** `Dependencies.Workspace` gives you OBO-authenticated SDK access in every tool. No token sprawl.
5. **Eval cases as code.** `eval/eval_dataset.py` is the regression suite. Same file format as MLflow agent eval.

## Known stubs / limitations to mention

Be transparent about what's not wired up in fe-stable:

- **GitHub tools are stubs** — they return `{"stub": true, ...}` because the framework supports it but we haven't wired a repo. Drew can wire this with PyGithub + a service-account token.
- **No real failed jobs / lineage entries in fe-stable** — the system tables don't have rich data here. In Uplight's environment, lineage will show real entries because actual pipelines write to actual tables.
- **No Genie Spaces configured** — the agent will report none found. In Uplight, this will surface real Spaces.

Frame these as "the agent doesn't pretend things exist that don't" rather than as missing features.

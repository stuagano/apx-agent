# apx-agent Examples

Quick index — what each example does and which direction data/auth flows.

## Agents & Apps

| Example | What it does |
|---------|-------------|
| [bakehouse-agent](./bakehouse-agent/) | **DataAgent + RouterAgent showcase** over the built-in `samples.bakehouse` dataset. Routes "how are sales?" to a governed `DataAgent` over the sales tables and "what do customers say?" to a `DataAgent` over the review text. **Zero setup** — both leaves use SQL (serverless warehouse auto-discovered), no Vector Search endpoint required; an optional README "Upgrade" swaps the reviews leaf to semantic Vector Search. |
| [data-triage-agent](./data-triage-agent/) | Investigates missing Databricks data — 6-step SequentialAgent pipeline (presence → lineage → pipeline → genie → code → synthesis). Delegates SQL + Delta forensics to the data-inspector sub-agent via A2A. Deploys to either Apps (`--target apps`) or Model Serving (`--target model-serving`). |
| [data-inspector](./data-inspector/) | SQL + Delta forensics — standalone or sub-agent via MCP/A2A |
| [contract-parsing-agent](./contract-parsing-agent/) | Extracts structured terms from contract PDFs into Unity Catalog |
| [entity-resolution-agent](./entity-resolution-agent/) | Fuzzy-match account resolution via Vector Search — HandoffAgent (Supervisor + Evaluator) |
| [eligibility-agent](./eligibility-agent/) | Document-based program eligibility assessment (W-2s, paystubs) |
| [explain-my-bill-agent](./explain-my-bill-agent/) | Energy billing Q&A — looks up customer profiles, AMI smart-meter data, billing history, and rate schedules from Unity Catalog. Ships a `catalog/register_agent.py` to expose the agent as a UC function (SQL-callable). |
| [shortage-intelligence-agent](./shortage-intelligence-agent/) | Detects demand shortage signals, reports to sourcing + sales |
| [apx-builder](./apx-builder/) | **Natural-language agent builder** — describe an agent, the builder scaffolds + deploys it. Tools: `search_tables`, `list_genie_spaces`, `scaffold_project`, `deploy_agent`, `poll_deployment`. |
| [agent-hub](./agent-hub/) | Central registry + chat UI for all deployed apx-agent apps |
| [account-search-service](./account-search-service/) | Standalone fuzzy account lookup API — callable by other agents |
| [afr-enrollment-api](./afr-enrollment-api/) | Deterministic AFR enrollment pipeline — no LLM, high throughput |
| [memory_demo](./memory_demo/) | **MemoryStore + ExampleStore** — seeded memories, few-shot examples, recall/remember tools, system prompt assembled per turn. Two run modes: local in-process (`app.py`) + Databricks Apps deploy (`agent_server/` + `databricks.yml`). Verified live on fe-stable. |
| [customer_triage](./customer_triage/) | **HandoffAgent** over four LlmAgents (triage / billing / account / technical) with memory wired into the account specialist. Demonstrates the full apx-agent surface: `@tool(uc=...)` decorator, `Dependencies.Workspace` for OBO, `genie_tool` / `vector_search_tool`, peer handoffs, principal-keyed memory recall across handoffs. Apps-target deploy verified live on fe-stable. |
| [hubspot-complaints-agent](./hubspot-complaints-agent/) | **DataAgent + scheduled batch job** — summarizes HubSpot support-ticket complaints by month from a synced Unity Catalog table. Chat interactively ("summarize complaints for June"), or let the monthly `hubspot-complaint-summary` Databricks Job write an exact ticket count (SQL) + qualitative theme summary (`run_once`) to a `complaint_summaries` Delta table automatically. |

## MCP Servers & Shared Libraries

| Example | What it does |
|---------|-------------|
| [databricks-mcp-server](./databricks-mcp-server/) | Exposes ~44 Databricks operations as MCP tools over stdio |
| [databricks-skills](./databricks-skills/) | Claude Code skills for Databricks workflows |
| [databricks-tools-core](./databricks-tools-core/) | Core Databricks auth and tool primitives shared across examples |

## Slack Integration — Two Opposite Directions

These two patterns are **not interchangeable**. Pick based on which direction auth flows:

| Pattern | Direction | Auth flows to | Use when |
|---------|-----------|---------------|----------|
| [slack-uc-mcp](./slack-uc-mcp/) ⚠️ *Private Preview* | Databricks → Slack | Slack OAuth2 (via UC u2m) | Genie / Agent Bricks / custom agent reads Slack as the calling user — UC handles per-user OAuth, exchange, refresh. Ships `slack_history_tool` + `slack_post_tool` apx-agent factories that wrap the pattern with `ResourceSpec` declarations and request-context user-identity resolution. |
| [slack-agent](./slack-agent/) | Slack → Databricks | Databricks OIDC | Slack-initiated flow — slash command in Slack runs an agent with the Slack user's Databricks identity |

**Default to [slack-uc-mcp](./slack-uc-mcp/)** for most Databricks ↔ Slack integrations. It's a Databricks-native config (UC External Connection pointed at `mcp.slack.com/mcp`) — no custom app, no token storage, no refresh logic. Each Databricks user authorizes Slack OAuth once; UC stores and replays their token. Governance and per-user channel permissions are enforced automatically.

**[slack-agent](./slack-agent/)** is the right choice only when the flow originates *in Slack* (slash command) and the agent must run as the Slack user's *Databricks* identity. It's a custom apx-agent app you deploy, and you own the OAuth + token-store code.

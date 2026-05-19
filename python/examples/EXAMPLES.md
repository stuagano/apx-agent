# apx-agent Examples

Quick index — what each example does and which direction data/auth flows.

## Reference Implementation

| Example | What it does |
|---------|-------------|
| [databricks-builder-app](./databricks-builder-app/) | **Full-stack reference app** — multi-user chat UI with streaming, session resumption, per-request auth, and MLflow tracing. Shows how to build a production app on `ClaudeSDKClient` + `McpSSEServerConfig`. |

## Agents & Apps

| Example | What it does |
|---------|-------------|
| [data-triage-agent](./data-triage-agent/) | Investigates missing Databricks data — 6-step SequentialAgent pipeline |
| [data-inspector](./data-inspector/) | SQL + Delta forensics — standalone or sub-agent via MCP/A2A |
| [contract-parsing-agent](./contract-parsing-agent/) | Extracts structured terms from contract PDFs into Unity Catalog |
| [entity-resolution-agent](./entity-resolution-agent/) | Fuzzy-match account resolution via Vector Search |
| [eligibility-agent](./eligibility-agent/) | Document-based program eligibility assessment (W-2s, paystubs) |
| [shortage-intelligence-agent](./shortage-intelligence-agent/) | Detects demand shortage signals, reports to sourcing + sales |
| [agent-hub](./agent-hub/) | Central registry + chat UI for all deployed apx-agent apps |
| [account-search-service](./account-search-service/) | Standalone fuzzy account lookup API — callable by other agents |
| [afr-enrollment-api](./afr-enrollment-api/) | Deterministic AFR enrollment pipeline — no LLM, high throughput |
| [memory_demo](./memory_demo/) | **MemoryBank + ExampleStore** — seeded memories, few-shot examples, recall/remember tools, system prompt assembled per turn. Standalone runnable demo of the durable-context surface. |

## MCP Servers & Shared Libraries

| Example | What it does |
|---------|-------------|
| [databricks-mcp-server](./databricks-mcp-server/) | Exposes 71 Databricks operations as MCP tools — used by `databricks-builder-app` via SSE |
| [databricks-skills](./databricks-skills/) | Claude Code skills for Databricks workflows — loaded by `databricks-builder-app` |
| [databricks-tools-core](./databricks-tools-core/) | Core Databricks auth and tool primitives shared across examples |

## Slack Integration — Two Opposite Directions

These two patterns are **not interchangeable**. Pick based on which direction auth flows:

| Pattern | Direction | Auth flows to | Use when |
|---------|-----------|---------------|----------|
| [slack-uc-mcp](./slack-uc-mcp/) ⚠️ *Private Preview* | Databricks → Slack | Slack OAuth2 (via UC u2m) | Genie / Agent Bricks / custom agent reads Slack as the calling user — UC handles per-user OAuth, exchange, refresh |
| [slack-agent](./slack-agent/) | Slack → Databricks | Databricks OIDC | Slack-initiated flow — slash command in Slack runs an agent with the Slack user's Databricks identity |

**Default to [slack-uc-mcp](./slack-uc-mcp/)** for most Databricks ↔ Slack integrations. It's a Databricks-native config (UC External Connection pointed at `mcp.slack.com/mcp`) — no custom app, no token storage, no refresh logic. Each Databricks user authorizes Slack OAuth once; UC stores and replays their token. Governance and per-user channel permissions are enforced automatically.

**[slack-agent](./slack-agent/)** is the right choice only when the flow originates *in Slack* (slash command) and the agent must run as the Slack user's *Databricks* identity. It's a custom apx-agent app you deploy, and you own the OAuth + token-store code.

# apx-agent Examples

Quick index — what each example does and which direction data/auth flows.

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

## MCP Servers

| Example | What it does |
|---------|-------------|
| [databricks-mcp-server](./databricks-mcp-server/) | Exposes Databricks operations as MCP tools for Claude Code / AI assistants |
| [databricks-skills](./databricks-skills/) | Claude Code skills for Databricks workflows |
| [databricks-tools-core](./databricks-tools-core/) | Core Databricks tool primitives shared across examples |

## Slack Integration — Two Opposite Directions

These two patterns are **not interchangeable**. Pick based on which direction auth flows:

| Pattern | Direction | Auth flows to | Use when |
|---------|-----------|---------------|----------|
| [slack-agent](./slack-agent/) | Slack → Databricks | Databricks OIDC | Slack user wants to query Databricks — agent runs with their Databricks identity |
| UC External Connection | Databricks → Slack | Slack OAuth2 | Genie/agent wants to read Slack — respects per-user Slack channel permissions |

**slack-agent** is an apx-agent app you deploy. The Slack user authenticates to Databricks and the agent runs as them.

**UC External Connection** (`mcp.slack.com`) is a Databricks-native config — no custom app needed. Each Databricks user authorizes Slack OAuth once; UC stores and replays their token. This is the right pattern for giving Genie read-access to Slack while preserving per-user permissions.

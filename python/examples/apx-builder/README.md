# apx-builder

A **natural-language agent builder** for apx-agent projects. Describe the agent
you want in plain English and apx-builder scaffolds a new apx-agent project,
deploys it as a Databricks App, and hands back a live URL — entirely through
conversation, with no code required from the user.

apx-builder is itself an apx-agent app: a FastAPI server (`app.py`) wrapping an
`Agent` whose tools do the scaffolding and deployment work, plus a React chat
frontend served from `client/`.

## Tools

The agent is wired with five tools (defined in `tools/`):

| Tool | Description |
|------|-------------|
| `search_tables` | Search Unity Catalog for tables matching a term. Returns `catalog.schema.table` identifiers (with comments) so the agent can suggest data sources. |
| `list_genie_spaces` | List the Genie spaces available in the workspace. Returns the `id` and `name` of each space. |
| `scaffold_project` | Generate a complete apx-agent project (app, agent, tools, config) for the described use case and upload it to a Databricks workspace path. **Gated:** human approval required (`PolicyGate` ASK). Inputs (`app_name`, `use_case`, table names) are validated before codegen. |
| `deploy_agent` | Create the Databricks App if it doesn't exist, then deploy the scaffolded project from its workspace path. Returns the app name. **Gated:** human approval required. |
| `poll_deployment` | Wait for the freshly deployed agent to come fully live — API readiness (RUNNING + SUCCEEDED) then an HTTP health check — and return the app URL when it's ready. |

## Safety

- **Input validation** — `app_name` must be a Databricks-safe slug; `use_case`
  and table/Genie identifiers reject quotes, backslashes, and newlines before
  they are interpolated into generated source.
- **Approval gate** — `scaffold_project` and `deploy_agent` are wrapped in a
  `PolicyGate` ASK. The turn pauses until a human approves via the apx
  approvals UI (`/_apx/approvals`); discovery tools (`search_tables`,
  `list_genie_spaces`, `poll_deployment`) are not gated.

## Run locally

```bash
cd apx-builder
uv sync
uv run uvicorn app:app --reload
```

The agent is available at `http://localhost:8000`. The React chat UI is served
at `/` (from `client/dist` when built); the apx-agent A2A surface is available
at `/responses`, `/.well-known/agent.json`, and `/health`.

Authentication uses your local Databricks credentials (`DATABRICKS_HOST` +
`DATABRICKS_TOKEN`, or a `DATABRICKS_CONFIG_PROFILE` from `~/.databrickscfg`),
which the tools use to read Unity Catalog / Genie and to create and deploy the
target app.

## Deploy

`app.yml` runs `uvicorn app:app` on Databricks Apps. Deploy with the Databricks
CLI / Asset Bundle as with the other examples.

## License

© Databricks, Inc. See [LICENSE.md](../LICENSE.md).

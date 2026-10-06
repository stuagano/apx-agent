# PLG Discovery

Users share an organization URL and operating documents, then a governed APX
agent interviews them and builds a staged technology blueprint.

`agent.py` declares the agent and Python tools. `app.py` mounts the React wizard
directly on the native DurableAgentServer. The browser streams
`POST /api/invocations`, then reads the persisted result before accepting its
structured artifacts. Managed sessions retain conversation history. **New
conversation** starts a fresh session without deleting the previous one.

The declaration selects `durable_agent_server` and managed sessions. Agent Bricks
owns the Runtime Store, Session Store provisioning, and MLflow tracing. The
bundle retains only this example's wheel and browser build steps. There is no
Node agent server, Python tool bridge, or process-local developer override API.

## Check locally

```bash
npm install --prefix client
npm test --prefix client
npm run build --prefix client
uv run pytest -q
```

These checks need no live workspace. Text-like onboarding files are read in the
browser; binary files are represented by filename.

## Deploy

Install `apx-agent[agentbricks]`, enable the required agent services in the chosen
workspace, and build the browser client. APX checks prerequisites before building
or provisioning:

```bash
npm run build --prefix client
uv run apx-agent agents deploy . --target apps --profile <profile>
```

The app name is `agent-bricks-discovery`. For local execution after provisioning
its managed stores, use the intended Databricks profile and run
`DATABRICKS_APP_PORT=8000 uv run python -m app`. Local execution still connects
to the remote managed session store. Do not substitute an in-memory session
backend to make a missing store appear to work.

## Product files

- `prompts/discovery_playbook.md`: staged interview and artifact contract
- `prompts/skills/nonprofit_discovery.md`: callable discovery methodology
- `data/component_catalog.json`: blueprint options
- `nonprofit-saas-landscape-2025-2026.md`: grounding research brief

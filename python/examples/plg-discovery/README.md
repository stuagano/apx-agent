# PLG Discovery

A barebones nonprofit technology-discovery app: users share an organization URL and operating documents, then a governed APX agent interviews them and builds a staged technology blueprint.

## Architecture

The app uses one native APX declaration and one generated TypeScript AppKit host:

```text
React PLG wizard
  └─ POST /api/agents/chat (AppKit SSE + real threads)
       └─ generated APX AppKit host
            ├─ Databricks foundation model
            ├─ governed Python tools
            └─ MLflow/AppKit OTLP traces, metrics, and logs
```

- [`agent.py`](agent.py) declares the discovery agent, web-research tool, and shipped nonprofit-discovery skill.
- [`client/`](client/) keeps the original PLG wizard, validates `apx-artifact` output, and renders the profile, current-systems gate, domain relevance, and blueprint.
- [`server/grounding.py`](server/grounding.py) composes the playbook, component catalog, skill inventory, and research brief into the system prompt.
- [`databricks.yml`](databricks.yml) selects the shared native AppKit host and configures MLflow telemetry.

The small **Dev** launcher is enabled by default in local and deployed builds. Its five inline tabs edit the live AppKit agent:

1. Config
2. Instructions
3. Tools and markdown skills
4. AppKit sessions
5. Effective prompt

Set `APX_DEV_UI=0` on the App only when the launcher and its routes should be disabled. Overrides are process-local and reset on restart or redeploy.

## Local checks

From `python/examples/plg-discovery`:

```bash
uv sync
npm ci --prefix client
npm test --prefix client
npm run build --prefix client
uv run pytest -q
```

The tests do not require a live Databricks workspace. Text-like onboarding files are read in the browser; binary files are represented by filename rather than uploaded to a second backend.

## Deploy

Build the client, then deploy through APX so it stages the local APX wheel, Python tool bridge, and generated TypeScript AppKit host:

```bash
npm run build --prefix client
uv run apx-agent agents deploy . \
  --target apps \
  --profile <profile> \
  --var catalog=<catalog> \
  --var schema=<schema> \
  --var sql_warehouse_id=<warehouse-id> \
  --var llm_endpoint_name=databricks-claude-sonnet-4-6
```

The deploy command resolves or creates the MLflow experiment unless `--var mlflow_experiment_id=<id>` is supplied. Always pass the intended CLI profile explicitly; this example has no profile default.

After deployment:

```bash
databricks apps get plg-discovery --profile <profile> -o json
databricks apps logs plg-discovery --follow --profile <profile>
```

## Product files

- `prompts/discovery_playbook.md` — staged interview and artifact contract
- `prompts/skills/nonprofit_discovery.md` — callable discovery methodology
- `data/component_catalog.json` — Databricks-hosted blueprint options
- `nonprofit-saas-landscape-2025-2026.md` — grounding research brief
- `docs/superpowers/` — original product spec and implementation plan

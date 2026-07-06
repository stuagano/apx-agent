# hubspot-complaints-agent

apx-agent project targeting Databricks Apps.

## Setup
```bash
uv sync
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
uv run apx-agent run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI (chat, edit,
#   topology, traces, eval, setup wizard). Edit agent.py in your IDE;
#   --reload picks up changes. See docs/getting-started.md for the walkthrough.
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
uv run apx-agent deploy --target apps  # validates, deploys, runs the bundle
```

## Promoting to another environment
`databricks.yml` ships `dev` (default), `staging`, and `prod` targets. All
three default to the same workspace/catalog/schema until you customize one —
promoting is an explicit edit, not a hidden step.

The `catalog`/`schema` override below only applies to scaffolds with a data
source (`--template data`/`coworker`) — a base `LlmAgent` scaffold has no
`catalog`/`schema` variables to override, so it promotes via
`--bundle-target`/`--profile` alone.

To point `staging` at its own UC catalog/schema, add a `variables:` override
under its target in `databricks.yml`:

```yaml
targets:
  staging:
    mode: production
    variables:
      catalog: <your-staging-catalog>
      schema: <your-staging-schema>
    resources:
      apps:
        hubspot-complaints-agent:
          name: hubspot-complaints-agent
```

Then deploy to the staging workspace by pointing `--profile` at its
Databricks CLI profile:

```bash
uv run apx-agent deploy --target apps --bundle-target staging --profile <staging-profile>
```

Repeat the same recipe for `prod` (its own `variables:` override + its own
`--profile`). `--bundle-target` selects which `databricks.yml` target to
deploy; `--profile` selects which workspace credentials to use — they're
independent, so forgetting to change `--profile` when you add a `staging`
override just redeploys to the same workspace under a different catalog.

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `hubspot-complaints-agent` — Databricks bundle
> resource references like `${resources.experiments.hubspot-complaints-agent_experiment.id}`
> are easier to read with snake_case names.

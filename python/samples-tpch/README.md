# samples-tpch

apx-agent project targeting Databricks Apps.

## Setup
```bash
uv sync --group dev
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
uv run apx-agent agents run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI (chat, edit,
#   topology, traces, eval, setup wizard). Edit agent.py in your IDE;
#   --reload picks up changes. See docs/getting-started.md for the walkthrough.
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
uv run apx-agent agents deploy --target apps  # validates, deploys, runs the bundle
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
        samples-tpch:
          name: samples-tpch
```

Then deploy to the staging workspace by pointing `--profile` at its
Databricks CLI profile:

```bash
uv run apx-agent agents deploy --target apps --bundle-target staging --profile <staging-profile>
```

Repeat the same recipe for `prod` (its own `variables:` override + its own
`--profile`). `--bundle-target` selects which `databricks.yml` target to
deploy; `--profile` selects which workspace credentials to use — they're
independent, so forgetting to change `--profile` when you add a `staging`
override just redeploys to the same workspace under a different catalog.

## Upgrade apx-agent
Bump the `@ref` in `pyproject.toml` (tag or commit SHA), then:
```bash
uv lock --upgrade-package apx-agent
uv sync --group dev
uv run apx deploy --target apps
```
Always `uv sync` before deploy — `apx deploy` bundles the *running*
install into the App wheel. See the framework's `docs/upgrade.md`.

## CI/CD
Scaffolded pipelines follow a three-stage branch flow (``dev`` is laptop-only):

| Trigger | What runs |
|---|---|
| PR → `main` | unit tests |
| PR → `release` | unit tests + deploy `--bundle-target staging` |
| Push to `release` | gated deploy `--bundle-target prod` |

Configure secrets `DATABRICKS_HOST_{STAGING,PROD}`,
`DATABRICKS_CLIENT_ID_*`, `DATABRICKS_CLIENT_SECRET_*` (and optional
`FRAMEWORK_REPO_TOKEN` for a private apx-agent pin). Create a GitHub
Environment named `prod` with required reviewers. Full walkthrough:
framework `docs/deploy-cicd.md`.

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `samples-tpch` — Databricks bundle
> resource references like `${resources.experiments.samples-tpch_experiment.id}`
> are easier to read with snake_case names.

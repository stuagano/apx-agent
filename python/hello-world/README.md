# hello-world

apx-agent project targeting Databricks Apps.

## Install (once)
```bash
pip install apx-agent
```

## Setup
```bash
uv sync
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
apx-agent run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI. Edit agent.py;
#   --reload picks up changes. See docs/getting-started.md for the walkthrough.
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
apx-agent deploy --target apps
```

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `hello-world` — Databricks bundle
> resource references like `${resources.experiments.hello-world_experiment.id}`
> are easier to read with snake_case names.

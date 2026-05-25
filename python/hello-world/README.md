# hello-world

apx-agent project targeting Databricks Apps.

## Setup
```bash
uv sync
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
uv run uvicorn agent_server.start_server:app --host 127.0.0.1 --port 8000
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy

If the framework wheel hasn't changed, just deploy the source:
```bash
cd python/hello-world
databricks bundle deploy --profile <profile>
databricks apps deploy hello-world \
  --source-code-path /Workspace/Users/<you>/.bundle/hello-world/dev/files/.build \
  --profile <profile>
```

If you've rebuilt the wheel (e.g. after editing `builder-ui` or framework code), run `make wheel` from the repo root first — it rebuilds the frontend, packages the wheel, copies it into place, and patches `.build/uv.lock` with the new hash:
```bash
# from repo root
make wheel
cd python/hello-world
databricks bundle deploy --profile <profile>
databricks apps deploy hello-world \
  --source-code-path /Workspace/Users/<you>/.bundle/hello-world/dev/files/.build \
  --profile <profile>
```

> **Note:** `databricks bundle deploy` only uploads workspace files. You must also run `databricks apps deploy` to trigger the app to pick up the new code.

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `hello-world` — Databricks bundle
> resource references like `${resources.experiments.hello-world_experiment.id}`
> are easier to read with snake_case names.

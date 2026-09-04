# Deploying without `apx deploy` (raw DABs path)

`apx deploy` is the recommended deploy path — it handles wheel pinning,
`pyproject.toml` sanitization, and `.build/` staging automatically. But if
you're wiring `databricks bundle deploy` + `databricks apps deploy` directly
(e.g. from a CI pipeline that already owns the DABs lifecycle), there are a
few gaps to fill manually.

## The core problem: `apx-agent` is not on public PyPI

`apx-agent` lives on Databricks' internal PyPI proxy. The Databricks Apps
container environment may not have access to that proxy, depending on the
workspace network config. The result is:

```
[BUILD] No dependencies file found. Skipping installation.
ModuleNotFoundError: No module named 'apx_agent'
```

The container IS running `uv sync` or `pip install` (other packages like
`uvicorn` land fine), but `apx-agent` is silently skipped or unreachable.

## Fix: bundle the wheel + use a start script

**Step 1** — Download the wheel into the project:

```bash
pip download apx-agent==0.5.0 --no-deps -d .
# → apx_agent-0.5.0-py3-none-any.whl
```

**Step 2** — Add `start.sh` that installs it before launching uvicorn:

```bash
#!/bin/bash
set -e
pip install ./apx_agent-0.5.0-py3-none-any.whl --quiet
exec uvicorn agent_server.start_server:app \
    --host 0.0.0.0 \
    --port "${DATABRICKS_APP_PORT:-8080}"
```

**Step 3** — Point `app.yml` at the script (command must be a **list**, not a string):

```yaml
command:
  - bash
  - start.sh
env:
  - name: APX_MODEL
    value: databricks-claude-sonnet-4-6
  - name: APX_SMOKE_MODE
    value: "0"
```

**Step 4** — Commit both the wheel and `start.sh`. The bundle deploy uploads
everything under the project directory.

> **Note:** Add `apx_agent-*.whl` to `.gitignore` if you don't want it in
> source control — but then your CI pipeline must download it before bundling.

## app.yml command format

The `command` field must be a YAML list. A bare string is silently misread:

```yaml
# ❌ WRONG — silently ignored on some runtime versions
command: uvicorn agent_server.start_server:app --host 0.0.0.0

# ✅ CORRECT
command:
  - uvicorn
  - agent_server.start_server:app
  - --host
  - 0.0.0.0
  - --port
  - $DATABRICKS_APP_PORT
```

## Entrypoint: `agent_server.start_server:app`, not `app:app`

The `app.py` file at the project root is a **local dev helper** (adds the dev
UI, CORS, `/_apx/` routes). The production entrypoint for Databricks Apps is
`agent_server/start_server:app`, which uses `create_app` directly without the
dev-only extras.

## genie_tool with empty space ID

If `PORTFOLIO_GENIE_SPACE_ID` (or equivalent) is empty at module load time,
`genie_tool("")` raises at import. Guard module-level calls:

```python
_GENIE_SPACE = os.environ.get("PORTFOLIO_GENIE_SPACE_ID", "")

if _GENIE_SPACE:
    from apx_agent import genie_tool
    query_portfolio = genie_tool(_GENIE_SPACE, name="query_portfolio", description="...")
else:
    def query_portfolio(question: str) -> str:
        return "Portfolio Genie space not configured (set PORTFOLIO_GENIE_SPACE_ID)."
```

## Querying the deployed agent

The agent exposes `POST /responses` using the MLflow ResponsesAgent contract:

```bash
curl -X POST https://<app-url>/responses \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "input": [{"role": "user", "content": "Why is ACME Re September data missing?"}],
    "stream": false
  }'
```

The response `output` array contains interleaved `function_call`,
`function_call_output`, and `message` items. Parse with the pattern in
`query.py` (included in examples that use this deploy path) or use
`apx agents query` (see below).

## When to use `apx deploy` instead

Use the raw DABs path only when you own the DABs lifecycle from CI. For
local development and one-off deploys, `apx deploy --profile <PROFILE>` is
simpler — it handles all of the above automatically.

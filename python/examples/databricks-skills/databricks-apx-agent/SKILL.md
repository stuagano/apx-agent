---
name: databricks-apx-agent
description: "Build Genie-backed agents as Databricks Apps using the apx-agent SDK. Use this skill when the user asks to build an agent, chatbot, or AI assistant that wraps one or more Genie Spaces and should run as a deployed Databricks App with a chat UI and embedded developer page."
---

# apx-agent SDK — Genie App Pattern

Build Databricks Apps that expose a Genie Space through a conversational AI agent. The `apx_agent` SDK provides:

- `create_app()` — FastAPI app factory with agent protocol wired in
- `/_apx/agent` — embedded developer UI with live tracing
- `genie_tool()` — Genie space as an agent tool (async, OBO, declares `attach_resources`)
- `Dependencies.Headers` / `Dependencies.UserClient` — OBO auth (user's token passed through)

## Scaffolding Steps

Run these in order before writing any files:

```bash
# 1. Copy the apx-agent wheel into your project directory so pip can install it
cp .claude/skills/databricks-apx-agent/apx_agent-0.18.0-py3-none-any.whl ./

# 2. Create the static directory for the frontend placeholder
mkdir -p static
```

## Required Files

### `app.py`

Replace every `<...>` placeholder before deploy. A literal
`description="<One-line description...>"` breaks A2A routing if copied
verbatim — fill in a real one-line description of what the agent does.

```python
"""Sales insights agent — Genie-backed apx agent."""

import os

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apx_agent import Agent, AgentConfig, create_app, genie_tool

GENIE_SPACE_ID = os.environ["GENIE_SPACE_ID"]

SYSTEM_PROMPT = """You are an AI assistant that answers questions about sales pipeline data.
Use the ask_genie tool to retrieve data. Always provide clear, accurate answers."""

# genie_tool is async + OBO via Dependencies.UserClient, and attaches a
# ResourceSpec("genie_space", ...) so deploy/resource collection sees the space.
ask_genie = genie_tool(
    GENIE_SPACE_ID,
    name="ask_genie",
    description="Query sales pipeline and opportunity data via Genie.",
)

agent = Agent(tools=[ask_genie], instructions=SYSTEM_PROMPT)

config = AgentConfig(
    name="sales-insights-agent",
    description="Answers natural-language questions about sales pipeline data via Genie.",
)

app = create_app(agent, config=config)


# --- Static files (serve at /static, not /, so POST /responses is not intercepted) ---
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")
```

**Why `genie_tool()` (not a hand-rolled Genie poll loop)?**
`genie_tool()` already runs async under the caller's OBO `WorkspaceClient`,
declares the Genie space via `attach_resources` / `ResourceSpec`, and surfaces
failures as `ToolError`. Do not reimplement `/start_conversation` + poll with
raw `httpx` unless you have a reason the factory cannot cover — and if you do,
call `attach_resources(fn, [ResourceSpec("genie_space", space_id)])` yourself.

### `requirements.txt`

```
fastapi>=0.100.0
uvicorn>=0.23.0
databricks-sdk>=0.20.0
pydantic-settings>=2.0
httpx>=0.27.0
mcp>=1.0.0
databricks-openai>=0.1.0
openai-agents>=0.13.0
./apx_agent-0.18.0-py3-none-any.whl
```

> **Note:** `apx-agent` is not on PyPI. Build a wheel locally and include it in the source directory:
> ```bash
> cd /path/to/apx-agent/python && uv build --wheel --out-dir /tmp/my-agent/
> ```
> Upload the `.whl` file alongside `app.py`. The `./` prefix in requirements.txt tells pip to install from the local file.

### `app.yaml`

```yaml
command:
  - uvicorn
  - app:app
  - --host=0.0.0.0
  - --port=8000

env:
  - name: GENIE_SPACE_ID
    value: "01234567-89ab-cdef-0123-456789abcdef"
```

### `static/index.html`

Minimal placeholder — the real UI is at `/_apx/agent`.

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Sales Insights Agent</title>
  <meta http-equiv="refresh" content="0; url=/_apx/agent">
</head>
<body>Redirecting to <a href="/_apx/agent">agent UI</a>...</body>
</html>
```

## Deployment Checklist

1. **Grant Genie Space permissions** — run this BEFORE deploying:
   ```python
   from databricks.sdk import WorkspaceClient
   from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

   ws = WorkspaceClient()
   ws.permissions.update(
       request_object_type="genie",
       request_object_id="01234567-89ab-cdef-0123-456789abcdef",
       access_control_list=[
           AccessControlRequest(group_name="users", permission_level=PermissionLevel.CAN_RUN)
       ]
   )
   ```
   Without this, users' OBO tokens will be rejected by the Genie API.

2. **Build and include the `apx-agent` wheel** in the source directory

3. **Upload all files** to the Databricks workspace via `manage_workspace_files`

4. **Deploy** via `create_and_deploy_app`

5. **Verify** the app is at `SUCCEEDED` / `RUNNING` via `get_app_status`

6. **Open `/_apx/agent`** — the embedded developer UI shows live tool traces

## What `/_apx/agent` Provides

- Chat UI connected to the agent's `/responses` SSE endpoint
- Live trace panel — every `ask_genie` call shows input, output, latency
- Tool inspector at `/_apx/tools`
- Health probe at `/_apx/probe`
- Agent card at `/.well-known/agent.json`

## Multiple Genie Spaces

Add one `genie_tool(...)` per space (distinct `name=` / `description=`) and register all as tools:

```python
ask_genie_sales = genie_tool(
    SALES_SPACE_ID,
    name="ask_genie_sales",
    description="Query sales pipeline and opportunity data.",
)
ask_genie_support = genie_tool(
    SUPPORT_SPACE_ID,
    name="ask_genie_support",
    description="Query customer support tickets and case history.",
)

agent = Agent(
    tools=[ask_genie_sales, ask_genie_support],
    instructions=SYSTEM_PROMPT,
)
```

## What NOT to Do

```python
# ❌ WRONG — stub description breaks A2A agent-card routing if copied literally
config = AgentConfig(
    name="<app-name>-agent",
    description="<One-line description of what the agent does>",
)

# ❌ WRONG — hand-rolled Genie poll without attach_resources
async def ask_genie(question: str, headers: Dependencies.Headers) -> str:
    ...  # raw httpx start_conversation + poll — no ResourceSpec declared

# ❌ WRONG — this is for scripts/notebooks, not Databricks Apps
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

# ❌ WRONG — app.mount("/", StaticFiles(...)) intercepts POST /responses
app.mount("/", StaticFiles(directory="static"), html=True)

# ✅ RIGHT — genie_tool (declares resources) + mount at /static
ask_genie = genie_tool(GENIE_SPACE_ID, description="Query domain data via Genie.")
app.mount("/static", StaticFiles(directory="static"), name="static")
```

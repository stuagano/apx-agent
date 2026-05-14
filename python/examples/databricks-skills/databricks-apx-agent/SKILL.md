---
name: databricks-apx-agent
description: "Build Genie-backed agents as Databricks Apps using the apx-agent SDK. Use this skill when the user asks to build an agent, chatbot, or AI assistant that wraps one or more Genie Spaces and should run as a deployed Databricks App with a chat UI and embedded developer page."
---

# apx-agent SDK — Genie App Pattern

Build Databricks Apps that expose a Genie Space through a conversational AI agent. The `apx_agent` SDK provides:

- `create_app()` — FastAPI app factory with agent protocol wired in
- `/_apx/agent` — embedded developer UI with live tracing
- `genie_tool()` / async OBO variant — Genie space as an agent tool
- `Dependencies.Headers` — OBO auth (user's token passed through)

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

```python
"""<App Name> — Genie-backed apx agent."""

import asyncio
import os
import logging
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from apx_agent import Agent, AgentConfig, Dependencies, create_app

logger = logging.getLogger(__name__)

GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "<space_id>")

SYSTEM_PROMPT = """You are an AI assistant that answers questions about <domain>.
Use the ask_genie tool to retrieve data. Always provide clear, accurate answers."""


async def ask_genie(question: str, headers: Dependencies.Headers) -> str:
    """Query <domain> data via Genie Space."""
    token = headers.token.get_secret_value() if headers.token else ""
    host = f"https://{headers.host}" if headers.host else os.environ.get("DATABRICKS_HOST", "")
    auth = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{host}/api/2.0/genie/spaces/{GENIE_SPACE_ID}/start_conversation",
            headers={"Authorization": auth},
            json={"content": question},
        )
        resp.raise_for_status()
        data = resp.json()
        conversation_id = data.get("conversation_id", "")
        message_id = data.get("message_id", "")

        msg_data: dict = {}
        for _ in range(30):
            poll = await client.get(
                f"{host}/api/2.0/genie/spaces/{GENIE_SPACE_ID}/conversations/{conversation_id}/messages/{message_id}",
                headers={"Authorization": auth},
            )
            msg_data = poll.json()
            status = msg_data.get("status", "")
            if status == "COMPLETED":
                break
            if status in ("FAILED", "CANCELLED"):
                return f"Genie query {status.lower()}."
            await asyncio.sleep(2)

        if msg_data.get("status") not in ("COMPLETED", "FAILED", "CANCELLED"):
            return "Genie query timed out after 60 seconds. Please try again."

        for att in msg_data.get("attachments", []):
            text_block = att.get("text", {})
            if text_block.get("content"):
                return str(text_block["content"])
        return ""


agent = Agent(tools=[ask_genie], instructions=SYSTEM_PROMPT)

config = AgentConfig(
    name="<app-name>-agent",
    description="<One-line description of what the agent does>",
)

app = create_app(agent, config=config)


# --- Static files (serve at /static, not /, so POST /responses is not intercepted) ---
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")
```

**Why `Dependencies.Headers` instead of `genie_tool()`?**
The built-in `genie_tool()` uses synchronous `ws.api_client.do()` which blocks uvicorn's event loop, stalling the SSE stream back to the browser. The async `httpx` pattern above is non-blocking.

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
    value: "<space_id>"
```

### `static/index.html`

Minimal placeholder — the real UI is at `/_apx/agent`.

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title><App Name></title>
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
       request_object_id="<space_id>",
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

Add one `ask_genie_*` function per space and register all as tools:

```python
async def ask_genie_sales(question: str, headers: Dependencies.Headers) -> str:
    """Query sales pipeline and opportunity data."""
    return await _query_genie(question, headers, SALES_SPACE_ID)

async def ask_genie_support(question: str, headers: Dependencies.Headers) -> str:
    """Query customer support tickets and case history."""
    return await _query_genie(question, headers, SUPPORT_SPACE_ID)

agent = Agent(tools=[ask_genie_sales, ask_genie_support], instructions=SYSTEM_PROMPT)
```

Extract the shared polling logic into a helper `_query_genie(question, headers, space_id)`.

## What NOT to Do

```python
# ❌ WRONG — blocks the event loop, SSE stream hangs
from apx_agent import genie_tool
ask_genie = genie_tool(GENIE_SPACE_ID)  # uses sync ws.api_client.do()

# ❌ WRONG — this is for scripts/notebooks, not Databricks Apps
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

# ❌ WRONG — app.mount("/", StaticFiles(...)) intercepts POST /responses
app.mount("/", StaticFiles(directory="static"), html=True)

# ✅ RIGHT — async httpx with OBO, mounted at /static not /
async def ask_genie(question: str, headers: Dependencies.Headers) -> str: ...
app.mount("/static", StaticFiles(directory="static"), name="static")
```

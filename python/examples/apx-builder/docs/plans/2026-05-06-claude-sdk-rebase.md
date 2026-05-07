# apx-builder: Rebase on claude-agent-sdk

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the apx-agent framework with claude-agent-sdk so tool execution is transparent, errors surface properly, and the build phase works reliably.

**Architecture:** The new `app.py` is a vanilla FastAPI server with a `/responses` endpoint that runs a `claude-agent-sdk` `query()` session in a background thread (required workaround for issue #462 — subprocess transport fails in uvicorn). The agent writes project files to `/tmp/` with the `Write` tool, uploads them with the AI Dev Kit `upload_folder` MCP tool, and creates/deploys the Databricks App with a thin custom MCP tool that wraps `databricks.sdk` directly. Session state is maintained by `claude-agent-sdk` via `session_id`.

**Tech Stack:** `claude-agent-sdk` (PyPI), `databricks-tools-core` + `databricks-mcp-server` (from `databricks-solutions/ai-dev-kit` GitHub repo), `fastapi`, `databricks-sdk`, Python 3.11+, TypeScript/React frontend.

---

## Context

The repo is the `feat-apx-builder` worktree of the `apx-agent` monorepo. All work happens under:

```
python/examples/apx-builder/
```

The existing `/responses` endpoint uses the `apx-agent` framework, which calls Python tool functions via internal ASGI transport. Exceptions are swallowed and stdout never reaches Databricks Apps logs. The new approach:

- `app.py` — FastAPI + `claude-agent-sdk`; replaces `apx-agent` entirely
- `mcp_loader.py` — NEW: loads AI Dev Kit MCP server in-process + registers two custom app tools
- `system_prompt.py` — rewrite: new `get_system_prompt(user_email)` function, updated tool names
- `client/src/useChat.ts` — add `session_id` tracking so multi-turn works with SDK sessions
- `pyproject.toml` / `requirements.txt` — swap `apx-agent` for new deps
- Delete `tools/scaffold_project.py`, `tools/deploy_agent.py`, `tools/poll_deployment.py`, `tools/discover_tables.py`
- Delete `tests/test_scaffold_project.py`, `tests/test_deploy_agent.py`, `tests/test_poll_deployment.py`, `tests/test_discover_tables.py`
- Rewrite `tests/test_system_prompt.py`
- Add `tests/test_mcp_loader.py`, `tests/test_app_endpoint.py`

---

## Key APIs and Patterns

### claude-agent-sdk `@tool` decorator (from AI Dev Kit builder app pattern)

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("my_tool", "Does something useful.", {"param_name": str, "other": int})
def my_tool_fn(args: dict) -> dict:
    result = do_work(args["param_name"])
    return {"content": [{"type": "text", "text": json.dumps(result)}]}

server = create_sdk_mcp_server(name="myserver", tools=[my_tool_fn])
# Tool name in allowed_tools: "mcp__myserver__my_tool"
```

### claude-agent-sdk `query()` in fresh event loop thread (issue #462 workaround)

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
import asyncio, threading, queue
from contextvars import copy_context

def _run_agent(user_message, options, q, ctx):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def go():
            text = ""
            sid = None
            async def prompt_gen():
                yield {"type": "user", "message": {"role": "user", "content": user_message}}
            async for msg in query(prompt=prompt_gen(), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text = block.text
                elif isinstance(msg, ResultMessage):
                    sid = msg.session_id
            q.put(("done", (text, sid)))
        try:
            loop.run_until_complete(go())
        except Exception as e:
            q.put(("error", e))
        finally:
            loop.close()
    ctx.run(run)
```

### databricks-tools-core auth context (per-request multi-user auth)

```python
from databricks_tools_core.auth import set_databricks_auth, clear_databricks_auth, get_workspace_client
from contextvars import copy_context

# In request handler:
set_databricks_auth(host, token)
try:
    ...  # any call to get_workspace_client() uses these creds
finally:
    clear_databricks_auth()

# In tool functions (run in threads via copy_context):
ctx = copy_context()
def run():
    ws = get_workspace_client()  # reads from context vars
    ...
ctx.run(run)
```

### AI Dev Kit MCP tool loading pattern (from Builder App's databricks_tools.py)

```python
from databricks_mcp_server.server import mcp
from databricks_mcp_server.tools import sql, file, genie, compute  # triggers registration

sdk_tools = []
for name, mcp_tool in mcp._tool_manager._tools.items():
    schema = _convert_schema(mcp_tool.parameters)  # {"property": python_type}
    sdk_tools.append(_make_wrapper(name, mcp_tool.description, schema, mcp_tool.fn))

server = create_sdk_mcp_server(name="databricks", tools=sdk_tools)
# Tools available as: mcp__databricks__execute_sql, mcp__databricks__upload_folder, etc.
```

---

## Task 1: Update dependencies

**Files:**
- Modify: `python/examples/apx-builder/pyproject.toml`
- Modify: `python/examples/apx-builder/requirements.txt`

- [ ] **Step 1: Replace pyproject.toml**

Write this complete file (replaces existing content):

```toml
[project]
name = "apx-builder"
version = "0.1.0"
description = "Natural language agent builder — describe your agent and I'll build and deploy it"
requires-python = ">=3.11"
dependencies = [
    "claude-agent-sdk>=0.1.19",
    "fastapi>=0.119.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "databricks-tools-core @ git+https://github.com/databricks-solutions/ai-dev-kit.git#subdirectory=databricks-tools-core",
    "databricks-mcp-server @ git+https://github.com/databricks-solutions/ai-dev-kit.git#subdirectory=databricks-mcp-server",
    "httpx>=0.27.0",
    "mcp>=1.0.0",
    "fastmcp>=0.1.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.27.0",
]

[tool.pytest.ini_options]
addopts = "-m 'not integration'"
markers = [
    "integration: live-app eval (requires APP_URL and DATABRICKS_TOKEN)",
]

[tool.apx.metadata]
app-name = "apx-builder"
app-entrypoint = "app:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["apx_builder"]
```

- [ ] **Step 2: Replace requirements.txt**

Write this complete file:

```
claude-agent-sdk>=0.1.19
fastapi>=0.119.0
uvicorn>=0.37.0
databricks-sdk>=0.74.0
databricks-tools-core @ git+https://github.com/databricks-solutions/ai-dev-kit.git#subdirectory=databricks-tools-core
databricks-mcp-server @ git+https://github.com/databricks-solutions/ai-dev-kit.git#subdirectory=databricks-mcp-server
httpx>=0.27.0
mcp>=1.0.0
fastmcp>=0.1.0
```

- [ ] **Step 3: Reinstall with uv**

```bash
cd python/examples/apx-builder
uv sync
```

Expected: uv installs `claude-agent-sdk`, `databricks-tools-core`, `databricks-mcp-server` (from git), and other deps. No errors.

- [ ] **Step 4: Verify imports work**

```bash
cd python/examples/apx-builder
uv run python -c "from claude_agent_sdk import ClaudeAgentOptions, query; print('claude-agent-sdk OK')"
uv run python -c "from databricks_tools_core.auth import get_workspace_client; print('tools-core OK')"
uv run python -c "from databricks_mcp_server.server import mcp; print('mcp-server OK')"
```

Expected: three "OK" lines.

- [ ] **Step 5: Commit**

```bash
git add python/examples/apx-builder/pyproject.toml python/examples/apx-builder/requirements.txt
git commit -m "chore(apx-builder): replace apx-agent with claude-agent-sdk + AI Dev Kit deps"
```

---

## Task 2: Create mcp_loader.py

**Files:**
- Create: `python/examples/apx-builder/mcp_loader.py`
- Create: `python/examples/apx-builder/tests/test_mcp_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_loader.py`:

```python
"""Tests for mcp_loader.py — verifies server loading and custom tool structure."""
import json
from unittest.mock import MagicMock, patch


def test_get_mcp_servers_returns_both_servers():
    """get_mcp_servers() returns a dict with databricks and apx keys."""
    fake_mcp = MagicMock()
    fake_mcp._tool_manager._tools = {}

    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server) as mock_create, \
         patch.dict("sys.modules", {
             "databricks_mcp_server": MagicMock(),
             "databricks_mcp_server.server": MagicMock(mcp=fake_mcp),
             "databricks_mcp_server.tools": MagicMock(),
             "databricks_mcp_server.tools.sql": MagicMock(),
             "databricks_mcp_server.tools.file": MagicMock(),
             "databricks_mcp_server.tools.genie": MagicMock(),
             "databricks_mcp_server.tools.compute": MagicMock(),
         }):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)

        # Reset singletons so loading happens in this test
        mcp_loader._databricks_server = None
        mcp_loader._databricks_tool_names = None
        mcp_loader._apx_server = None

        servers, tool_names = mcp_loader.get_mcp_servers()

    assert "databricks" in servers
    assert "apx" in servers


def test_apx_tool_names_are_present():
    """The apx MCP server tool names are mcp__apx__ prefixed."""
    fake_mcp = MagicMock()
    fake_mcp._tool_manager._tools = {}
    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server), \
         patch.dict("sys.modules", {
             "databricks_mcp_server": MagicMock(),
             "databricks_mcp_server.server": MagicMock(mcp=fake_mcp),
             "databricks_mcp_server.tools": MagicMock(),
             "databricks_mcp_server.tools.sql": MagicMock(),
             "databricks_mcp_server.tools.file": MagicMock(),
             "databricks_mcp_server.tools.genie": MagicMock(),
             "databricks_mcp_server.tools.compute": MagicMock(),
         }):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)
        mcp_loader._databricks_server = None
        mcp_loader._databricks_tool_names = None
        mcp_loader._apx_server = None

        _, tool_names = mcp_loader.get_mcp_servers()

    assert "mcp__apx__create_and_deploy_app" in tool_names
    assert "mcp__apx__get_app_status" in tool_names


def test_convert_schema_maps_string_to_str():
    from mcp_loader import _convert_schema

    schema = {"properties": {"table_name": {"type": "string"}, "limit": {"type": "integer"}}}
    result = _convert_schema(schema)

    assert result["table_name"] is str
    assert result["limit"] is int


def test_convert_schema_handles_anyof_optional():
    from mcp_loader import _convert_schema

    schema = {"properties": {"name": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    result = _convert_schema(schema)

    assert result["name"] is str
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_mcp_loader.py -v
```

Expected: ImportError or ModuleNotFoundError (mcp_loader doesn't exist yet).

- [ ] **Step 3: Create mcp_loader.py**

Create `mcp_loader.py`:

```python
"""Load in-process MCP servers for the claude-agent-sdk agent."""
import json
import logging
from contextvars import copy_context

from claude_agent_sdk import tool, create_sdk_mcp_server
from databricks.sdk.errors import NotFound
from databricks_tools_core.auth import get_workspace_client

logger = logging.getLogger(__name__)

# Singletons — loaded once at first request
_databricks_server = None
_databricks_tool_names = None
_apx_server = None
_apx_tool_names = ["mcp__apx__create_and_deploy_app", "mcp__apx__get_app_status"]


def _convert_schema(json_schema: dict) -> dict:
    """Convert FastMCP JSON schema to claude-agent-sdk simple format: {param: python_type}."""
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    result = {}
    for param, spec in json_schema.get("properties", {}).items():
        if "anyOf" in spec:
            for opt in spec["anyOf"]:
                if opt.get("type") != "null":
                    result[param] = type_map.get(opt.get("type"), str)
                    break
        else:
            result[param] = type_map.get(spec.get("type"), str)
    return result


def _make_wrapper(name: str, description: str, schema: dict, fn):
    """Wrap a FastMCP sync function as a claude-agent-sdk tool.

    Propagates Databricks auth context vars to the worker thread via copy_context().
    Handles JSON-string coercion for list/dict params that Claude sometimes sends as strings.
    """
    @tool(name, description, schema)
    def wrapper(args: dict) -> dict:
        ctx = copy_context()

        def run():
            parsed = {}
            for k, v in args.items():
                if isinstance(v, str) and v.strip().startswith(("[", "{")):
                    try:
                        parsed[k] = json.loads(v)
                    except json.JSONDecodeError:
                        parsed[k] = v
                else:
                    parsed[k] = v
            return fn(**parsed)

        result = ctx.run(run)
        result_str = json.dumps(result, default=str) if isinstance(result, (dict, list)) else str(result)
        return {"content": [{"type": "text", "text": result_str}]}

    return wrapper


# Custom app management tools
@tool(
    "create_and_deploy_app",
    (
        "Create a Databricks App if it doesn't exist, then trigger deployment "
        "from a workspace path. Returns the app name and URL."
    ),
    {"app_name": str, "source_code_path": str},
)
def _create_and_deploy_app(args: dict) -> dict:
    ctx = copy_context()

    def run():
        ws = get_workspace_client()
        app_name = args["app_name"]
        source_code_path = args["source_code_path"]
        try:
            ws.apps.get(app_name)
        except NotFound:
            ws.apps.create(name=app_name, description="Agent built by apx-builder")
        ws.apps.deploy(app_name=app_name, source_code_path=source_code_path)
        app = ws.apps.get(app_name)
        return {
            "name": app_name,
            "url": app.url or "",
            "status": "deployment triggered",
        }

    result = ctx.run(run)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool(
    "get_app_status",
    "Get the deployment status and URL of a Databricks App.",
    {"app_name": str},
)
def _get_app_status(args: dict) -> dict:
    ctx = copy_context()

    def run():
        ws = get_workspace_client()
        app = ws.apps.get(args["app_name"])
        active = app.active_deployment
        return {
            "name": args["app_name"],
            "url": app.url or "",
            "app_state": app.app_status.state.value if app.app_status else "UNKNOWN",
            "deploy_state": active.status.state.value if active and active.status else "UNKNOWN",
        }

    result = ctx.run(run)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


def get_mcp_servers() -> tuple[dict, list[str]]:
    """Return (servers_dict, all_tool_names). Singletons loaded on first call.

    servers_dict is passed directly to ClaudeAgentOptions.mcp_servers.
    tool_names are the allowed_tools names in mcp__<server>__<tool> format.
    """
    global _databricks_server, _databricks_tool_names, _apx_server

    if _databricks_server is None:
        from databricks_mcp_server.server import mcp
        from databricks_mcp_server.tools import sql, file, genie, compute  # noqa: F401

        sdk_tools = []
        names = []
        for name, mcp_tool in mcp._tool_manager._tools.items():
            schema = _convert_schema(mcp_tool.parameters)
            sdk_tools.append(_make_wrapper(name, mcp_tool.description, schema, mcp_tool.fn))
            names.append(f"mcp__databricks__{name}")

        _databricks_server = create_sdk_mcp_server(name="databricks", tools=sdk_tools)
        _databricks_tool_names = names
        logger.info("Loaded %d databricks MCP tools", len(names))

    if _apx_server is None:
        _apx_server = create_sdk_mcp_server(
            name="apx",
            tools=[_create_and_deploy_app, _get_app_status],
        )

    return (
        {"databricks": _databricks_server, "apx": _apx_server},
        _databricks_tool_names + _apx_tool_names,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_mcp_loader.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/examples/apx-builder/mcp_loader.py python/examples/apx-builder/tests/test_mcp_loader.py
git commit -m "feat(apx-builder): add mcp_loader — in-process MCP servers for claude-agent-sdk"
```

---

## Task 3: Rewrite app.py

**Files:**
- Modify: `python/examples/apx-builder/app.py`
- Create: `python/examples/apx-builder/tests/test_app_endpoint.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_endpoint.py`:

```python
"""Tests for the /responses FastAPI endpoint."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_responses_returns_correct_output_format():
    """/responses returns output with expected shape and session_id."""
    from app import app

    mock_text = "What should your agent do?"
    mock_session_id = "sess_abc123"

    with patch("app.get_mcp_servers") as mock_servers, \
         patch("app.asyncio.to_thread") as mock_to_thread, \
         patch("app.set_databricks_auth"), \
         patch("app.clear_databricks_auth"), \
         patch("app._collect_result") as mock_collect, \
         patch("app.asyncio.get_event_loop") as mock_loop:

        mock_servers.return_value = ({}, ["mcp__apx__create_and_deploy_app"])

        # Simulate the user email lookup
        mock_to_thread.return_value = "user@example.com"

        # Simulate the queue result
        mock_q = MagicMock()
        mock_q.get.return_value = ("done", (mock_text, mock_session_id))

        import queue as queue_module
        mock_loop_instance = MagicMock()
        mock_loop.return_value = mock_loop_instance

        async def fake_run_in_executor(_, fn):
            return fn()

        mock_loop_instance.run_in_executor = fake_run_in_executor

        with patch("app.queue.Queue", return_value=mock_q), \
             patch("app.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/responses",
                    json={"input": [{"role": "user", "content": "I want to build an agent"}]},
                    headers={"Authorization": "Bearer fake-token"},
                )

    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["text"] == mock_text
    assert data["session_id"] == mock_session_id


@pytest.mark.asyncio
async def test_responses_returns_400_for_empty_input():
    """/responses returns 400 when input is empty."""
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/responses",
            json={"input": []},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_responses_passes_session_id_for_resumption():
    """/responses passes session_id to ClaudeAgentOptions.resume when provided."""
    from app import app

    options_captured = {}

    with patch("app.get_mcp_servers", return_value=({}, [])), \
         patch("app.asyncio.to_thread", return_value="user@example.com"), \
         patch("app.set_databricks_auth"), \
         patch("app.clear_databricks_auth"), \
         patch("app.get_system_prompt", return_value="system prompt"), \
         patch("app.ClaudeAgentOptions") as mock_opts_cls, \
         patch("app.asyncio.get_event_loop") as mock_loop:

        mock_opts_cls.side_effect = lambda **kw: (options_captured.update(kw), MagicMock())[1]

        mock_q = MagicMock()
        mock_q.get.return_value = ("done", ("hello", "sess_new"))

        mock_loop_instance = MagicMock()
        mock_loop.return_value = mock_loop_instance

        async def fake_run_in_executor(_, fn):
            return fn()

        mock_loop_instance.run_in_executor = fake_run_in_executor

        with patch("app.queue.Queue", return_value=mock_q), \
             patch("app.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post(
                    "/responses",
                    json={
                        "input": [{"role": "user", "content": "Hello"}],
                        "session_id": "sess_existing",
                    },
                    headers={"Authorization": "Bearer fake-token"},
                )

    assert options_captured.get("resume") == "sess_existing"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_app_endpoint.py -v
```

Expected: ImportError or AttributeError (app.py still uses apx-agent).

- [ ] **Step 3: Rewrite app.py**

Replace the entire content of `app.py`:

```python
import asyncio
import logging
import os
import queue
import threading
from contextvars import copy_context
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from databricks.sdk import WorkspaceClient
from databricks_tools_core.auth import set_databricks_auth, clear_databricks_auth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from mcp_loader import get_mcp_servers
from system_prompt import get_system_prompt

logger = logging.getLogger(__name__)
app = FastAPI()


def _collect_result(user_message: str, options: ClaudeAgentOptions, q: queue.Queue, ctx) -> None:
    """Run query() in a fresh event loop thread and put (text, session_id) on the queue.

    Must run in a thread because claude-agent-sdk's subprocess transport
    is incompatible with uvicorn's running event loop (issue #462).
    Uses copy_context() to propagate Databricks auth context vars into the thread.
    """
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def go():
            collected_text = ""
            new_session_id = None

            async def prompt_gen():
                yield {"type": "user", "message": {"role": "user", "content": user_message}}

            async for msg in query(prompt=prompt_gen(), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            collected_text = block.text
                elif isinstance(msg, ResultMessage):
                    new_session_id = msg.session_id

            q.put(("done", (collected_text, new_session_id)))

        try:
            loop.run_until_complete(go())
        except Exception as exc:
            q.put(("error", exc))
        finally:
            loop.close()

    ctx.run(run)


def _get_from_queue(q: queue.Queue) -> tuple:
    try:
        return q.get(timeout=300)
    except queue.Empty:
        return ("timeout", None)


@app.post("/responses")
async def responses(request: Request):
    body = await request.json()
    messages = body.get("input", [])
    session_id = body.get("session_id")

    if not messages:
        raise HTTPException(status_code=400, detail="input must not be empty")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")

    if not host:
        raise HTTPException(status_code=500, detail="DATABRICKS_HOST not configured")

    ws = WorkspaceClient(host=host, token=token)
    user_email = await asyncio.to_thread(lambda: ws.current_user.me().user_name)

    set_databricks_auth(host, token)
    try:
        servers, tool_names = get_mcp_servers()
        user_message = messages[-1]["content"]

        options = ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=["Write"] + tool_names,
            permission_mode="bypassPermissions",
            resume=session_id,
            mcp_servers=servers,
            system_prompt=get_system_prompt(user_email),
            env={
                "ANTHROPIC_API_KEY": token,
                "ANTHROPIC_BASE_URL": f"{host}/serving-endpoints/anthropic",
                "ANTHROPIC_MODEL": "databricks-claude-sonnet-4-6",
                "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-disable-beta-headers: true",
            },
        )

        q: queue.Queue = queue.Queue()
        ctx = copy_context()
        thread = threading.Thread(
            target=_collect_result,
            args=(user_message, options, q, ctx),
            daemon=True,
        )
        thread.start()

        loop = asyncio.get_event_loop()
        msg_type, payload = await loop.run_in_executor(None, _get_from_queue, q)

        if msg_type == "timeout":
            raise HTTPException(status_code=504, detail="Agent timed out after 5 minutes")
        if msg_type == "error":
            logger.error("Agent error: %s", payload)
            raise HTTPException(status_code=500, detail=str(payload))

        text, new_session_id = payload
        return {
            "output": [{"type": "message", "content": [{"text": text or ""}]}],
            "session_id": new_session_id or session_id,
        }
    finally:
        clear_databricks_auth()


# Serve the React frontend
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    _here.parent / "client" / "dist",
]
_CLIENT_DIST = next((c for c in _candidates if c.exists()), None)

if _CLIENT_DIST is not None:
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_app_endpoint.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/examples/apx-builder/app.py python/examples/apx-builder/tests/test_app_endpoint.py
git commit -m "feat(apx-builder): rewrite app.py using claude-agent-sdk and FastAPI"
```

---

## Task 4: Rewrite system_prompt.py

**Files:**
- Modify: `python/examples/apx-builder/system_prompt.py`
- Modify: `python/examples/apx-builder/tests/test_system_prompt.py`

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_system_prompt.py` entirely:

```python
"""Tests for the updated system_prompt.py."""
from system_prompt import get_system_prompt


def test_get_system_prompt_returns_string():
    prompt = get_system_prompt("user@example.com")
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_user_email_is_embedded():
    prompt = get_system_prompt("alice@databricks.com")
    assert "alice@databricks.com" in prompt


def test_build_phase_uses_write_tool():
    prompt = get_system_prompt("user@example.com")
    assert "Write" in prompt, "Build phase must reference the Write tool for file creation"


def test_build_phase_uses_upload_folder():
    prompt = get_system_prompt("user@example.com")
    assert "upload_folder" in prompt or "mcp__databricks__upload_folder" in prompt


def test_build_phase_uses_create_and_deploy_app():
    prompt = get_system_prompt("user@example.com")
    assert "create_and_deploy_app" in prompt or "mcp__apx__create_and_deploy_app" in prompt


def test_discovery_references_execute_sql():
    prompt = get_system_prompt("user@example.com")
    assert "execute_sql" in prompt or "mcp__databricks__execute_sql" in prompt


def test_discovery_references_get_genie():
    prompt = get_system_prompt("user@example.com")
    assert "get_genie" in prompt or "mcp__databricks__get_genie" in prompt


def test_no_backtick_rule_present():
    prompt = get_system_prompt("user@example.com")
    lower = prompt.lower()
    assert "backtick" in lower or "code formatting" in lower


def test_phase_3_plain_english_tables():
    prompt = get_system_prompt("user@example.com")
    phase3_start = prompt.index("## Phase 3")
    phase3_section = prompt[phase3_start:].lower()
    assert "plain english" in phase3_section or "natural" in phase3_section


def test_no_jargon_rule():
    lower = get_system_prompt("user@example.com").lower()
    assert "jargon" in lower or "plain english" in lower


def test_old_tool_names_not_present():
    """scaffold_project, deploy_agent, poll_deployment must not appear in the prompt."""
    prompt = get_system_prompt("user@example.com")
    assert "scaffold_project" not in prompt
    assert "deploy_agent" not in prompt
    assert "poll_deployment" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_system_prompt.py -v
```

Expected: Several failures (old tests check for scaffold_project/deploy_agent, new tests check for missing new tool names).

- [ ] **Step 3: Rewrite system_prompt.py**

Replace the entire content of `system_prompt.py`:

```python
def get_system_prompt(user_email: str) -> str:
    """Return the apx-builder system prompt with the user's email embedded for workspace paths."""
    return f"""
# apx-builder Agent — System Instructions

You are the apx-builder assistant. Your job is to help a field rep (who may have no coding experience) go from
"I want an agent" to a live deployed URL — entirely through conversation, in under 15 minutes.

Keep every message short, friendly, and jargon-free. You are a helpful colleague, not a technical wizard.
Never mention code, Python, pyproject.toml, workspace paths, or any internal implementation details.
Never use backtick or code formatting — not even for table names or app names. Plain text only.

---

## Phase 1: Discovery

Ask **one question at a time**. Do not ask multiple questions in a single message.
Use plain English — no technical terms, no jargon.

### Step 1 — Use case

Start with:
> "What should your agent do? Describe it in plain English."

Listen for the use case description. Then continue to the next step.

### Step 2 — Data sources

Ask:
> "Which tables or data sources should it use?"

While the rep is answering (or after), use execute_sql (mcp__databricks__execute_sql) to search for
relevant tables. Use a query like:

```sql
SELECT table_catalog, table_schema, table_name, comment
FROM system.information_schema.tables
WHERE lower(table_name) LIKE '%<keyword>%'
   OR lower(coalesce(comment, '')) LIKE '%<keyword>%'
LIMIT 20
```

Present the results naturally:
> "I found these tables in your catalog — do any of these look right? [list names]"

Let the rep pick from your suggestions or name their own. Confirm the final list before moving on.

### Step 3 — Genie spaces (conditional)

Only ask this if the rep mentions Genie, AI/BI dashboards, or conversational analytics.
If relevant, call get_genie (mcp__databricks__get_genie) with no arguments to list all spaces.
Present options by name:
> "I found these Genie spaces — should the agent connect to any of them? [list names]"

If not relevant, skip this step entirely.

### Step 4 — Lineage

Ask:
> "Should your agent be able to answer questions about data lineage — like which pipelines feed a table,
> or which columns come from where?"

A yes/no answer is fine.

### Step 5 — Name

Ask:
> "What should we call this agent?"

Suggest a short slug derived from the use case (lowercase letters and hyphens, no spaces).
For example, if the use case is "answer sales questions", suggest sales-assistant.
The app will be deployed as mcp-{{app_name}}.

Confirm the name before moving on.

---

## Phase 2: Build

Once all five discovery questions are answered, announce:
> "Got everything I need — building your agent now. This takes about 2 minutes."

Then execute the following steps **in this exact order**.

### Step 1 — Write project files

Write these four files to /tmp/mcp-{{app_name}}/ using the Write tool.
Replace {{app_name}} with the actual slug, {{use_case}} with the use case, and fill in
the tools list based on the gathered information.

**File: /tmp/mcp-{{app_name}}/app.py**

Generate app.py based on the tables, genie spaces, and lineage flag:

```python
from apx_agent import Agent, create_app[, sql_tool][, genie_tool][, lineage_tool]

agent = Agent(
    tools=[
        sql_tool("catalog.schema.table_name"),  # one line per table
        genie_tool("the-space-id"),  # Space Display Name  — one per genie space
        lineage_tool(),  # only if include_lineage is True
    ],
    instructions="You are a data assistant for: {{use_case}}. Answer questions using the available tools.",
)
app = create_app(agent)
```

Rules for generating app.py:
- Add only the tools the user asked for. Import only what you use.
- If no tools at all: write `from apx_agent import Agent, create_app` (no extras).
- sql_tool takes the full three-part table identifier (e.g., sql_tool("main.sales.orders")).
- genie_tool takes the space ID (not the name). Add a comment with the space name.
- lineage_tool() goes last and only if the user said yes to lineage.

**File: /tmp/mcp-{{app_name}}/pyproject.toml**

```toml
[project]
name = "mcp-{{app_name}}"
requires-python = ">=3.11"
dependencies = [
    "apx-agent @ git+https://github.com/stuagano/apx-agent.git#subdirectory=python",
]

[tool.apx.agent]
name = "mcp-{{app_name}}"
description = "{{use_case}}"
model = "databricks-claude-sonnet-4-6"
url = ""

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**File: /tmp/mcp-{{app_name}}/requirements.txt**

```
apx-agent @ git+https://github.com/stuagano/apx-agent.git#subdirectory=python
fastapi>=0.119.0
uvicorn>=0.37.0
databricks-sdk>=0.74.0
httpx>=0.27.0
```

**File: /tmp/mcp-{{app_name}}/app.yml**

```yaml
command:
  - uvicorn
  - app:app
  - --workers
  - "1"
```

### Step 2 — Upload to workspace

Call mcp__databricks__upload_folder with:
- local_folder: /tmp/mcp-{{app_name}}
- workspace_folder: /Workspace/Users/{user_email}/apx-builder/mcp-{{app_name}}

### Step 3 — Create and deploy

Call mcp__apx__create_and_deploy_app with:
- app_name: mcp-{{app_name}}
- source_code_path: /Workspace/Users/{user_email}/apx-builder/mcp-{{app_name}}

### Step 4 — Share the URL

The tool returns a "url" field. Share it with the user in plain English.

**CRITICAL: NEVER share any URL before create_and_deploy_app returns it.**

---

## Phase 3: Finish

When filling in {{tables}}, list the table names in plain English — for example,
"the sales_data and customer_accounts tables" — not as a Python list or comma-separated identifiers.

### If create_and_deploy_app succeeded:

> "Your agent is deploying at {{url}}. It should be ready in about a minute. It can answer questions about {{tables}}.
> Try asking it: [generate a concrete example question based on the use case and tables]."

### If create_and_deploy_app returned a deployment_error:

> "Something went wrong deploying the agent — [paraphrase the error in plain English]. Want to try again?"

---

## Error Handling

- If upload_folder fails: report the error in plain English and ask if they'd like to try again.
- If create_and_deploy_app fails: same — plain English, offer to retry.
- Never surface stack traces, file paths, or internal error details to the rep.

---

## Tone and Style

- Short messages. Conversational. One thing at a time.
- If the rep seems confused, rephrase without introducing technical terms.
- Never ask more than one question per message.
- The flow should feel like chatting with a helpful colleague who happens to know how to build agents.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd python/examples/apx-builder
uv run pytest tests/test_system_prompt.py -v
```

Expected: 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/examples/apx-builder/system_prompt.py python/examples/apx-builder/tests/test_system_prompt.py
git commit -m "feat(apx-builder): rewrite system_prompt to use claude-agent-sdk tool names"
```

---

## Task 5: Update useChat.ts for session_id

**Files:**
- Modify: `python/examples/apx-builder/client/src/useChat.ts`

- [ ] **Step 1: Replace useChat.ts**

Replace the entire content of `client/src/useChat.ts`:

```typescript
import { useState, useCallback, useRef } from 'react'
import type { Message } from './types'

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const messagesRef = useRef<Message[]>([])
  const sessionIdRef = useRef<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { id: crypto.randomUUID(), role: 'user', content: text }
    const next = [...messagesRef.current, userMsg]
    setMessages(next)
    messagesRef.current = next
    setIsLoading(true)
    try {
      const body: Record<string, unknown> = {
        input: next.map(m => ({ role: m.role, content: m.content })),
      }
      if (sessionIdRef.current) {
        body.session_id = sessionIdRef.current
      }
      const resp = await fetch('/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      // Store session_id for subsequent turns
      if (data?.session_id) {
        sessionIdRef.current = data.session_id
      }
      const outputText: string =
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        data?.output?.find((o: any) => o.type === 'message')
          ?.content?.[0]?.text ?? 'No response.'
      const assistantMsg: Message = { id: crypto.randomUUID(), role: 'assistant', content: outputText }
      setMessages(prev => [...prev, assistantMsg])
      messagesRef.current = [...messagesRef.current, assistantMsg]
    } catch (err) {
      console.error('[apx-builder] sendMessage failed:', err)
      const errMsg: Message = { id: crypto.randomUUID(), role: 'assistant', content: 'Something went wrong. Please try again.' }
      setMessages(prev => [...prev, errMsg])
      messagesRef.current = [...messagesRef.current, errMsg]
    } finally {
      setIsLoading(false)
    }
  }, [])

  const reset = useCallback(() => {
    setMessages([])
    messagesRef.current = []
    sessionIdRef.current = null
  }, [])

  return { messages, isLoading, sendMessage, reset }
}
```

- [ ] **Step 2: Rebuild the frontend**

```bash
cd python/examples/apx-builder/client
npm run build
```

Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 3: Commit**

```bash
cd python/examples/apx-builder
git add client/src/useChat.ts client/dist/
git commit -m "feat(apx-builder): add session_id tracking to useChat for multi-turn SDK sessions"
```

---

## Task 6: Delete old tool files and tests

**Files:**
- Delete: `tools/scaffold_project.py`, `tools/deploy_agent.py`, `tools/poll_deployment.py`, `tools/discover_tables.py`
- Delete: `tests/test_scaffold_project.py`, `tests/test_deploy_agent.py`, `tests/test_poll_deployment.py`, `tests/test_discover_tables.py`

- [ ] **Step 1: Delete the old tool files**

```bash
cd python/examples/apx-builder
rm tools/scaffold_project.py tools/deploy_agent.py tools/poll_deployment.py tools/discover_tables.py
```

- [ ] **Step 2: Delete the corresponding tests**

```bash
rm tests/test_scaffold_project.py tests/test_deploy_agent.py tests/test_poll_deployment.py tests/test_discover_tables.py
```

- [ ] **Step 3: Run full test suite to verify nothing is broken**

```bash
cd python/examples/apx-builder
uv run pytest tests/ -v
```

Expected: All remaining tests pass (test_mcp_loader.py + test_app_endpoint.py + test_system_prompt.py). No import errors.

- [ ] **Step 4: Commit**

```bash
cd python/examples/apx-builder
git add -A
git commit -m "chore(apx-builder): remove old apx-agent tool files and tests"
```

---

## Task 7: Integration smoke test

**Files:** No code changes — this task validates the deployed app.

- [ ] **Step 1: Check the git log for the branch to confirm clean history**

```bash
git log --oneline python/examples/apx-builder/ | head -10
```

Expected: See the 5 commits from tasks 1–6 in order.

- [ ] **Step 2: Run the full unit test suite one final time**

```bash
cd python/examples/apx-builder
uv run pytest tests/ -v
```

Expected: All unit tests pass. No integration tests run (guarded by `not integration` marker).

- [ ] **Step 3: Deploy to fe-stable**

```bash
cd python/examples/apx-builder
# Upload to workspace
databricks workspace import-dir . /Users/stuart.gano@databricks.com/apx-builder --profile fe-stable --overwrite

# Deploy
databricks apps deploy apx-builder \
  --source-code-path /Workspace/Users/stuart.gano@databricks.com/apx-builder \
  --profile fe-stable

# Poll until RUNNING
databricks apps get apx-builder --profile fe-stable -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
active = d.get('active_deployment', {})
print('Deploy state:', active.get('status', {}).get('state', 'unknown'))
print('App state:', d.get('app_status', {}).get('state', 'unknown'))
"
```

Expected: Both states show SUCCEEDED / RUNNING within ~60 seconds.

- [ ] **Step 4: Run the Tier 1 conversation eval against the live app**

```bash
cd python/examples/apx-builder
APP_URL=https://apx-builder-<workspace>.databricksapps.com \
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com \
uv run pytest evals/test_conversation.py -v -m integration
```

Expected: All 5 turns pass the LLM judge.

- [ ] **Step 5: Manual build test**

Open the app URL, walk through all 5 discovery questions, then provide an app name.
Verify:
- The agent says "Got everything I need — building your agent now."
- The Write tool calls appear in the agent's response (or are visible in logs)
- `upload_folder` and `create_and_deploy_app` execute without error
- The agent shares a working URL

---

## Self-Review

**Spec coverage:**
- ✅ Replace apx-agent with claude-agent-sdk → Tasks 1, 3
- ✅ In-process MCP server loading → Task 2
- ✅ Discovery tools (execute_sql, get_genie) → Task 4 (system prompt guides Claude to use these)
- ✅ Build phase (Write + upload_folder + create_and_deploy_app) → Task 4
- ✅ session_id multi-turn → Tasks 3, 5
- ✅ Delete old tool files → Task 6
- ✅ Update tests → Tasks 2, 3, 4, 6

**Placeholder scan:** No TBDs or incomplete steps found.

**Type consistency:**
- `get_system_prompt(user_email: str) -> str` defined in Task 4, called in Task 3 (app.py line `system_prompt=get_system_prompt(user_email)`) ✅
- `get_mcp_servers() -> tuple[dict, list[str]]` defined in Task 2, called in Task 3 ✅
- `_collect_result(user_message, options, q, ctx)` defined and called within Task 3 ✅
- `session_id` flows from request body → `ClaudeAgentOptions.resume` → `ResultMessage.session_id` → response body → `sessionIdRef` ✅

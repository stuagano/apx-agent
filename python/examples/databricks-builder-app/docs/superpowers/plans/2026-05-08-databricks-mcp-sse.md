# Databricks MCP SSE Transport Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace in-process `databricks-mcp-server` loading in `databricks-builder-app` with an SSE MCP connection, so the MCP server runs as a separate process and is independently hostable.

**Architecture:** The `databricks-mcp-server` is started separately (via `start_local.sh` locally, or as its own Databricks App when hosted) and exposes tools over SSE. The builder app connects via `McpSSEServerConfig` — a plain config dict the Claude Agent SDK uses to route tool calls to the SSE endpoint. Auth is handled by the MCP server using its own credentials (env vars / `~/.databrickscfg` locally; OAuth M2M on Databricks Apps).

**Tech Stack:** Python 3.11, FastMCP 2.12+, Claude Agent SDK (`McpSSEServerConfig`), uv, bash

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `databricks-mcp-server/databricks_mcp_server/__init__.py` | Export `TOOL_NAMES: list[str]` — plain list of 71 tool names, no SDK imports |
| Create | `databricks-mcp-server/tests/test_tool_names.py` | Assert `TOOL_NAMES` matches what `mcp` registers |
| Replace | `databricks-builder-app/server/services/databricks_tools.py` | `get_databricks_server_config()` → SSE config + prefixed tool names |
| Create | `databricks-builder-app/tests/test_databricks_tools.py` | Unit tests for `get_databricks_server_config()` |
| Modify | `databricks-builder-app/server/services/apx_tools.py` | Add `check_operation_status` + `list_operations` tools (moved from old `databricks_tools.py`) |
| Modify | `databricks-builder-app/server/services/agent.py` | Use `get_databricks_server_config()`; remove in-process globals and singleton |
| Modify | `databricks-builder-app/.env.local` | Add `DATABRICKS_MCP_SERVER_URL=http://localhost:8080/sse` |
| Create | `databricks-builder-app/scripts/start_local.sh` | Start MCP server (SSE :8080) + builder app (:8000) together |

---

### Task 1: Export TOOL_NAMES from databricks-mcp-server

**Files:**
- Modify: `databricks-mcp-server/databricks_mcp_server/__init__.py`
- Create: `databricks-mcp-server/tests/__init__.py`
- Create: `databricks-mcp-server/tests/test_tool_names.py`

- [ ] **Step 1: Write the failing test**

Create `databricks-mcp-server/tests/__init__.py` (empty):

```python
```

Create `databricks-mcp-server/tests/test_tool_names.py`:

```python
"""Verify TOOL_NAMES matches tools actually registered in the MCP server."""


def test_tool_names_matches_registered_tools():
    from databricks_mcp_server import TOOL_NAMES
    from databricks_mcp_server.server import mcp

    registered = set(mcp._tool_manager._tools.keys())
    declared = set(TOOL_NAMES)
    missing = registered - declared
    extra = declared - registered
    assert not missing, f"Tools registered but missing from TOOL_NAMES: {missing}"
    assert not extra, f"Names in TOOL_NAMES but not registered: {extra}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /path/to/ai-dev-kit/databricks-mcp-server
pip install -e . -q
pytest tests/test_tool_names.py -v
```

Expected: `FAILED` — `ImportError: cannot import name 'TOOL_NAMES'`

- [ ] **Step 3: Add TOOL_NAMES to `__init__.py`**

Replace `databricks-mcp-server/databricks_mcp_server/__init__.py` with:

```python
"""Databricks MCP Server - FastMCP-based tools for Databricks operations."""

__version__ = "0.1.0"

# All tool names registered by this server.
# Import this (not server.py) when you only need names — avoids triggering
# tool registrations and Databricks SDK imports.
TOOL_NAMES: list[str] = [
    "ask_genie",
    "ask_genie_followup",
    "cancel_run",
    "create_job",
    "create_or_update_dashboard",
    "create_or_update_genie",
    "create_or_update_ka",
    "create_or_update_mas",
    "create_or_update_pipeline",
    "create_pipeline",
    "create_volume_directory",
    "delete_genie",
    "delete_job",
    "delete_ka",
    "delete_mas",
    "delete_pipeline",
    "delete_volume_directory",
    "delete_volume_file",
    "download_from_volume",
    "execute_databricks_command",
    "execute_sql",
    "execute_sql_multi",
    "find_job_by_name",
    "find_ka_by_name",
    "find_mas_by_name",
    "find_pipeline_by_name",
    "get_best_cluster",
    "get_best_warehouse",
    "get_dashboard",
    "get_genie",
    "get_job",
    "get_ka",
    "get_mas",
    "get_pipeline",
    "get_pipeline_events",
    "get_run",
    "get_run_output",
    "get_serving_endpoint_status",
    "get_table_details",
    "get_update",
    "list_clusters",
    "list_dashboards",
    "list_genie",
    "list_jobs",
    "list_runs",
    "list_serving_endpoints",
    "list_volume_files",
    "list_warehouses",
    "manage_uc_connections",
    "manage_uc_grants",
    "manage_uc_monitors",
    "manage_uc_objects",
    "manage_uc_security_policies",
    "manage_uc_sharing",
    "manage_uc_storage",
    "manage_uc_tags",
    "publish_dashboard",
    "query_serving_endpoint",
    "run_job_now",
    "run_python_file_on_databricks",
    "start_update",
    "stop_pipeline",
    "trash_dashboard",
    "unpublish_dashboard",
    "update_job",
    "update_pipeline",
    "upload_file",
    "upload_folder",
    "upload_to_volume",
    "wait_for_run",
]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_tool_names.py -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
cd /path/to/ai-dev-kit
git add databricks-mcp-server/databricks_mcp_server/__init__.py \
        databricks-mcp-server/tests/__init__.py \
        databricks-mcp-server/tests/test_tool_names.py
git commit -m "feat(mcp-server): export TOOL_NAMES list from __init__"
```

---

### Task 2: Replace databricks_tools.py in builder app

**Files:**
- Replace: `databricks-builder-app/server/services/databricks_tools.py`
- Create: `databricks-builder-app/tests/__init__.py`
- Create: `databricks-builder-app/tests/test_databricks_tools.py`

- [ ] **Step 1: Add pytest to builder app dev dependencies**

In `databricks-builder-app/pyproject.toml`, add to `[dependency-groups] dev`:

```toml
[dependency-groups]
dev = [
  "click>=8.1.8",
  "pytest>=8.0.0",
  "ruff>=0.9.6",
  "watchdog[watchmede]>=6.0.0",
]
```

Install it:

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
uv sync
```

- [ ] **Step 2: Write the failing tests**

Create `databricks-builder-app/tests/__init__.py` (empty):

```python
```

Create `databricks-builder-app/tests/test_databricks_tools.py`:

```python
"""Tests for SSE MCP server config function."""
import pytest


def test_returns_sse_config_type(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    from server.services.databricks_tools import get_databricks_server_config
    config, _ = get_databricks_server_config()
    assert config["type"] == "sse"


def test_returns_correct_url(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    from server.services.databricks_tools import get_databricks_server_config
    config, _ = get_databricks_server_config()
    assert config["url"] == "http://localhost:8080/sse"


def test_tool_names_prefixed(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    from server.services.databricks_tools import get_databricks_server_config
    _, tool_names = get_databricks_server_config()
    assert all(n.startswith("mcp__databricks__") for n in tool_names)
    assert "mcp__databricks__execute_sql" in tool_names
    assert "mcp__databricks__list_warehouses" in tool_names


def test_tool_name_count(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    from server.services.databricks_tools import get_databricks_server_config
    _, tool_names = get_databricks_server_config()
    assert len(tool_names) == 71


def test_raises_without_url(monkeypatch):
    monkeypatch.delenv("DATABRICKS_MCP_SERVER_URL", raising=False)
    from server.services.databricks_tools import get_databricks_server_config
    with pytest.raises(ValueError, match="DATABRICKS_MCP_SERVER_URL"):
        get_databricks_server_config()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
uv run pytest tests/test_databricks_tools.py -v
```

Expected: `ERROR` — `cannot import name 'get_databricks_server_config'`

- [ ] **Step 4: Replace databricks_tools.py**

Overwrite `databricks-builder-app/server/services/databricks_tools.py` with:

```python
"""SSE MCP server config for Databricks tools.

The databricks-mcp-server runs as a separate process, serving its tools via SSE.
Set DATABRICKS_MCP_SERVER_URL to its /sse endpoint before starting the builder app
(e.g. http://localhost:8080/sse for local dev).
"""

import os

from claude_agent_sdk.types import McpSSEServerConfig
from databricks_mcp_server import TOOL_NAMES


def get_databricks_server_config() -> tuple[McpSSEServerConfig, list[str]]:
    """Return SSE config and prefixed tool names for the Databricks MCP server.

    Raises:
        ValueError: if DATABRICKS_MCP_SERVER_URL is not set.
    """
    url = os.environ.get("DATABRICKS_MCP_SERVER_URL")
    if not url:
        raise ValueError(
            "DATABRICKS_MCP_SERVER_URL is not set. "
            "Start the databricks-mcp-server with --transport sse and point this "
            "env var at its /sse endpoint (e.g. http://localhost:8080/sse)."
        )
    config = McpSSEServerConfig(type="sse", url=url)
    tool_names = [f"mcp__databricks__{name}" for name in TOOL_NAMES]
    return config, tool_names
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_databricks_tools.py -v
```

Expected: all 5 tests `PASSED`

- [ ] **Step 6: Commit**

```bash
cd /path/to/ai-dev-kit
git add databricks-builder-app/server/services/databricks_tools.py \
        databricks-builder-app/tests/__init__.py \
        databricks-builder-app/tests/test_databricks_tools.py \
        databricks-builder-app/pyproject.toml
git commit -m "feat(builder-app): replace in-process databricks tools with SSE config"
```

---

### Task 3: Move operation tracker tools into apx_tools.py

The `check_operation_status` and `list_operations` tools were defined in `databricks_tools.py` to support the async-handoff heartbeat pattern. That pattern is gone with SSE (the MCP server handles its own long-running calls), but these tools must continue to exist so Claude can still call them if needed during a session. Move them to `apx_tools.py`.

**Files:**
- Modify: `databricks-builder-app/server/services/apx_tools.py`

- [ ] **Step 1: Add the two tools to apx_tools.py**

Open `databricks-builder-app/server/services/apx_tools.py`. After the existing `import` block at the top, add `time` to the imports:

```python
import base64
import json
import logging
import threading
import time
from contextvars import copy_context
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server
from databricks.sdk.errors import NotFound
from databricks_tools_core.auth import get_workspace_client
```

Update `APX_TOOL_NAMES` to include the tracker tools:

```python
APX_TOOL_NAMES = [
    "mcp__apx__manage_workspace_files",
    "mcp__apx__create_and_deploy_app",
    "mcp__apx__get_app_status",
    "mcp__apx__check_operation_status",
    "mcp__apx__list_operations",
]
```

Add the two tool functions before `load_apx_tools()`. Copy them exactly from the current `databricks_tools.py` — the `@tool` definitions for `check_operation_status` and `list_operations` (which call `get_operation`, `complete_operation`, `list_operations` from `operation_tracker`). Add the import at the top:

```python
from .operation_tracker import (
    get_operation,
    list_operations as _list_operations,
)
```

Then add the tools (copy from current `databricks_tools.py` `_create_check_operation_status_tool()` and `_create_list_operations_tool()` bodies, but as module-level `@tool`-decorated functions instead of factory functions):

```python
@tool(
    "check_operation_status",
    """Check status of an async operation.

Use this to get results of long-running operations that were moved to
background execution. When a tool takes longer than 30 seconds, it returns
an operation_id instead of blocking. Use this tool to poll for the result.

Args:
    operation_id: The operation ID returned by the long-running tool

Returns:
    - status: 'running', 'completed', or 'failed'
    - tool_name: Name of the original tool
    - result: The operation result (if completed)
    - error: Error message (if failed)
    - elapsed_seconds: Time since operation started
""",
    {"operation_id": str},
)
def _check_operation_status(args: dict) -> dict:
    operation_id = args.get("operation_id", "")
    op = get_operation(operation_id)
    if not op:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": "not_found",
                            "error": (
                                f"Operation {operation_id} not found. "
                                "It may have expired (TTL: 1 hour) or never existed."
                            ),
                        }
                    ),
                }
            ]
        }
    result = {
        "status": op.status,
        "operation_id": op.operation_id,
        "tool_name": op.tool_name,
        "elapsed_seconds": round(time.time() - op.started_at, 1),
    }
    if op.status == "completed":
        result["result"] = op.result
    elif op.status == "failed":
        result["error"] = op.error
    return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}


@tool(
    "list_operations",
    """List all tracked async operations.

Use this to see all operations that are running or recently completed.
Useful for checking what's in progress or finding an operation ID.

Args:
    status: Optional filter - 'running', 'completed', or 'failed'

Returns:
    List of operations with their status and elapsed time
""",
    {"status": str},
)
def _list_ops(args: dict) -> dict:
    status_filter = args.get("status") or None
    ops = _list_operations(status_filter)
    return {"content": [{"type": "text", "text": json.dumps(ops, default=str)}]}
```

Update `load_apx_tools()` to include the new tools:

```python
def load_apx_tools():
    """Return (server, tool_names). Singleton — loaded once on first call."""
    global _apx_server
    if _apx_server is None:
        with _init_lock:
            if _apx_server is None:
                _apx_server = create_sdk_mcp_server(
                    name="apx",
                    tools=[
                        _manage_workspace_files,
                        _create_and_deploy_app,
                        _get_app_status,
                        _check_operation_status,
                        _list_ops,
                    ],
                )
    return _apx_server, APX_TOOL_NAMES
```

- [ ] **Step 2: Verify the import works**

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
uv run python -c "from server.services.apx_tools import load_apx_tools; s, names = load_apx_tools(); print(names)"
```

Expected output:
```
['mcp__apx__manage_workspace_files', 'mcp__apx__create_and_deploy_app', 'mcp__apx__get_app_status', 'mcp__apx__check_operation_status', 'mcp__apx__list_operations']
```

- [ ] **Step 3: Commit**

```bash
cd /path/to/ai-dev-kit
git add databricks-builder-app/server/services/apx_tools.py
git commit -m "feat(builder-app): move operation tracker tools from databricks_tools to apx_tools"
```

---

### Task 4: Update agent.py to use SSE config

**Files:**
- Modify: `databricks-builder-app/server/services/agent.py`

- [ ] **Step 1: Replace the import**

In `agent.py`, change line 48:

```python
# Remove:
from .databricks_tools import load_databricks_tools

# Add:
from .databricks_tools import get_databricks_server_config
```

- [ ] **Step 2: Remove the in-process singleton globals and function**

Remove these lines from `agent.py` (lines ~64-105):

```python
# Cached Databricks tools (loaded once)
_databricks_server = None
_databricks_tool_names = None

...

def get_databricks_tools(force_reload: bool = False):
    """Get Databricks tools, optionally forcing a reload.

    Args:
        force_reload: If True, recreate the MCP server to clear any corrupted state

    Returns:
        Tuple of (server, tool_names)
    """
    global _databricks_server, _databricks_tool_names
    if _databricks_server is None or force_reload:
        if force_reload:
            logger.info('Force reloading Databricks MCP server')
        _databricks_server, _databricks_tool_names = load_databricks_tools()
    return _databricks_server, _databricks_tool_names
```

- [ ] **Step 3: Update the tool-loading call in stream_agent_response**

Find this block inside `stream_agent_response` (around line 351):

```python
    # Get in-process Databricks tools
    databricks_server, databricks_tool_names = get_databricks_tools()
    allowed_tools.extend(databricks_tool_names)
    logger.info(f'Databricks MCP server configured with {len(databricks_tool_names)} tools')
```

Replace with:

```python
    # Connect to Databricks MCP server via SSE
    databricks_config, databricks_tool_names = get_databricks_server_config()
    allowed_tools.extend(databricks_tool_names)
    logger.info(f'Databricks MCP server (SSE) configured with {len(databricks_tool_names)} tools')
```

- [ ] **Step 4: Update mcp_servers in ClaudeAgentOptions**

Find the `ClaudeAgentOptions(...)` call (around line 411). Change:

```python
      mcp_servers={'databricks': databricks_server, 'apx': apx_server},  # In-process SDK tools
```

To:

```python
      mcp_servers={'databricks': databricks_config, 'apx': apx_server},
```

- [ ] **Step 5: Verify the module imports cleanly**

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
DATABRICKS_MCP_SERVER_URL=http://localhost:8080/sse \
  uv run python -c "from server.services.agent import stream_agent_response; print('OK')"
```

Expected: `OK` with no errors.

- [ ] **Step 6: Commit**

```bash
cd /path/to/ai-dev-kit
git add databricks-builder-app/server/services/agent.py
git commit -m "feat(builder-app): use SSE MCP config in agent — remove in-process tool loading"
```

---

### Task 5: Add env var and create start_local.sh

**Files:**
- Modify: `databricks-builder-app/.env.local`
- Create: `databricks-builder-app/scripts/start_local.sh`

- [ ] **Step 1: Add DATABRICKS_MCP_SERVER_URL to .env.local**

Open `databricks-builder-app/.env.local` and append:

```
# Databricks MCP Server (SSE transport)
DATABRICKS_MCP_SERVER_URL=http://localhost:8080/sse
```

- [ ] **Step 2: Create start_local.sh**

Create `databricks-builder-app/scripts/start_local.sh`:

```bash
#!/usr/bin/env bash
# Start the Databricks MCP server (SSE) and the builder app together for local dev.
# Usage: ./scripts/start_local.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PROJECT_DIR")"
MCP_SERVER_DIR="$REPO_ROOT/databricks-mcp-server"

# Kill any existing processes on the ports
echo "Checking for existing processes on :8080 and :8000..."
lsof -ti:8080 | xargs kill -9 2>/dev/null || true
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
sleep 1

cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$(jobs -p)" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

# Start the Databricks MCP server
echo "Starting Databricks MCP server on http://localhost:8080/sse ..."
cd "$PROJECT_DIR"
uv run python "$MCP_SERVER_DIR/run_server.py" --transport sse --port 8080 &

# Give the MCP server a moment to bind
sleep 2

# Start the builder app
echo "Starting builder app on http://localhost:8000 ..."
uv run uvicorn server.app:app --reload --port 8000 --reload-dir server &

echo ""
echo "Services running:"
echo "  Databricks MCP server:  http://localhost:8080/sse"
echo "  Builder app:            http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop both."

wait
```

- [ ] **Step 3: Make the script executable**

```bash
chmod +x /path/to/ai-dev-kit/databricks-builder-app/scripts/start_local.sh
```

- [ ] **Step 4: Commit**

```bash
cd /path/to/ai-dev-kit
git add databricks-builder-app/.env.local \
        databricks-builder-app/scripts/start_local.sh
git commit -m "feat(builder-app): add SSE env var and start_local.sh"
```

---

### Task 6: Local verification

- [ ] **Step 1: Run the full test suite**

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
uv run pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Start both services**

```bash
cd /path/to/ai-dev-kit/databricks-builder-app
./scripts/start_local.sh
```

Expected output includes:
```
Starting Databricks MCP server on http://localhost:8080/sse ...
Starting builder app on http://localhost:8000 ...
```

- [ ] **Step 3: Verify the MCP server is reachable**

In a separate terminal:

```bash
curl -s -N -H "Accept: text/event-stream" http://localhost:8080/sse | head -5
```

Expected: SSE handshake output (event stream headers / `data:` lines), not a connection refused error.

- [ ] **Step 4: Verify the builder app starts cleanly**

```bash
curl -s http://localhost:8000/health 2>/dev/null || curl -s http://localhost:8000/ | head -5
```

Expected: HTTP 200 response (not an import error or crash).

- [ ] **Step 5: Send a test message through the builder app**

Use the builder app UI at `http://localhost:8000` or use the API directly:

```bash
# Create a project first if needed, then invoke agent with a simple query
curl -s -X POST http://localhost:8000/invoke_agent \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<your-project-id>", "message": "List available warehouses"}' | python3 -m json.tool
```

Expected: returns `execution_id` and `conversation_id` without errors.

- [ ] **Step 6: Confirm tool call reaches MCP server**

In the terminal running `start_local.sh`, watch the MCP server logs when the agent invokes `list_warehouses`. Expected: log output from the MCP server showing the tool was called.

- [ ] **Step 7: Commit any cleanup**

```bash
cd /path/to/ai-dev-kit
git add -A
git status  # verify only expected files changed
git commit -m "chore: verify SSE integration works locally"
```

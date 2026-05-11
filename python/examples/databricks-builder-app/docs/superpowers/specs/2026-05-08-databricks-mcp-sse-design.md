# Databricks MCP Server: SSE Transport Cutover

**Date:** 2026-05-08  
**Status:** Approved

## Problem

The `databricks-builder-app` loads all Databricks tools in-process by importing `databricks_mcp_server` directly and wrapping its FastMCP tools with `create_sdk_mcp_server()`. This tightly couples the builder app to the MCP server code, prevents the MCP server from being hosted independently, and requires complex in-process auth propagation via Python contextvars and `copy_context()`.

## Goal

Replace in-process tool loading with an SSE MCP connection. The `databricks-mcp-server` runs as a separate process (local dev) or separate Databricks App (hosted), and the builder app connects to it via `McpSSEServerConfig`. This unlocks independent hosting and eliminates ~350 lines of wrapper/heartbeat/async-handoff complexity in `databricks_tools.py`.

## Auth Model

**Phase 1 (this spec):** The MCP server authenticates as itself.
- Local dev: reads from `~/.databrickscfg` (default profile) or env vars
- Hosted (Databricks App): OAuth M2M via `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` — the SDK handles this automatically

The builder app passes **no auth headers** to the MCP server. All tool calls execute under the MCP server's own credentials.

**Phase 2 (follow-on):** Per-user OBO token passthrough via `Authorization`/`X-Databricks-Host` headers + FastMCP middleware. Out of scope here.

## Changes

### `databricks-mcp-server/run_server.py` (done)

Accepts `--transport` flag (`stdio` default, `sse`, `http`, `streamable-http`) plus `--host`/`--port` for HTTP-based transports. No logic change to tools.

### `databricks-mcp-server/databricks_mcp_server/__init__.py`

Export `TOOL_NAMES: list[str]` — a plain list of all tool names the server registers (e.g. `["execute_sql", "list_warehouses", ...]`). This is the only thing the builder app needs to import from the MCP server package. The actual tool code never runs in the builder app process.

### `databricks-builder-app/server/services/databricks_tools.py`

**Replace entirely.** New responsibility: return the SSE server config and the prefixed tool name list.

```python
def get_databricks_server_config() -> tuple[McpSSEServerConfig, list[str]]:
    url = os.environ["DATABRICKS_MCP_SERVER_URL"]  # hard-fail if not set
    config = McpSSEServerConfig(type="sse", url=url)
    tool_names = [f"mcp__databricks__{n}" for n in TOOL_NAMES]
    return config, tool_names
```

No wrappers, no heartbeat, no async-handoff, no `create_sdk_mcp_server`.

### `databricks-builder-app/server/services/agent.py`

Replace the `get_databricks_tools()` call with `get_databricks_server_config()`. The `mcp_servers` dict entry for `'databricks'` becomes the SSE config dict instead of an SDK server object:

```python
databricks_config, databricks_tool_names = get_databricks_server_config()
...
options = ClaudeAgentOptions(
    mcp_servers={
        'databricks': databricks_config,
        'apx': apx_server,
    },
    ...
)
```

The `force_reload` / `get_databricks_tools` singleton pattern is removed. The operation tracker tools (`check_operation_status`, `list_operations`) move to `apx_tools.py` since they were builder-app-specific bookkeeping, not Databricks API tools.

### `databricks-builder-app/.env.local`

Add:
```
DATABRICKS_MCP_SERVER_URL=http://localhost:8080/sse
```

### `databricks-builder-app/scripts/start_local.sh` (new)

```bash
#!/usr/bin/env bash
set -e
REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
MCP_DIR="$REPO_ROOT/../databricks-mcp-server"

# Start MCP server in background
(cd "$MCP_DIR" && uv run python run_server.py --transport sse --port 8080) &
MCP_PID=$!
trap "kill $MCP_PID 2>/dev/null" EXIT

# Start builder app
cd "$REPO_ROOT"
uv run uvicorn server.app:app --reload --port 8000
```

## What Does Not Change

- `apx_tools.py` — in-process workspace upload + app deploy/status tools (no change)
- All other services — untouched
- Hosted Databricks App auth — works via OAuth M2M with no code change
- The MCP server's tools — no changes to any tool implementations

## Out of Scope

- Per-user OBO token passthrough
- TLS for the local SSE connection
- MCP server health-check / auto-reconnect in the builder app

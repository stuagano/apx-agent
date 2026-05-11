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

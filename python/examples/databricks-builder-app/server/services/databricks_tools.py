"""SSE MCP server config for Databricks tools.

The databricks-mcp-server runs as a separate process, serving its tools via SSE.
Set DATABRICKS_MCP_SERVER_URL to its /sse endpoint before starting the builder app
(e.g. http://localhost:8080/sse for local dev).
"""

import os

from claude_agent_sdk.types import McpSSEServerConfig

# Tool names served by the databricks-mcp-server.
# Keep in sync with databricks_mcp_server/databricks_mcp_server/__init__.py.
TOOL_NAMES: list[str] = [
    "ask_genie", "ask_genie_followup", "cancel_run", "create_job",
    "create_or_update_dashboard", "create_or_update_genie", "create_or_update_ka",
    "create_or_update_mas", "create_or_update_pipeline", "create_pipeline",
    "create_volume_directory", "delete_genie", "delete_job", "delete_ka",
    "delete_mas", "delete_pipeline", "delete_volume_directory", "delete_volume_file",
    "download_from_volume", "execute_databricks_command", "execute_sql",
    "execute_sql_multi", "find_job_by_name", "find_ka_by_name", "find_mas_by_name",
    "find_pipeline_by_name", "get_best_cluster", "get_best_warehouse", "get_dashboard",
    "get_genie", "get_job", "get_ka", "get_mas", "get_pipeline", "get_pipeline_events",
    "get_run", "get_run_output", "get_serving_endpoint_status", "get_table_details",
    "get_update", "get_volume_file_info", "list_clusters", "list_dashboards",
    "list_genie", "list_jobs", "list_runs", "list_serving_endpoints", "list_volume_files",
    "list_warehouses", "manage_uc_connections", "manage_uc_grants", "manage_uc_monitors",
    "manage_uc_objects", "manage_uc_security_policies", "manage_uc_sharing",
    "manage_uc_storage", "manage_uc_tags", "publish_dashboard", "query_serving_endpoint",
    "run_job_now", "run_python_file_on_databricks", "start_update", "stop_pipeline",
    "trash_dashboard", "unpublish_dashboard", "update_job", "update_pipeline",
    "upload_file", "upload_folder", "upload_to_volume", "wait_for_run",
]


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

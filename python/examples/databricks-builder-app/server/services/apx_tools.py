"""In-process apx tools for building and deploying Databricks Apps via the Agent SDK.

These tools give the agent the ability to:
  - Upload scaffolded files to the Databricks workspace
  - Create and deploy a Databricks App from a workspace path
  - Poll app deployment status

Auth is propagated via contextvars (set_databricks_auth in agent.py),
so tools always run with the requesting user's token — no separate subprocess
or MCP server process needed.
"""

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

from .operation_tracker import get_operation
from .operation_tracker import list_operations as _list_operations

logger = logging.getLogger(__name__)

_apx_server = None
_init_lock = threading.Lock()

APX_TOOL_NAMES = [
    "mcp__apx__manage_workspace_files",
    "mcp__apx__create_and_deploy_app",
    "mcp__apx__get_app_status",
    "mcp__apx__check_operation_status",
    "mcp__apx__list_operations",
]


def _upload_directory(args: dict) -> dict:
    try:
        local_path = Path(args["local_path"])
        workspace_path = args["workspace_path"].rstrip("/")

        ws = get_workspace_client()
        from databricks.sdk.service.workspace import ImportFormat

        ws.workspace.mkdirs(workspace_path)
        uploaded = []
        for file_path in sorted(local_path.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(local_path)
            ws_file_path = f"{workspace_path}/{relative}"
            ws_parent = ws_file_path.rsplit("/", 1)[0]
            if ws_parent != workspace_path:
                ws.workspace.mkdirs(ws_parent)
            with open(file_path, "rb") as f:
                content = f.read()
            ws.workspace.import_(
                path=ws_file_path,
                content=base64.b64encode(content).decode(),
                format=ImportFormat.RAW,
                overwrite=True,
            )
            uploaded.append(str(relative))

        return {
            "status": "success",
            "workspace_path": workspace_path,
            "files_uploaded": len(uploaded),
            "files": uploaded,
        }
    except Exception as exc:
        logger.error("manage_workspace_files failed: %s", exc)
        return {"error": str(exc)}


@tool(
    "manage_workspace_files",
    (
        "Upload a local directory to the Databricks workspace. "
        "Use local_path='/tmp/my-agent' and workspace_path='/Workspace/Users/<email>/my-agent'."
    ),
    {"local_path": str, "workspace_path": str},
)
def _manage_workspace_files(args: dict) -> dict:
    result = copy_context().run(_upload_directory, args)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool(
    "create_and_deploy_app",
    (
        "Create a Databricks App (if it doesn't exist) and deploy it from a workspace path. "
        "Returns the app name and URL once deployment is triggered."
    ),
    {"app_name": str, "source_code_path": str},
)
def _create_and_deploy_app(args: dict) -> dict:
    def run():
        try:
            ws = get_workspace_client()
            app_name = args["app_name"]
            source_code_path = args["source_code_path"]
            try:
                ws.apps.get(app_name)
                logger.info("App %s already exists, redeploying", app_name)
            except NotFound:
                logger.info("Creating app %s", app_name)
                ws.apps.create(name=app_name, description="Agent built by apx-builder")
            ws.apps.deploy(app_name=app_name, source_code_path=source_code_path)
            app = ws.apps.get(app_name)
            return {
                "name": app_name,
                "url": app.url or "",
                "status": "deployment triggered",
            }
        except Exception as exc:
            logger.error("create_and_deploy_app failed: %s", exc)
            return {"error": str(exc)}

    result = copy_context().run(run)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool(
    "get_app_status",
    "Check the deployment status and URL of a Databricks App.",
    {"app_name": str},
)
def _get_app_status(args: dict) -> dict:
    def run():
        try:
            ws = get_workspace_client()
            app = ws.apps.get(args["app_name"])
            active = app.active_deployment
            return {
                "name": args["app_name"],
                "url": app.url or "",
                "app_state": app.app_status.state.value if app.app_status else "UNKNOWN",
                "deploy_state": (
                    active.status.state.value
                    if active and active.status
                    else "UNKNOWN"
                ),
            }
        except Exception as exc:
            logger.error("get_app_status failed: %s", exc)
            return {"error": str(exc)}

    result = copy_context().run(run)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


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

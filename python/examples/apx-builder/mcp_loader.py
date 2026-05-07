"""Load in-process MCP servers for the claude-agent-sdk agent."""
import base64
import json
import logging
import threading
from contextvars import copy_context
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server
from databricks.sdk.errors import NotFound
from databricks_tools_core.auth import get_workspace_client

logger = logging.getLogger(__name__)

# Singletons — loaded once at first request
_apx_server = None
_apx_tool_names = [
    "mcp__apx__manage_workspace_files",
    "mcp__apx__create_and_deploy_app",
    "mcp__apx__get_app_status",
]
_init_lock = threading.Lock()


def _upload_directory(args: dict) -> dict:
    """Implementation for manage_workspace_files. Separated for testability."""
    try:
        action = args.get("action", "upload")
        local_path = Path(args["local_path"])
        workspace_path = args["workspace_path"].rstrip("/")

        if action != "upload":
            return {"error": f"Unsupported action: {action}. Only 'upload' is supported."}

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
        }
    except Exception as exc:
        logger.error("manage_workspace_files failed: %s", exc)
        return {"error": str(exc)}


@tool(
    "manage_workspace_files",
    (
        "Upload a local directory to the Databricks workspace. "
        "Use action='upload', local_path='/tmp/dir', workspace_path='/Workspace/...'."
    ),
    {"action": str, "local_path": str, "workspace_path": str},
)
def _manage_workspace_files(args: dict) -> dict:
    ctx = copy_context()
    result = ctx.run(_upload_directory, args)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


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
        try:
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
        except Exception as exc:
            logger.error("create_and_deploy_app failed: %s", exc)
            return {"error": str(exc)}

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
        try:
            ws = get_workspace_client()
            app = ws.apps.get(args["app_name"])
            active = app.active_deployment
            return {
                "name": args["app_name"],
                "url": app.url or "",
                "app_state": app.app_status.state.value if app.app_status else "UNKNOWN",
                "deploy_state": active.status.state.value if active and active.status else "UNKNOWN",
            }
        except Exception as exc:
            logger.error("get_app_status failed: %s", exc)
            return {"error": str(exc)}

    result = ctx.run(run)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


def get_mcp_servers() -> tuple[dict, list[str]]:
    """Return (servers_dict, all_tool_names). Singleton loaded on first call."""
    global _apx_server

    if _apx_server is None:
        with _init_lock:
            if _apx_server is None:
                _apx_server = create_sdk_mcp_server(
                    name="apx",
                    tools=[_manage_workspace_files, _create_and_deploy_app, _get_app_status],
                )

    return (
        {"apx": _apx_server},
        _apx_tool_names,
    )

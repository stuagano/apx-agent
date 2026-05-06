"""Load in-process MCP servers for the claude-agent-sdk agent."""
import json
import logging
import threading
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
_init_lock = threading.Lock()


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

    Propagates Databricks auth context vars to the inner call via copy_context().
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
    """Return (servers_dict, all_tool_names). Singletons loaded on first call.

    servers_dict is passed directly to ClaudeAgentOptions.mcp_servers.
    tool_names are the allowed_tools names in mcp__<server>__<tool> format.
    """
    global _databricks_server, _databricks_tool_names, _apx_server

    if _databricks_server is None:
        with _init_lock:
            if _databricks_server is None:
                from databricks_mcp_server.server import mcp
                from databricks_mcp_server.tools import sql, file, genie, compute  # noqa: F401

                import asyncio
                import concurrent.futures

                # asyncio.run() cannot be called from a running event loop (e.g. uvicorn),
                # so we load tools in a worker thread that has no event loop.
                def _load():
                    return asyncio.run(mcp.list_tools())

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                    mcp_tools = _ex.submit(_load).result()

                sdk_tools = []
                names = []
                for mcp_tool in mcp_tools:
                    schema = _convert_schema(mcp_tool.parameters)
                    sdk_tools.append(_make_wrapper(mcp_tool.name, mcp_tool.description, schema, mcp_tool.fn))
                    names.append(f"mcp__databricks__{mcp_tool.name}")

                _databricks_server = create_sdk_mcp_server(name="databricks", tools=sdk_tools)
                _databricks_tool_names = names
                logger.info("Loaded %d databricks MCP tools", len(names))

    if _apx_server is None:
        with _init_lock:
            if _apx_server is None:
                _apx_server = create_sdk_mcp_server(
                    name="apx",
                    tools=[_create_and_deploy_app, _get_app_status],
                )

    return (
        {"databricks": _databricks_server, "apx": _apx_server},
        _databricks_tool_names + _apx_tool_names,
    )

"""Databricks-provided Unity Gateway MCP services (``system.ai.*``) as agent tools (#1268).

Unity Gateway serves MCP servers as Unity Catalog securables at
``https://<workspace>/ai-gateway/mcp-services/<catalog>.<schema>.<service>``,
called with the ``ai-gateway`` OAuth scope. Calls are subject to Unity Gateway
grants, policies, and tracing. The workspace Managed MCP endpoints
(``/api/2.0/mcp/...``) bypass Unity Gateway; prefer these services for new
integrations when they cover the use case.

``unity_gateway_mcp(service)`` wraps the service's MCP tools through the same
``mcp_consume`` path as ``mcp_toolkit``, run as the calling user (OBO). Each
tool declares the ``ai-gateway`` user scope and a
``ServiceBinding(service, "user")`` — the binding shape ``sandbox_tool`` uses
(#1266) — so ``apx-agent doctor`` can explain the preview behind it.
"""

from __future__ import annotations

from typing import Any

from .mcp_consume import DEFAULT_TIMEOUT_S

MCP_SERVICES_PATH = "/ai-gateway/mcp-services/"

# Beta services that need the account-level Enhanced Unity Gateway preview.
ENHANCED_UG_SERVICES = frozenset({"system.ai.dbsql", "system.ai.web_search", "system.ai.sandbox"})

# GA Databricks-provided connectors: each user signs in to the provider with their own account.
CONNECTOR_SERVICES = frozenset({
    "system.ai.asana",
    "system.ai.atlassian",
    "system.ai.github",
    "system.ai.gmail",
    "system.ai.google_calendar",
    "system.ai.google_drive",
    "system.ai.microsoft_365",
    "system.ai.slack",
})

ENABLE_ENHANCED_UG = (
    "An account admin enables the Enhanced Unity Gateway preview on the account console "
    "Previews page (required for system.ai.dbsql, system.ai.sandbox and system.ai.web_search)."
)
CONNECTOR_SIGN_IN = (
    "Each user signs in to the provider with their own account the first time the tool runs; "
    "the provider's org admin may need to approve the Databricks app."
)


def unity_gateway_mcp_path(service: str) -> str:
    """Workspace path of a Unity Gateway MCP service, e.g. ``/ai-gateway/mcp-services/system.ai.dbsql``."""
    if len(service.split(".")) != 3 or "" in service.split("."):
        raise ValueError(f"Unity Gateway MCP service must be catalog.schema.service, got {service!r}")
    return f"{MCP_SERVICES_PATH}{service}"


def unity_gateway_mcp_url(service: str, *, workspace_host: str) -> str:
    """Full Unity Gateway MCP URL for ``service`` on ``workspace_host`` (scheme optional)."""
    from ._managed_mcp import _normalise_host

    return f"{_normalise_host(workspace_host)}{unity_gateway_mcp_path(service)}"


def unity_gateway_mcp(
    service: str,
    *,
    tools: list[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[Any]:
    """Wrap a Databricks-provided Unity Gateway MCP service's tools as agent tools.

    Args:
        service: A ``system.ai.*`` MCP service, e.g. ``"system.ai.web_search"``,
            ``"system.ai.dbsql"``, ``"system.ai.sandbox"``, ``"system.ai.github"``.
        tools: Remote tool names to wrap. When given, no network call is made at
            construction. When omitted, the service's tools are listed once with
            the default workspace client (the App service principal in Apps, your
            profile locally), which needs access to the service.
        timeout_s: Per-call ceiling in seconds for every wrapped tool.

    Every call runs as the calling user (OBO) against the caller's workspace.
    Local tool names are ``<service>_<remote tool>`` (e.g. ``web_search_search``)
    so they cannot collide with ``sandbox_tool``'s ``run_in_sandbox`` or another
    service's tools.

    Example::

        from apx_agent import Agent, unity_gateway_mcp

        agent = Agent(tools=[*unity_gateway_mcp("system.ai.web_search")])
    """
    from ._resources import ServiceBinding, bind_service, require_user_api_scopes
    from .mcp_consume import _make_mcp_tool, _mark_remote_description, _slug

    binding = ServiceBinding(service, "user")  # validates system.ai.*
    path = unity_gateway_mcp_path(service)
    short = service.rsplit(".", 1)[1]
    if tools is not None:
        if not tools:
            raise ValueError("unity_gateway_mcp(tools=[]) wraps nothing; omit tools to discover them")
        remote: dict[str, str | None] = dict.fromkeys(tools)
    else:
        remote = _discover(service, path)
    out = []
    for remote_name, description in remote.items():
        tool = _make_mcp_tool(
            path, remote_name, description=_mark_remote_description(description) or "",
            headers=None, transport="http", name=_slug(f"{short}_{remote_name}"), timeout_s=timeout_s,
        )
        require_user_api_scopes(tool, ["ai-gateway"])
        out.append(bind_service(tool, binding))
    return out


def _discover(service: str, path: str) -> dict[str, str | None]:
    """Name → description of every tool on the service, listed with the default workspace client."""
    from ._defaults import _make_workspace_client
    from .mcp_consume import _list_remote_tools, _run_sync

    ws = _make_workspace_client()
    url = f"{ws.config.host.rstrip('/')}{path}"
    try:
        listed = _run_sync(_list_remote_tools(url, ws.config.authenticate(), "http"))
    except Exception as exc:
        hint = ENABLE_ENHANCED_UG if service in ENHANCED_UG_SERVICES else (
            "Grant the identity access to the MCP service in Unity Gateway.")
        raise ValueError(
            f"Could not list the tools on Unity Gateway MCP service {service} ({exc}). {hint} "
            "Pass tools=[...] to skip discovery."
        ) from exc
    return {t.name: t.description for t in listed}

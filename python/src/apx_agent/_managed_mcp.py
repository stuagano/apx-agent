"""Databricks Managed MCP integration — generate URLs and client configs.

Databricks Managed MCP (Public Preview, Jan 2026) exposes Unity Catalog
functions, Genie spaces, and Vector Search indexes as MCP servers, hosted
by the platform. Anything an apx-agent declares as a Mosaic AI resource
becomes a Managed MCP endpoint automatically — there's no per-agent MCP
server to deploy, no app.yaml to maintain, no naming conventions for
discovery.

This module provides two helpers:

  * ``managed_mcp_urls(agent, workspace_host)`` — walks the agent's
    declared ``ResourceSpec`` list and emits the Managed MCP URL for
    each resource, paired with the OAuth scope it needs.

  * ``managed_mcp_client_config(endpoints, name=...)`` — produces the
    MCP server config snippet ready to drop into Claude Desktop,
    Cursor, or any client that speaks the standard ``mcpServers``
    config shape.

URL patterns (from the Databricks managed MCP docs):
  * UC function:   ``/api/2.0/mcp/functions/{catalog}/{schema}/{function_name}``
  * Genie space:   ``/api/2.0/mcp/genie/{genie_space_id}``
  * Vector Search: ``/api/2.0/mcp/vector-search/{catalog}/{schema}/{index_name}``

Resources outside those three kinds (serving endpoints, SQL warehouses,
UC tables) don't have a corresponding Managed MCP server today — the
walker reports them with ``url=None`` and ``unsupported=True`` so the
caller can decide how to surface them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._resources import collect_resource_specs

if TYPE_CHECKING:
    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedMCPEndpoint:
    """A single Databricks Managed MCP endpoint.

    Attributes:
        kind: Resource kind from the underlying ``ResourceSpec`` —
            ``"uc_function"``, ``"genie_space"``,
            ``"vector_search_index"``, or one of the unsupported kinds
            (``"serving_endpoint"``, ``"sql_warehouse"``, ``"uc_table"``).
        identifier: The resource identifier (full UC name, space id, etc.).
        url: The fully-qualified Managed MCP URL. ``None`` for unsupported
            kinds.
        oauth_scope: The OAuth scope a client must request to call the
            endpoint. ``None`` for unsupported kinds.
        unsupported: ``True`` when this resource kind has no Managed MCP
            equivalent (the caller may want to skip it or surface it
            separately).
    """

    kind: str
    identifier: str
    url: str | None
    oauth_scope: str | None
    unsupported: bool = False


# ---------------------------------------------------------------------------
# Mapping table
# ---------------------------------------------------------------------------


# kind -> (path template, oauth scope). Path templates use {0}, {1}, {2}
# placeholders filled from the dotted identifier.
_KIND_TO_PATH: dict[str, tuple[str, str]] = {
    "uc_function": ("/api/2.0/mcp/functions/{0}/{1}/{2}", "unity-catalog"),
    "genie_space": ("/api/2.0/mcp/genie/{0}", "genie"),
    "vector_search_index": ("/api/2.0/mcp/vector-search/{0}/{1}/{2}", "vector-search"),
}


def _normalise_host(workspace_host: str) -> str:
    """Strip trailing slash and ensure the host has a scheme."""
    host = workspace_host.rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


def _build_url(kind: str, identifier: str, workspace_host: str) -> tuple[str | None, str | None]:
    """Return ``(url, oauth_scope)`` for the kind/identifier pair, or ``(None, None)``."""
    template_scope = _KIND_TO_PATH.get(kind)
    if template_scope is None:
        return None, None
    template, scope = template_scope

    if kind == "genie_space":
        return f"{workspace_host}{template.format(identifier)}", scope

    # UC function / vector search use three-part identifiers
    parts = identifier.split(".")
    if len(parts) != 3:
        logger.warning(
            "Managed MCP URL for %s expects a three-part identifier; got %r — skipping.",
            kind, identifier,
        )
        return None, None
    return f"{workspace_host}{template.format(*parts)}", scope


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def managed_mcp_urls(
    agent: "BaseAgent",
    *,
    workspace_host: str,
    model: str | None = None,
) -> list[ManagedMCPEndpoint]:
    """Return the Managed MCP endpoints for every resource the agent declares.

    Args:
        agent: The apx-agent ``BaseAgent`` to walk.
        workspace_host: Databricks workspace host (e.g.
            ``"https://my-workspace.cloud.databricks.com"`` — scheme
            optional, trailing slash optional).
        model: Optional model serving endpoint name to include in the
            walk. Passed through to ``collect_resource_specs``; usually
            irrelevant for Managed MCP since model endpoints aren't
            exposed as MCP servers, but kept for symmetry with
            ``log_agent``.

    Returns:
        One ``ManagedMCPEndpoint`` per declared resource. Endpoints whose
        kind has no Managed MCP equivalent are returned with
        ``unsupported=True`` and ``url=None`` so the caller can choose to
        filter or surface them separately. Ordering preserves the
        walker's first-occurrence order.
    """
    host = _normalise_host(workspace_host)
    specs = collect_resource_specs(agent, model=model)

    endpoints: list[ManagedMCPEndpoint] = []
    for spec in specs:
        url, scope = _build_url(spec.kind, spec.identifier, host)
        if url is None:
            endpoints.append(ManagedMCPEndpoint(
                kind=spec.kind,
                identifier=spec.identifier,
                url=None,
                oauth_scope=None,
                unsupported=True,
            ))
        else:
            endpoints.append(ManagedMCPEndpoint(
                kind=spec.kind,
                identifier=spec.identifier,
                url=url,
                oauth_scope=scope,
                unsupported=False,
            ))
    return endpoints


def managed_mcp_client_config(
    endpoints: list[ManagedMCPEndpoint],
    *,
    name: str = "databricks",
    include_unsupported: bool = False,
) -> dict:
    """Produce an MCP client config snippet from a list of endpoints.

    Returns a dict in the standard ``mcpServers`` shape understood by
    Claude Desktop, Cursor, and other MCP-aware clients. Drop the
    returned dict into the client's config file (``claude_desktop_config.json``,
    ``~/.cursor/mcp.json``, etc.) under the ``mcpServers`` key.

    Each endpoint becomes one server entry. The key is derived from the
    kind and identifier — e.g. ``"databricks.uc_function.main.tools.classify_intent"``
    — so multiple endpoints from one agent don't collide.

    Args:
        endpoints: Output of ``managed_mcp_urls(...)``.
        name: Prefix applied to every server key. Defaults to
            ``"databricks"``. Override to namespace multiple agents
            cleanly in the same client config.
        include_unsupported: When ``False`` (default), endpoints with
            ``unsupported=True`` are skipped. Set ``True`` if you want
            them in the output anyway (e.g. as a TODO list).

    Returns:
        A dict of shape::

            {
              "mcpServers": {
                "databricks.uc_function.main.tools.classify_intent": {
                  "type": "http",
                  "url": "https://.../api/2.0/mcp/functions/main/tools/classify_intent",
                  "oauth_scope": "unity-catalog"
                },
                ...
              }
            }
    """
    servers: dict[str, dict] = {}
    for ep in endpoints:
        if ep.unsupported and not include_unsupported:
            continue
        key = f"{name}.{ep.kind}.{ep.identifier}"
        entry: dict = {"type": "http", "url": ep.url}
        if ep.oauth_scope:
            entry["oauth_scope"] = ep.oauth_scope
        if ep.unsupported:
            entry["unsupported"] = True
        servers[key] = entry
    return {"mcpServers": servers}

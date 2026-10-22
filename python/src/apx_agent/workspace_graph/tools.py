"""workspace_graph_tools — the workspace graph as six OBO-scoped agent tools.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so ``UserClientDependency`` resolves eagerly — see catalog.py.
"""

import asyncio
from typing import Any

from .._tool_factory import build_tool
from .builder import Graph, get_graph
from .schema import Node

_LIST_CAP = 50


def _brief(n: Node) -> dict[str, str]:
    return {"id": n.id, "type": n.type, "name": n.name, "fqn": n.fqn}


def _capped(nodes: list[Node]) -> dict[str, Any]:
    return {
        "count": len(nodes),
        "results": [_brief(n) for n in nodes[:_LIST_CAP]],
        "truncated": len(nodes) > _LIST_CAP,
    }


def _not_found(graph: Graph, identifier: str) -> dict[str, Any]:
    return {
        "error": "not_found",
        "identifier": identifier,
        "suggestions": graph.suggest(identifier),
    }


def workspace_graph_tools(catalogs: list[str] | None = None) -> list[Any]:
    """Return six tools that query the caller's workspace graph.

    Usage::

        from apx_agent import Agent, workspace_graph_tools

        agent = Agent(tools=[*workspace_graph_tools(catalogs=["main"])])

    The graph (Unity Catalog, Genie, Vector Search, Model Serving, apx Apps and
    the edges between them) is built with the caller's own credentials and
    cached per caller for 10 minutes.

    Args:
        catalogs: Catalogs to load. ``None`` loads the first 10 visible catalogs.
    """
    from .._defaults import UserClientDependency

    scope = list(catalogs) if catalogs else None

    async def _graph(ws: Any) -> Graph:
        return await asyncio.to_thread(get_graph, ws, scope)

    async def graph_summary(ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        return (await _graph(ws)).summary()

    async def search_assets(
        ws: UserClientDependency,
        query: str,
        asset_type: str | None = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        return _capped((await _graph(ws)).search(query, asset_type))

    async def get_asset(ws: UserClientDependency, identifier: str) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        return graph.get(identifier) or _not_found(graph, identifier)

    async def analyze_impact(
        ws: UserClientDependency, identifier: str
    ) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        node = graph.resolve(identifier)
        if node is None:
            return _not_found(graph, identifier)
        return {"asset": _brief(node), **_capped(graph.impact(node.id))}

    async def get_lineage(ws: UserClientDependency, identifier: str) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        node = graph.resolve(identifier)
        if node is None:
            return _not_found(graph, identifier)
        lin = graph.lineage(node.id)
        return {
            "asset": _brief(node),
            "upstream": [_brief(n) for n in lin["upstream"][:_LIST_CAP]],
            "downstream": [_brief(n) for n in lin["downstream"][:_LIST_CAP]],
        }

    async def find_orphans(ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        return _capped((await _graph(ws)).orphans())

    specs = [
        (
            graph_summary,
            "graph_summary",
            "Summarize the workspace graph: asset counts by type, edge counts, warnings "
            "(sources that could not be read), and when it was built. Call this first.",
        ),
        (
            search_assets,
            "search_assets",
            "Search workspace assets by name or fully-qualified name. Optionally filter by "
            "asset_type: Catalog, Schema, Table, View, Volume, Function, Model, GenieSpace, "
            "VectorSearchIndex, ServingEndpoint, App. Returns ids for the other tools.",
        ),
        (
            get_asset,
            "get_asset",
            "Get one asset by id or fully-qualified name: its properties (columns, comment, "
            "owner-facing details) and every directly connected asset.",
        ),
        (
            analyze_impact,
            "analyze_impact",
            "What breaks if this asset changes or is dropped? Returns everything contained in "
            "it or downstream of it (views, indexes, Genie spaces, endpoints, apps).",
        ),
        (
            get_lineage,
            "get_lineage",
            "Data-flow lineage for an asset: upstream sources and downstream consumers, up to "
            "3 hops, excluding catalog/schema containment.",
        ),
        (
            find_orphans,
            "find_orphans",
            "Assets nothing reads, feeds, indexes, serves, or uses — candidates for cleanup.",
        ),
    ]
    return [build_tool(fn, name=name, description=desc) for fn, name, desc in specs]

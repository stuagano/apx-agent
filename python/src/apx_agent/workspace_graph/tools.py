"""workspace_graph_tools — the workspace graph as six OBO-scoped agent tools.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so ``UserClientDependency`` resolves eagerly — see catalog.py.
"""

import asyncio
from typing import Any

from .._resources import require_user_api_scopes
from .._tool_factory import build_tool
from .builder import Graph, get_graph
from .schema import Node

_LIST_CAP = 50

# OBO scopes every connector needs; without them a deployed App gets only the
# scaffold baseline (ai-gateway, sql) and every connector fails -> empty graph.
SCOPES = [
    "catalog.catalogs:read",
    "catalog.schemas:read",
    "catalog.tables:read",
    "genie",
    "vector-search",
    "model-serving",
    "apps",
]


def _brief(n: Node) -> dict[str, str]:
    return {"id": n.id, "type": n.type, "name": n.name, "fqn": n.fqn}


def _capped(nodes: list[Node]) -> dict[str, Any]:
    return {
        "count": len(nodes),
        "results": [_brief(n) for n in nodes[:_LIST_CAP]],
        "truncated": len(nodes) > _LIST_CAP,
    }


def _partial(graph: Graph) -> dict[str, Any]:
    """Tell the agent when some sources could not be read."""
    if not graph.warnings:
        return {"partial": False}
    return {"partial": True, "warnings": graph.warnings}


def _not_found(graph: Graph, identifier: str) -> dict[str, Any]:
    return {
        "error": "not_found",
        "identifier": identifier,
        "suggestions": graph.suggest(identifier),
        **_partial(graph),
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
        graph = await _graph(ws)
        return {**graph.summary(), **_partial(graph)}

    async def search_assets(
        ws: UserClientDependency,
        query: str,
        asset_type: str | None = None,
    ) -> dict[str, Any]:
        graph = await _graph(ws)
        return {**_capped(graph.search(query, asset_type)), **_partial(graph)}

    async def get_asset(ws: UserClientDependency, identifier: str) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        result = graph.get(identifier)
        if result is None:
            return _not_found(graph, identifier)
        outgoing = result["outgoing"]
        incoming = result["incoming"]
        return {
            "node": result["node"],
            "outgoing": outgoing[:_LIST_CAP],
            "outgoing_count": len(outgoing),
            "incoming": incoming[:_LIST_CAP],
            "incoming_count": len(incoming),
            "truncated": len(outgoing) > _LIST_CAP or len(incoming) > _LIST_CAP,
            **_partial(graph),
        }

    async def analyze_impact(
        ws: UserClientDependency, identifier: str
    ) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        node = graph.resolve(identifier)
        if node is None:
            return _not_found(graph, identifier)
        return {
            "asset": _brief(node),
            **_capped(graph.impact(node.id)),
            **_partial(graph),
        }

    async def get_lineage(ws: UserClientDependency, identifier: str) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        node = graph.resolve(identifier)
        if node is None:
            return _not_found(graph, identifier)
        lin = graph.lineage(node.id)
        upstream = lin["upstream"]
        downstream = lin["downstream"]
        return {
            "asset": _brief(node),
            "upstream": [_brief(n) for n in upstream[:_LIST_CAP]],
            "upstream_count": len(upstream),
            "downstream": [_brief(n) for n in downstream[:_LIST_CAP]],
            "downstream_count": len(downstream),
            "truncated": len(upstream) > _LIST_CAP or len(downstream) > _LIST_CAP,
            **_partial(graph),
        }

    async def find_orphans(ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        graph = await _graph(ws)
        out: dict[str, Any] = {**_capped(graph.orphans()), **_partial(graph)}
        complete = not any(w.startswith("lineage:") for w in graph.warnings)
        out["lineage_complete"] = complete
        if not complete:
            out["note"] = (
                "Lineage was incomplete (see warnings); some assets listed may have "
                "upstream or downstream data flow that was not loaded."
            )
        return out

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
            "owner-facing details) and every directly connected asset. Connections (outgoing/"
            "incoming) are capped at 50 items with outgoing_count, incoming_count, and "
            "truncated flag to signal if results were cut.",
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
            "3 hops, excluding catalog/schema containment. Results are capped at 50 items each "
            "with upstream_count, downstream_count, and truncated flag to signal if cut.",
        ),
        (
            find_orphans,
            "find_orphans",
            "Candidate orphans: assets nothing reads, feeds, indexes, serves, or uses. Results "
            "are candidates only; check lineage_complete (false means lineage was not fully "
            "loaded, so some listed assets may have data flow that was missed).",
        ),
    ]
    return [
        require_user_api_scopes(build_tool(fn, name=name, description=desc), SCOPES)
        for fn, name, desc in specs
    ]

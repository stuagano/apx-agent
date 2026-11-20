"""Dev UI discover routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    Any,
    DiscoverGraphResponse,
    DiscoverImpactResponse,
    FileResponse,
    HTMLResponse,
    HTTPException,
    Request,
    _overlay_agent,
    _topo_static_root,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    def _discover_scope(catalogs: str | None) -> list[str] | None:
        if catalogs is None:
            return None
        names = [c.strip() for c in catalogs.split(",") if c.strip()]
        return names or None

    @router.get("/_apx/discover", include_in_schema=False)
    async def discover_ui() -> Any:
        """Workspace graph viewer — second entry of the Topology bundle."""
        page = _topo_static_root / "discover.html"
        if not page.exists():
            return HTMLResponse(
                "Discover UI not built — run "
                "<code>cd python/dev-ui/topology &amp;&amp; npm run build</code>.",
                status_code=503,
            )
        return FileResponse(page, media_type="text/html")

    @router.get("/_apx/discover/graph.json", response_model=DiscoverGraphResponse)
    async def discover_graph(
        request: Request, catalogs: str | None = None, refresh: bool = False
    ) -> Any:
        """The caller's workspace graph — the same graph their agent tools see.

        OBO only (#612): fails closed (401) on Apps without a user token.
        """
        import asyncio as _asyncio

        from apx_agent._defaults import _ws_prefer_obo
        from apx_agent.workspace_graph import builder

        ws = _ws_prefer_obo(request)
        graph = await _asyncio.to_thread(
            builder.get_graph, ws, _discover_scope(catalogs), refresh=refresh
        )
        payload = graph.to_json()
        payload["agent"] = _overlay_agent(payload, request.app.state.agent_context)
        return payload

    @router.get("/_apx/discover/impact/{node_id:path}", response_model=DiscoverImpactResponse)
    async def discover_impact(request: Request, node_id: str, catalogs: str | None = None) -> Any:
        """Downstream ids for the canvas highlight — same path as ``analyze_impact``."""
        import asyncio as _asyncio

        from apx_agent._defaults import _ws_prefer_obo
        from apx_agent.workspace_graph import builder

        ws = _ws_prefer_obo(request)
        graph = await _asyncio.to_thread(builder.get_graph, ws, _discover_scope(catalogs))
        node = graph.resolve(node_id)
        if node is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "suggestions": graph.suggest(node_id)},
            )
        return {"id": node.id, "impacted": [n.id for n in graph.impact(node.id)]}

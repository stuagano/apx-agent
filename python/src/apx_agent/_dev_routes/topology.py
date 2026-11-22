"""Dev UI topology routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    FileResponse,
    HTMLResponse,
    HTTPException,
    JSONResponse,
    Request,
    TopologyDigestResponse,
    TopologyResponse,
    TopologyTracingResponse,
    TopologyTracingSetRequest,
    TopologyTracingSetResponse,
    _experiment_url,
    _find_env_path,
    _last_route_payload,
    _load_topology_node_metadata,
    _lookup_experiment_name,
    _topo_static_root,
    _vendor_root,
    _workspace_host_from_request,
    _write_env_file,
    _ws_upload_agent_file,
    annotate_topology,
    build_topology,
    inspect_node,
    logger,
    os,
    summarize_topology,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    # ------------------------------------------------------------------
    # Topology UI — interactive react-flow graph at /_apx/topology.
    # Serves the built React bundle from _static/topology/, plus two JSON
    # endpoints the UI fetches: /_apx/topology.json (full graph) and
    # /_apx/topology/inspect/{node_id} (per-node details).
    # ------------------------------------------------------------------

    @router.get("/_apx/topology", include_in_schema=False)
    async def topology_index() -> Any:
        index = _topo_static_root / "index.html"
        if not index.exists():
            return HTMLResponse(
                "Topology UI not built — run "
                "<code>cd python/dev-ui/topology &amp;&amp; npm run build</code>.",
                status_code=503,
            )
        return FileResponse(index, media_type="text/html")

    @router.get("/_apx/topology.json", response_model=TopologyResponse)
    async def topology_json(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        topology = build_topology(ctx)
        node_metadata = _load_topology_node_metadata()
        if node_metadata:
            return annotate_topology(topology, node_metadata=node_metadata)
        return topology

    @router.get("/_apx/topology/digest", response_model=TopologyDigestResponse)
    async def topology_digest(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        topology = build_topology(ctx)
        return summarize_topology(
            annotate_topology(
                topology,
                execution=_last_route_payload(ctx, include_tracking_fallback=False),
            )
        )

    @router.get("/_apx/topology/inspect/{node_id:path}", response_model=dict[str, Any])
    async def topology_inspect(node_id: str, request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        details = inspect_node(ctx, node_id)
        if details is None:
            raise HTTPException(status_code=404, detail=f"Node not found: {node_id}")
        return details

    @router.get("/_apx/topology/tracing", response_model=TopologyTracingResponse)
    async def topology_tracing(request: Request) -> Any:
        """Where this agent writes MLflow traces — experiment id/name + workspace deep link."""
        experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip() or None
        host = _workspace_host_from_request(request)
        name = _lookup_experiment_name(experiment_id) if experiment_id else None
        return {
            "experiment_id": experiment_id,
            "experiment_name": name,
            "workspace_host": host,
            "experiment_url": _experiment_url(host, experiment_id),
            "configured": bool(experiment_id),
        }

    @router.post("/_apx/topology/tracing", response_model=TopologyTracingSetResponse)
    async def topology_tracing_set(request: Request, body: TopologyTracingSetRequest) -> Any:
        """Set ``MLFLOW_EXPERIMENT_ID`` (live + .env) so Topology / Chat share one destination."""
        eid = (body.experiment_id or "").strip()
        if not eid:
            return JSONResponse(
                {"ok": False, "error": "experiment_id required"}, status_code=422
            )

        # Validate when the tracking store is reachable; still allow set if lookup fails
        # (local sqlite / egress) so the operator can pin an id ahead of deploy.
        name = _lookup_experiment_name(eid)
        os.environ["MLFLOW_EXPERIMENT_ID"] = eid
        try:
            import mlflow as _mlflow

            _mlflow.set_experiment(experiment_id=eid)
        except Exception:
            logger.debug("mlflow.set_experiment failed for %s", eid, exc_info=True)

        env_path = _find_env_path()
        if env_path is not None:
            _write_env_file(env_path, {"MLFLOW_EXPERIMENT_ID": eid})
            try:
                await _ws_upload_agent_file(request, env_path, env_path.read_text())
            except Exception:
                logger.debug("workspace .env upload skipped", exc_info=True)

        host = _workspace_host_from_request(request)
        return {
            "ok": True,
            "experiment_id": eid,
            "experiment_name": name,
            "experiment_url": _experiment_url(host, eid),
            "restart_hint": (
                "Live for this process. App.yaml / bundle env still wins on next deploy "
                "unless you also update mlflow_experiment_id."
            ),
        }

    @router.get("/_apx/topology/assets/{path:path}", include_in_schema=False)
    async def topology_assets(path: str) -> Any:
        target = _topo_static_root / "assets" / path
        if target.is_file():
            return FileResponse(target)
        raise HTTPException(status_code=404, detail="asset not found")

    # Vendored JS (marked + DOMPurify) for chat markdown rendering. Served
    # locally so the deployed app needs no CDN (offline/private-link safe).
    @router.get("/_apx/vendor/{filename}", include_in_schema=False)
    async def vendor_asset(filename: str) -> Any:
        # Resolve and confirm the result is inside _vendor_root (block path
        # traversal). {filename} is a single path segment, so a traversal
        # attempt also simply fails to match this route — guard is belt-and-braces.
        target = (_vendor_root / filename).resolve()
        if _vendor_root.resolve() not in target.parents or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(target, media_type="application/javascript")

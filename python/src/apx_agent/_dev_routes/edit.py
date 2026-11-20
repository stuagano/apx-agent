"""Dev UI edit routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    EditPreviewRequest,
    EditSaveRequest,
    EditSaveResponse,
    HTMLResponse,
    Request,
    WorkspaceClient,
    _current_root_instructions,
    _edit_panel_inventory,
    _extract_schemas_from_source,
    _find_agent_router_path,
    _instruction_target,
    _load_optimize_eval_rows,
    _optimize_instructions_sync,
    _parse_agent_nodes,
    _render_edit_ui,
    os,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""
    instruction_source_path = ctx.instruction_source_path
    instruction_source = ctx.instruction_source


    @router.get("/_apx/edit")
    async def edit_dev_ui(request: Request) -> HTMLResponse:
        path = _find_agent_router_path()
        ctx: AgentContext | None = request.app.state.agent_context
        live_schemas, live_agents = _edit_panel_inventory(ctx)
        if path and path.exists():
            return HTMLResponse(
                _render_edit_ui(
                    path.read_text(),
                    initial_schemas=live_schemas,
                    initial_agents=live_agents,
                )
            )
        return HTMLResponse(_render_edit_ui("", not_found=True))

    @router.post("/_apx/edit", response_model=EditSaveResponse)
    async def save_agent_router(request: Request, body: EditSaveRequest) -> Any:
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        content: str = body.content
        try:
            compile(content, "agent_router.py", "exec")
        except SyntaxError as e:
            return JSONResponse({"ok": False, "error": f"Syntax error at line {e.lineno}: {e.msg}"})
        path = _find_agent_router_path()
        if not path:
            return JSONResponse({"ok": False, "error": "agent_router.py not found in running process"})
        path.write_text(content)

        # Write back to workspace so the change survives restarts.
        ctx: AgentContext | None = request.app.state.agent_context
        ws_client: WorkspaceClient | None = getattr(request.app.state, "workspace_client", None)
        app_name = ctx.config.name if ctx else None
        restart_required = False
        if app_name and ws_client:
            try:
                app_info = await _asyncio.to_thread(lambda: ws_client.apps.get(app_name))
                ws_source = getattr(app_info, "default_source_code_path", None)
                if ws_source:
                    ws_path = ws_source.rstrip("/") + f"/{path.name}"
                    await _asyncio.to_thread(
                        lambda: ws_client.workspace.upload(ws_path, content.encode(), overwrite=True)
                    )
                    restart_required = True
            except Exception:
                pass

        return {"ok": True, "restart_required": restart_required}

    @router.post("/_apx/edit/preview", response_model=list[dict[str, Any]])
    async def preview_tool_schemas(body: EditPreviewRequest) -> Any:
        return _extract_schemas_from_source(body.source)

    @router.post("/_apx/edit/optimize-instructions")
    async def optimize_instructions(request: Request) -> Any:
        """Propose instructions for a selected node, scored through the full agent.

        Scores candidate instructions against the agent's existing eval dataset +
        judge, bridges through a transient prompt-registry entry, and returns
        the winning text, scores, and patched source without writing agent.py.
        The Edit tab previews the candidate; the existing Save route persists it.
        """
        from fastapi.responses import JSONResponse
        import asyncio as _asyncio

        ctx: AgentContext | None = request.app.state.agent_context
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Expected a JSON object"}, status_code=422)
        node = body.get("node", "agent")
        if not isinstance(node, str) or not node.isidentifier():
            return JSONResponse({"ok": False, "error": "node must be an agent variable name"}, status_code=422)
        if not isinstance(body.get("judge_name"), str):
            return JSONResponse({"ok": False, "error": "judge_name is required"}, status_code=422)
        judge_name = (body.get("judge_name") or "").strip()
        if not judge_name:
            return JSONResponse({"ok": False, "error": "judge_name is required"}, status_code=422)

        rows = _load_optimize_eval_rows()
        if not rows:
            return JSONResponse(
                {"ok": False, "error": "No eval dataset available. Add cases in the Eval tab or an evals.json first."},
                status_code=422,
            )

        from apx_agent._ui_edit import _find_agent_router_path, _find_root_agent_call, _set_agent_instructions

        source = None
        target = ctx.agent if ctx else None
        current_instructions = _current_root_instructions(ctx)
        if "node" in body or "source" in body:
            path = _find_agent_router_path()
            if path is None or not path.exists() or ctx is None:
                return JSONResponse({"ok": False, "error": "A running agent with editable source is required"}, status_code=422)
            source = path.read_text()
            if "source" in body and body["source"] != source:
                return JSONResponse({"ok": False, "error": "Save your editor changes and restart before optimizing."}, status_code=409)
            if path != instruction_source_path or source != instruction_source:
                return JSONResponse({"ok": False, "error": "Agent source changed since startup. Restart before optimizing."}, status_code=409)
            try:
                import ast

                selected = next((entry for entry in _parse_agent_nodes(source) if entry["name"] == node), None)
                if selected is None:
                    raise ValueError(f"Unknown instruction node {node!r}")
                call = _find_root_agent_call(source, node)
                if call is None:
                    raise ValueError(f"Node {node!r} has no editable leaf instructions")
                keyword = next((kw for kw in call.keywords if kw.arg == "instructions"), None)
                if keyword is None or not (isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str) and keyword.value.value):
                    raise ValueError("Optimization requires nonempty literal instructions in the selected declaration")
                current_instructions = selected["instructions"]
                _set_agent_instructions(source, current_instructions, target=node)
                target = _instruction_target(ctx.agent, node, path)
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

        experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip() or None
        try:
            result = await _asyncio.to_thread(
                _optimize_instructions_sync,
                judge_name=judge_name,
                rows=rows,
                agent=ctx.agent if ctx else None,
                model=getattr(ctx.config, "model", "") if ctx else "",
                current_instructions=current_instructions,
                experiment_id=experiment_id,
                reflection_model="databricks:/databricks-claude-sonnet-4-6",
                target=target,
            )
        except Exception as exc:  # noqa: BLE001 — optimize/predict failure → clean 500 (transient prompt already cleaned in finally)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        status = result.pop("status")
        if not result["ok"]:
            return JSONResponse(result, status_code=status)
        result["node"] = node
        if source is not None:
            if path.read_text() != source:
                return JSONResponse({"ok": False, "error": "Agent source changed while optimizing; reload and try again."}, status_code=409)
            result["source"] = _set_agent_instructions(source, result["candidate"], target=node)
        return result

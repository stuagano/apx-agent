"""Dev UI probe routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    HTMLResponse,
    Request,
    _render_probe_ui,
    _run_probe_checks,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/probe", include_in_schema=False)
    async def probe_dev_ui() -> HTMLResponse:
        return HTMLResponse(_render_probe_ui())

    @router.get("/_apx/probe/checks", response_model=dict[str, Any])
    async def probe_checks(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        conversation_store = getattr(request.app.state, "conversation_store", None)
        return await _run_probe_checks(
            ctx,
            headers=dict(request.headers),
            conversation_store=conversation_store,
        )

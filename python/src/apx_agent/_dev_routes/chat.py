"""Dev UI chat routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    HTMLResponse,
    Request,
    Response,
    _render_agent_ui,
    _render_unified_shell,
    standalone_chat_redirect,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/agent", include_in_schema=False)
    async def agent_dev_ui(request: Request) -> HTMLResponse:
        """Unified tabbed shell hosting every /_apx/* page in one iframe."""
        ctx: AgentContext | None = request.app.state.agent_context
        # A typed-entry native host doesn't mount the free-text Eval routes; don't offer the tab.
        typed = getattr(request.app.state, "_apx_typed_entry", False)
        return HTMLResponse(_render_unified_shell(ctx, hidden_tabs=frozenset({"eval"}) if typed else frozenset()))

    @router.get("/_apx/chat", include_in_schema=False)
    async def chat_dev_ui(request: Request) -> Response:
        """Bare chat content — loaded by the unified shell's Chat tab; opened alone it redirects there."""
        if redirect := standalone_chat_redirect(request):
            return redirect
        ctx: AgentContext | None = request.app.state.agent_context
        return HTMLResponse(
            _render_agent_ui(ctx, embed=request.query_params.get("embed") == "1")
        )

"""Dev UI conversations routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    ConversationItemResponse,
    ConversationSummaryResponse,
    JSONResponse,
    Request,
    logger,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/conversations",
                response_model=list[ConversationSummaryResponse])
    async def list_conversations_api(request: Request) -> Any:
        """Return conversations from the conversation store as JSON."""
        store = getattr(request.app.state, "conversation_store", None)
        if store is None:
            return []
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent_id = ctx.config.name if ctx else None
        try:
            paged = store.list_conversations(
                limit=50,
                order="desc",
                sort_by="updated_at",
                agent_id=agent_id,
            )
            # Raw list (not JSONResponse) so ``response_model`` actually
            # validates each row — FastAPI passes a Response through untouched.
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                }
                for c in paged.data
            ]
        except Exception:
            # Surface the failure to the UI (history panel shows an error
            # state) instead of silently rendering an empty list, and log
            # it so `apx-agent run` output points at the real cause.
            logger.exception("listing conversations failed")
            return JSONResponse({"error": "conversation store unavailable"},
                                status_code=503)

    @router.get("/_apx/conversations/{conv_id}/items",
                response_model=list[ConversationItemResponse])
    async def list_conversation_items_api(conv_id: str, request: Request) -> Any:
        """Return items for a conversation as JSON."""
        store = getattr(request.app.state, "conversation_store", None)
        if store is None:
            return []
        try:
            paged = store.list_items(conv_id, limit=200, order="asc")
            # Raw list (not JSONResponse) so ``response_model`` validates rows.
            return [
                {
                    "id": item.id,
                    "type": item.type,
                    "data": item.data.model_dump(exclude_none=True) if item.data else {},
                }
                for item in paged.data
            ]
        except Exception:
            logger.exception("listing items for conversation %s failed", conv_id)
            return JSONResponse({"error": "conversation store unavailable"},
                                status_code=503)

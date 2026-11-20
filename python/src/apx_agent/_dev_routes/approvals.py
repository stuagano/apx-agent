"""Dev UI approvals routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    ApprovalActionResponse,
    ApprovalInfo,
    Request,
    logger,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    # ── Approvals (human-in-the-loop ASK policy) ──────────────────────────

    def _decider(request: Request) -> str | None:
        """Identity of the principal making an approval decision, for the audit
        record. Reads the Apps proxy's forwarded identity headers; ``None`` in
        local dev (no forwarded user) — recorded honestly rather than faked."""
        return (
            request.headers.get("x-forwarded-email")
            or request.headers.get("x-forwarded-user")
            or None
        )

    def _find_approval_store(request: Request) -> Any:
        """Locate the ApprovalStore serving this app, if any.

        Resolution order:

        1. ``app.state.approval_store`` — explicit wiring by the embedding
           application (takes precedence so multi-agent apps can share one
           store).
        2. The root agent's ``before_tool`` hook: a bare
           :class:`~apx_agent._policy.PolicyGate` exposes ``.approvals``;
           a :func:`~apx_agent._guards.compose`-d hook exposes
           ``.callbacks`` which is walked for a gate.

        :param request: The incoming request (used for ``app.state``).
        :returns: The :class:`~apx_agent._policy.ApprovalStore`, or
            ``None`` when no PolicyGate is attached to this agent.
        """
        explicit = getattr(request.app.state, "approval_store", None)
        if explicit is not None:
            return explicit
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent = getattr(ctx, "agent", None) if ctx else None
        hook = getattr(agent, "_before_tool", None)
        candidates = [hook] if hook is not None else []
        # compose() exposes .callbacks — one level of nesting is all the
        # wiring layer produces (compose(guard, gate)), so no recursion.
        candidates.extend(getattr(hook, "callbacks", []) or [])
        for cand in candidates:
            approvals = getattr(cand, "approvals", None)
            if approvals is not None:
                return approvals
        return None

    @router.get("/_apx/approvals", response_model=list[ApprovalInfo])
    async def list_approvals_api(request: Request) -> Any:
        """Return pending approval requests as JSON for the chat UI banner."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return []
        try:
            return [
                {
                    "id": a.id,
                    "tool_name": a.tool_name,
                    "arguments": a.arguments,
                    "reason": a.reason,
                }
                for a in store.list_pending()
            ]
        except Exception:
            logger.exception("listing approvals failed")
            return JSONResponse({"error": "approval store unavailable"},
                                status_code=503)

    @router.post("/_apx/approvals/{approval_id}/approve", response_model=ApprovalActionResponse)
    async def approve_approval_api(approval_id: str, request: Request) -> Any:
        """Grant a pending approval — the agent's retry of the call passes."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return JSONResponse({"error": "no approval store configured"},
                                status_code=404)
        try:
            approval = store.approve(approval_id, decided_by=_decider(request))
        except KeyError:
            return JSONResponse({"error": f"unknown approval {approval_id}"},
                                status_code=404)
        return {
            "id": approval.id, "status": approval.status,
            "decided_by": approval.decided_by, "decided_at": approval.decided_at,
        }

    @router.post("/_apx/approvals/{approval_id}/deny", response_model=ApprovalActionResponse)
    async def deny_approval_api(approval_id: str, request: Request) -> Any:
        """Refuse a pending approval — the agent's retry becomes a hard DENY."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return JSONResponse({"error": "no approval store configured"},
                                status_code=404)
        try:
            approval = store.deny(approval_id, decided_by=_decider(request))
        except KeyError:
            return JSONResponse({"error": f"unknown approval {approval_id}"},
                                status_code=404)
        return {
            "id": approval.id, "status": approval.status,
            "decided_by": approval.decided_by, "decided_at": approval.decided_at,
        }

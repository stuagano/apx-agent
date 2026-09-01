"""Internal AppKit -> Python tool bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from databricks.sdk import WorkspaceClient
from pydantic import BaseModel, ConfigDict, SecretStr

from ._agents import BaseAgent, LlmAgent
from ._apps_authorization import infer_operation_authorization
from ._callbacks import build_callback_handler
from ._compile import CompileContext, _make_langchain_tool
from ._defaults import DatabricksAppsHeaders, _obo_ws_from_headers
from ._inspection import _state_param_name
from ._policy import ApprovalRequired
from ._topology import _iter_child_agents


class _ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: dict[str, Any] = {}


@dataclass(frozen=True)
class _ToolTarget:
    owner: Any | None
    fn: Any | None


def build_appkit_tool_bridge_router() -> APIRouter:
    """Build internal routes used by the generated AppKit host."""
    router = APIRouter()

    @router.post(
        "/_apx/internal/appkit/tools/{tool_name}",
        include_in_schema=False,
    )
    async def invoke_tool(
        request: Request,
        tool_name: str,
        body: _ToolRequest,
    ) -> dict[str, Any]:
        ctx = getattr(request.app.state, "agent_context", None)
        if ctx is None or ctx.agent is None:
            raise HTTPException(status_code=404, detail="Agent protocol not configured")

        target = _find_tool(ctx.agent, tool_name)
        if target.fn is None:
            raise HTTPException(status_code=404, detail=f"Unknown APX tool: {tool_name}")
        if _state_param_name(target.fn) is not None:
            raise HTTPException(
                status_code=400,
                detail=f"APX AppKit bridge cannot execute stateful tool: {tool_name}",
            )

        authorization = infer_operation_authorization(target.fn)
        headers = (
            _headers_from_request(
                request,
                include_token=authorization.execution_identity == "user",
            )
            if authorization.requires_request_context
            else None
        )
        if authorization.execution_identity == "user" and (
            headers is None or headers.token is None
        ):
            raise HTTPException(
                status_code=401,
                detail=f"APX tool {tool_name!r} requires forwarded user identity",
            )
        service_ws = WorkspaceClient()
        user_ws = (
            _obo_ws_from_headers(headers)
            if authorization.execution_identity == "user" and headers is not None
            else None
        )
        lc_tool = _make_langchain_tool(
            target.fn,
            CompileContext(
                service_ws=service_ws,
                user_ws=user_ws,
                model=ctx.config.model,
                headers=headers,
            ),
        )
        handler = build_callback_handler(target.owner)
        config = {"callbacks": [handler]} if handler is not None else None
        try:
            result = await lc_tool.ainvoke(body.args, config=config)
        except ApprovalRequired as exc:
            raise HTTPException(status_code=403, detail="Tool execution is denied") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"result": result}

    return router


def _find_tool(agent: BaseAgent, name: str) -> _ToolTarget:
    if isinstance(agent, LlmAgent):
        for fn in agent._tool_fns:
            if fn.__name__ == name:
                return _ToolTarget(agent, fn)
        return _ToolTarget(None, None)
    for _, child in _iter_child_agents(agent):
        target = _find_tool(child, name)
        if target.fn is not None:
            return target
    for fn in getattr(agent, "_tool_fns", []) or []:
        if fn.__name__ == name:
            return _ToolTarget(agent, fn)
    return _ToolTarget(None, None)


def _headers_from_request(
    request: Request,
    *,
    include_token: bool,
) -> DatabricksAppsHeaders:
    raw_request_id = request.headers.get("X-Request-Id")
    return DatabricksAppsHeaders(
        host=request.headers.get("X-Forwarded-Host"),
        user_name=request.headers.get("X-Forwarded-Preferred-Username"),
        user_id=request.headers.get("X-Forwarded-User"),
        user_email=request.headers.get("X-Forwarded-Email"),
        request_id=UUID(raw_request_id) if raw_request_id else None,
        token=(
            SecretStr(request.headers["X-Forwarded-Access-Token"])
            if include_token and request.headers.get("X-Forwarded-Access-Token")
            else None
        ),
    )

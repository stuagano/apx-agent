"""Internal AppKit -> Python tool bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr

from ._agents import BaseAgent, LlmAgent
from ._apps_authorization import infer_operation_authorization
from ._apps_host_manifest import AppsHostManifest, AppsHostTool
from ._callbacks import build_callback_handler
from ._compile import CompileContext, _make_langchain_tool
from ._defaults import (
    DatabricksAppsHeaders,
    _make_workspace_client,
    _obo_ws_from_headers,
)
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


_REQUEST_CONTEXT_HEADERS = frozenset(
    {
        b"x-forwarded-host",
        b"x-forwarded-preferred-username",
        b"x-forwarded-user",
        b"x-forwarded-email",
        b"x-forwarded-access-token",
        b"x-request-id",
    }
)


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

        manifest_tool = _manifest_tool(request, tool_name)
        _assert_live_tool_compatibility(target.fn, manifest_tool)
        authorization = manifest_tool.annotations
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
        if authorization.execution_identity == "user":
            service_ws = None
            user_ws = _obo_ws_from_headers(headers)
        else:
            service_ws = _make_workspace_client()
            user_ws = None
        lc_tool = _make_langchain_tool(
            target.fn,
            CompileContext(
                service_ws=service_ws,
                user_ws=user_ws,
                model=ctx.config.model,
                headers=headers,
                request=(
                    _bounded_request(
                        request,
                        include_token=authorization.execution_identity == "user",
                    )
                    if authorization.requires_request_context
                    else None
                ),
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


def _manifest_tool(request: Request, name: str) -> AppsHostTool:
    manifest = getattr(request.app.state, "apx_appkit_host_manifest", None)
    if not isinstance(manifest, AppsHostManifest):
        raise HTTPException(
            status_code=503,
            detail="APX AppKit bridge manifest is not configured",
        )
    matches = [tool for tool in manifest.tools if tool.name == name]
    if len(matches) != 1:
        raise HTTPException(
            status_code=409,
            detail=f"APX AppKit manifest does not uniquely identify tool: {name}",
        )
    return matches[0]


def _assert_live_tool_compatibility(fn: Any, manifest_tool: AppsHostTool) -> None:
    live = infer_operation_authorization(fn)
    annotations = manifest_tool.annotations
    if (
        live.execution_identity != annotations.execution_identity
        or live.requires_request_context != annotations.requires_request_context
        or annotations.requires_user_context
        != (annotations.execution_identity == "user")
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "APX AppKit manifest does not match live tool: "
                f"{manifest_tool.name}"
            ),
        )


def _bounded_request(request: Request, *, include_token: bool) -> Request:
    allowed = _REQUEST_CONTEXT_HEADERS
    if not include_token:
        allowed = allowed - {b"x-forwarded-access-token"}
    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.lower() in allowed
    ]
    return Request(
        {
            "type": "http",
            "asgi": request.scope.get("asgi", {"version": "3.0"}),
            "http_version": request.scope.get("http_version", "1.1"),
            "method": request.method,
            "scheme": request.scope.get("scheme", "http"),
            "path": request.scope.get("path", ""),
            "raw_path": request.scope.get("raw_path", b""),
            "query_string": request.scope.get("query_string", b""),
            "root_path": request.scope.get("root_path", ""),
            "headers": headers,
            "client": request.scope.get("client"),
            "server": request.scope.get("server"),
            "app": request.app,
        }
    )


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

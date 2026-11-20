"""Dev UI replay routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    ReplayLlmRequest,
    ReplayToolRequest,
    Request,
    _json,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""
    api_prefix = ctx.api_prefix


    @router.post("/_apx/replay/tool", include_in_schema=False)
    async def replay_tool(request: Request, body: ReplayToolRequest) -> Any:
        """Re-invoke a registered tool with arbitrary args. Used by the
        trace detail view to debug-iterate without restarting a conversation.

        Kept ``include_in_schema=False`` (#281): executes an arbitrary tool with
        forwarded OBO creds — the body is typed/validated but never advertised."""
        from fastapi.responses import JSONResponse
        import time as _time
        from httpx import ASGITransport, AsyncClient

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        tool_name = body.tool_name
        args = body.args
        if tool_name not in ctx._tool_map:
            return JSONResponse({"ok": False, "error": f"Tool '{tool_name}' not found"}, status_code=404)

        # Forward OBO headers so workspace-scoped tools work the same way the
        # runner invokes them. Not a remote hop (#1050): this is an in-process
        # ASGI loopback to this app's own /tools route, which never crosses the
        # Apps ingress — so it passes the caller's headers through verbatim
        # rather than using remote_hop_headers' synthesized Bearer.
        obo_headers = {
            "Authorization": request.headers.get("Authorization", ""),
            "X-Forwarded-Access-Token": request.headers.get("X-Forwarded-Access-Token", ""),
            "X-Forwarded-Host": request.headers.get("X-Forwarded-Host", ""),
        }
        t0 = _time.monotonic()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=request.app),
                base_url="http://internal",
            ) as client:
                resp = await client.post(
                    f"{api_prefix}/tools/{tool_name}",
                    json=args,
                    headers=obo_headers,
                )
            elapsed = int((_time.monotonic() - t0) * 1000)
            if resp.status_code >= 400:
                return JSONResponse({
                    "ok": False,
                    "error": f"Tool returned {resp.status_code}: {resp.text}",
                    "duration_ms": elapsed,
                }, status_code=200)
            result = resp.json()
            output = result if isinstance(result, str) else _json.dumps(result)
            return JSONResponse({"ok": True, "output": output, "duration_ms": elapsed})
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    @router.post("/_apx/replay/llm", include_in_schema=False)
    async def replay_llm(request: Request, body: ReplayLlmRequest) -> Any:
        """Re-invoke the configured model with edited messages. Returns
        the model's output text — useful for "what if I had asked X instead?".

        Kept ``include_in_schema=False`` (#281): invokes the LLM directly — the
        body is typed/validated but never advertised."""
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        messages = body.messages
        model = body.model or getattr(ctx.config, "model", "")
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        t0 = _time.monotonic()
        try:
            client = AsyncDatabricksOpenAI()
            resp = await client.chat.completions.create(
                model=model, messages=messages,  # type: ignore[arg-type]  # validated dicts; SDK wants ChatCompletionMessageParam
            )
            elapsed = int((_time.monotonic() - t0) * 1000)
            choices = getattr(resp, "choices", None) or []
            output = ""
            if choices:
                msg = getattr(choices[0], "message", None)
                output = (getattr(msg, "content", None) or "") if msg else ""
            return JSONResponse({"ok": True, "output": output, "duration_ms": elapsed, "model": model})
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

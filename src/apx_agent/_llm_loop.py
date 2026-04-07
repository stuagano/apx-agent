"""LLM loop — tool dispatch, context trimming, and /invocations handler."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from ._models import (
    AgentContext,
    AgentTool,
    AfterToolHook,
    BeforeToolHook,
    InvocationRequest,
    InvocationResponse,
    Message,
    OutputItem,
    OutputTextContent,
)

logger = logging.getLogger(__name__)


def _build_tool_schemas(tools: list[AgentTool]) -> list[dict[str, Any]]:
    """Convert AgentTools to OpenAI-compatible function calling format for Mosaic AI Model Serving."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


async def _dispatch_tool_call(
    request: Request,
    tool_call: dict[str, Any],
    ctx: AgentContext,
) -> Any:
    """Dispatch a single tool call — local via ASGI, sub-agent via HTTP."""
    import json as _json

    from httpx import ASGITransport, AsyncClient

    fn_name = tool_call["function"]["name"]
    try:
        arguments = _json.loads(tool_call["function"].get("arguments", "{}"))
    except Exception:
        arguments = {}

    tool = ctx.get_tool(fn_name)
    obo_header = {
        "Authorization": request.headers.get("Authorization", ""),
        "X-Forwarded-Access-Token": request.headers.get("X-Forwarded-Access-Token", ""),
        "X-Forwarded-Host": request.headers.get("X-Forwarded-Host", ""),
    }

    if tool and tool.sub_agent_url:
        # Sub-agent: POST to its /invocations with the message
        message = arguments.get("message", _json.dumps(arguments))
        async with AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{tool.sub_agent_url}/invocations",
                json={"input": [{"role": "user", "content": message}]},
                headers=obo_header,
            )
        if response.status_code >= 400:
            return f"Sub-agent error ({response.status_code}): {response.text}"
        data = response.json()
        try:
            return data["output"][0]["content"][0]["text"]
        except (KeyError, IndexError):
            return str(data)
    else:
        # Local tool: dispatch via ASGI to {api_prefix}/tools/<fn>
        api_prefix = ctx.config.api_prefix

        async with AsyncClient(
            transport=ASGITransport(app=request.app),
            base_url="http://internal",
        ) as client:
            response = await client.post(
                f"{api_prefix}/tools/{fn_name}",
                json=arguments,
                headers=obo_header,
            )
        if response.status_code >= 400:
            return f"Tool error ({response.status_code}): {response.text}"
        result = response.json()
        return result if isinstance(result, str) else str(result)


# ---------------------------------------------------------------------------
# Invocations handler
# ---------------------------------------------------------------------------


async def _maybe_trim_context(
    messages: list[dict[str, Any]],
    max_tokens: int,
    client: Any,
    endpoint_url: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Summarize the middle of the message history when the token budget is exceeded."""
    estimated = sum(len(str(m.get("content") or "")) for m in messages) // 4
    if estimated <= max_tokens:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= 2:
        return messages

    tail = non_system[-2:]
    middle = non_system[:-2]

    import json as _json

    summary_prompt = [{
        "role": "user",
        "content": (
            "Summarize the following conversation excerpt in 2–3 sentences, "
            "preserving key facts, decisions, and context:\n\n"
            + "\n".join(f"{m['role'].upper()}: {m.get('content', '')}" for m in middle)
        ),
    }]
    try:
        resp = await client.post(
            endpoint_url,
            json={"messages": summary_prompt},
            headers=auth_headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"]
        summary_msg: dict[str, Any] = {
            "role": "assistant",
            "content": f"[Earlier conversation summary: {summary}]",
        }
        return system_msgs + [summary_msg] + tail
    except Exception:
        logger.warning("Context trimming failed — continuing with original message list")
        return messages


async def _run_llm_loop(
    input_messages: list[Message],
    request: Request,
    tools: list[AgentTool] | None = None,
    instructions: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_iterations: int | None = None,
    before_tool: BeforeToolHook | None = None,
    after_tool: AfterToolHook | None = None,
    context_window_tokens: int | None = None,
) -> str:
    """Run the Mosaic AI model serving LLM loop and return the final response text."""
    import json as _json
    import time as _time

    from databricks.sdk import WorkspaceClient
    from httpx import AsyncClient

    ctx: AgentContext = request.app.state.agent_context
    ws: WorkspaceClient = request.app.state.workspace_client

    custom_inputs: dict[str, Any] = getattr(request.state, "custom_inputs", {})
    system_prompt = custom_inputs.get("instructions") or instructions or ctx.config.instructions
    effective_temp = temperature if temperature is not None else ctx.config.temperature
    effective_max_tokens = max_tokens if max_tokens is not None else ctx.config.max_tokens
    effective_max_iter = max_iterations if max_iterations is not None else ctx.config.max_iterations

    base_messages: list[dict[str, Any]] = []
    if system_prompt:
        base_messages.append({"role": "system", "content": system_prompt})
    base_messages += [
        {"role": m.role, "content": m.content, **({"name": m.name} if m.name else {})}
        for m in input_messages
    ]
    messages = base_messages

    auth_headers = ws.config.authenticate()
    endpoint_url = f"{ws.config.host.rstrip('/')}/serving-endpoints/{ctx.config.model}/invocations"

    model_params: dict[str, Any] = {}
    if effective_temp is not None:
        model_params["temperature"] = effective_temp
    if effective_max_tokens is not None:
        model_params["max_tokens"] = effective_max_tokens

    try:
        import mlflow as _mlflow

        _mlflow_available = True
    except ImportError:
        _mlflow_available = False

    async with AsyncClient() as client:
        for _ in range(effective_max_iter):
            effective_tools = tools if tools is not None else ctx.tools
            tool_schemas = _build_tool_schemas(effective_tools)

            if context_window_tokens is not None:
                messages = await _maybe_trim_context(messages, context_window_tokens, client, endpoint_url, auth_headers)

            llm_span = (
                _mlflow.start_span_no_context(
                    name="llm",
                    span_type="LLM",
                    attributes={"model": ctx.config.model, "input_messages": _json.dumps(messages)},
                )
                if _mlflow_available else None
            )
            try:
                payload: dict[str, Any] = {"messages": messages, **model_params}
                if tool_schemas:
                    payload["tools"] = tool_schemas
                response = await client.post(
                    endpoint_url,
                    json=payload,
                    headers=auth_headers,
                    timeout=60.0,
                )
                response.raise_for_status()
                data = response.json()

                choice = data["choices"][0]
                assistant_msg = choice["message"]
                finish_reason = choice.get("finish_reason") or choice.get("finishReason")
                messages.append(assistant_msg)

                if llm_span is not None:
                    llm_span.set_attribute("output_message", _json.dumps(assistant_msg))
                    llm_span.set_attribute("finish_reason", finish_reason or "")
            finally:
                if llm_span is not None:
                    llm_span.end()

            if finish_reason == "tool_calls":
                for tool_call in assistant_msg.get("tool_calls", []):
                    fn_name = tool_call["function"]["name"]
                    try:
                        arguments = _json.loads(tool_call["function"].get("arguments", "{}"))
                    except Exception:
                        arguments = {}

                    if before_tool is not None:
                        if inspect.iscoroutinefunction(before_tool):
                            await before_tool(fn_name, arguments)
                        else:
                            before_tool(fn_name, arguments)

                    tool_span = (
                        _mlflow.start_span_no_context(
                            name=fn_name,
                            span_type="TOOL",
                            attributes={"tool_call_id": tool_call.get("id", "")},
                        )
                        if _mlflow_available else None
                    )
                    result: Any = ""
                    _t0 = _time.monotonic()
                    try:
                        result = await _dispatch_tool_call(request, tool_call, ctx)
                        if tool_span is not None:
                            tool_span.set_attribute(
                                "result", result if isinstance(result, str) else _json.dumps(result)
                            )
                    finally:
                        if tool_span is not None:
                            tool_span.end()

                    if after_tool is not None:
                        if inspect.iscoroutinefunction(after_tool):
                            await after_tool(fn_name, arguments, result)
                        else:
                            after_tool(fn_name, arguments, result)

                    try:
                        if not hasattr(request.state, "tool_trace"):
                            request.state.tool_trace = []
                        request.state.tool_trace.append({
                            "name": fn_name,
                            "args": arguments,
                            "result": result if isinstance(result, (str, dict, list, int, float, bool)) else str(result),
                            "ms": round((_time.monotonic() - _t0) * 1000),
                        })
                    except Exception:
                        pass

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result if isinstance(result, str) else _json.dumps(result),
                    })
            else:
                return assistant_msg.get("content") or ""

    return next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "assistant"),
        "",
    )


async def _handle_invocation(
    request: Request,
    body: InvocationRequest,
) -> InvocationResponse | StreamingResponse:
    """Handle /invocations — returns JSON or SSE depending on body.stream."""
    import json as _json

    ctx: AgentContext | None = request.app.state.agent_context
    if ctx is None:
        raise HTTPException(status_code=503, detail="Agent protocol not configured")

    try:
        import mlflow

        import json as _json_span

        root_span = mlflow.start_span_no_context(
            name=ctx.config.name,
            span_type="CHAIN",
            attributes={
                "agent": ctx.config.name,
                "model": ctx.config.model,
                "custom_inputs": _json_span.dumps(body.custom_inputs) if body.custom_inputs else "{}",
            },
        )
    except ImportError:
        root_span = None

    request.state.custom_inputs = body.custom_inputs
    request.state.custom_outputs = {}
    messages = body.messages()

    if body.stream:
        async def _sse_generator() -> AsyncGenerator[str, None]:
            item_id = "msg_001"
            yield f"event: response.output_item.start\ndata: {_json.dumps({'item_id': item_id})}\n\n"
            full_text = ""
            try:
                async for chunk in ctx.agent.stream(messages, request):
                    full_text += chunk
                    yield f"event: output_text.delta\ndata: {_json.dumps({'item_id': item_id, 'text': chunk})}\n\n"
                output_item = OutputItem(content=[OutputTextContent(text=full_text)])
                yield f"event: response.output_item.done\ndata: {_json.dumps({'item_id': item_id, 'output': output_item.model_dump()})}\n\n"
                tool_trace = getattr(request.state, "tool_trace", [])
                if tool_trace:
                    yield f"event: tool.trace\ndata: {_json.dumps(tool_trace)}\n\n"
                    request.state.tool_trace = []
                custom_out = getattr(request.state, "custom_outputs", {})
                if custom_out:
                    yield f"event: custom_outputs\ndata: {_json.dumps(custom_out)}\n\n"
            except Exception as exc:
                error_payload = {"item_id": item_id, "error": str(exc)}
                yield f"event: error\ndata: {_json.dumps(error_payload)}\n\n"
                logger.exception("Error during streaming invocation")
            finally:
                if root_span is not None:
                    root_span.set_attribute("output", full_text)
                    root_span.end()

        return StreamingResponse(_sse_generator(), media_type="text/event-stream")

    text = ""
    try:
        text = await ctx.agent.run(messages, request)
        custom_out = getattr(request.state, "custom_outputs", {})
        return InvocationResponse(
            output=[OutputItem(content=[OutputTextContent(text=text)])],
            custom_outputs=custom_out,
        )
    finally:
        if root_span is not None:
            root_span.set_attribute("output", text)
            root_span.end()

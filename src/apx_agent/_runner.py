"""Runner adapter — bridges apx-agent tools to OpenAI Agents SDK Runner.run().

This module provides ``run_via_sdk()`` as a drop-in replacement for
``_run_llm_loop()``. It converts apx-agent's typed tool functions into
OpenAI Agents SDK ``FunctionTool`` instances and delegates the LLM loop
to ``Runner.run()`` via ``DatabricksOpenAI``.

Feature-flagged: set ``USE_RUNNER=true`` to use this path. When unset
or ``false``, the legacy ``_run_llm_loop()`` is used.

Tools are dispatched through the existing FastAPI ASGI routes so that
``Dependencies.*`` injection (OBO auth, WorkspaceClient, etc.) continues
to work — the SDK sees each tool as an opaque async function.
"""

from __future__ import annotations

import json as _json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Request

from ._models import AgentContext, AgentTool, Message

logger = logging.getLogger(__name__)

USE_RUNNER = os.environ.get("USE_RUNNER", "").lower() in ("true", "1", "yes")


def is_runner_enabled() -> bool:
    """Check whether the SDK runner path is active."""
    return USE_RUNNER


async def run_via_sdk(
    input_messages: list[Message],
    request: Request,
    tools: list[AgentTool] | None = None,
    instructions: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_iterations: int | None = None,
    **kwargs: Any,
) -> str:
    """Run the agent loop using OpenAI Agents SDK + DatabricksOpenAI.

    This is a drop-in replacement for ``_run_llm_loop()`` with the same
    signature (minus hooks that don't map to the SDK yet).
    """
    from agents import Agent as OAIAgent
    from agents import Runner, FunctionTool
    from agents.run_config import RunConfig
    from databricks_openai import AsyncDatabricksOpenAI
    from agents import set_default_openai_client, set_default_openai_api

    ctx: AgentContext = request.app.state.agent_context

    # Configure the SDK to use DatabricksOpenAI
    client = AsyncDatabricksOpenAI()
    set_default_openai_client(client)
    set_default_openai_api("chat_completions")

    effective_tools = tools if tools is not None else ctx.tools

    # Convert apx-agent tools to SDK FunctionTool instances
    function_tools = [
        _to_function_tool(t, request, ctx)
        for t in effective_tools
        if not t.sub_agent_url
    ]

    # Convert sub-agent tools to SDK FunctionTool instances
    # These call DatabricksOpenAI.responses.create(model="apps/<name>") for OBO
    sub_agent_tools = [
        _to_sub_agent_tool(t, client)
        for t in effective_tools
        if t.sub_agent_url
    ]

    effective_model = ctx.config.model
    effective_instructions = instructions or ctx.config.instructions
    effective_max_iter = max_iterations or ctx.config.max_iterations

    oai_agent = OAIAgent(
        name=ctx.config.name,
        model=effective_model,
        instructions=effective_instructions or "You are a helpful assistant.",
        tools=function_tools + sub_agent_tools,
    )

    # Convert input messages to the format Runner expects
    sdk_input = [
        {"role": m.role, "content": m.content}
        for m in input_messages
    ]

    result = await Runner.run(
        oai_agent,
        sdk_input,
        max_turns=effective_max_iter,
    )

    # Extract final output text
    if result.final_output is not None:
        return str(result.final_output)

    # Fallback: get the last assistant message from new_items
    for item in reversed(result.new_items):
        if hasattr(item, 'text'):
            return item.text
        if hasattr(item, 'content'):
            return str(item.content)

    return ""


async def stream_via_sdk(
    input_messages: list[Message],
    request: Request,
    tools: list[AgentTool] | None = None,
    instructions: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_iterations: int | None = None,
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
    """Stream the agent loop using OpenAI Agents SDK + DatabricksOpenAI.

    Yields text chunks as the LLM produces them — real token streaming,
    not run-to-completion-then-chunk.
    """
    from agents import Agent as OAIAgent
    from agents import Runner
    from databricks_openai import AsyncDatabricksOpenAI
    from agents import set_default_openai_client, set_default_openai_api

    ctx: AgentContext = request.app.state.agent_context

    client = AsyncDatabricksOpenAI()
    set_default_openai_client(client)
    set_default_openai_api("chat_completions")

    effective_tools = tools if tools is not None else ctx.tools

    function_tools = [
        _to_function_tool(t, request, ctx)
        for t in effective_tools
        if not t.sub_agent_url
    ]
    sub_agent_tools = [
        _to_sub_agent_tool(t, client)
        for t in effective_tools
        if t.sub_agent_url
    ]

    effective_model = ctx.config.model
    effective_instructions = instructions or ctx.config.instructions
    effective_max_iter = max_iterations or ctx.config.max_iterations

    oai_agent = OAIAgent(
        name=ctx.config.name,
        model=effective_model,
        instructions=effective_instructions or "You are a helpful assistant.",
        tools=function_tools + sub_agent_tools,
    )

    sdk_input = [
        {"role": m.role, "content": m.content}
        for m in input_messages
    ]

    result = Runner.run_streamed(
        oai_agent,
        sdk_input,
        max_turns=effective_max_iter,
    )

    async for event in result.stream_events():
        # RawResponsesStreamEvent contains the raw model output deltas
        if hasattr(event, 'data') and hasattr(event.data, 'delta'):
            delta = event.data.delta
            if isinstance(delta, str) and delta:
                yield delta
        # Also handle OutputTextDelta events from the Responses API format
        elif hasattr(event, 'type') and 'output_text' in str(getattr(event, 'type', '')):
            if hasattr(event, 'delta') and isinstance(event.delta, str):
                yield event.delta


def _to_function_tool(
    tool: AgentTool,
    request: Request,
    ctx: AgentContext,
) -> Any:
    """Wrap an apx-agent local tool as an OpenAI Agents SDK FunctionTool.

    Uses ``FunctionTool`` directly (not the ``@function_tool`` decorator)
    to control the JSON schema exactly. Tool calls dispatch through the
    existing FastAPI ASGI route so Dependencies.* injection works.
    """
    from agents.tool import FunctionTool
    from httpx import ASGITransport, AsyncClient

    api_prefix = ctx.config.api_prefix

    async def _on_invoke(ctx_sdk: Any, args_json: str) -> str:
        try:
            arguments = _json.loads(args_json) if args_json else {}
            obo_headers = {
                "Authorization": request.headers.get("Authorization", ""),
                "X-Forwarded-Access-Token": request.headers.get("X-Forwarded-Access-Token", ""),
                "X-Forwarded-Host": request.headers.get("X-Forwarded-Host", ""),
            }
            async with AsyncClient(
                transport=ASGITransport(app=request.app),
                base_url="http://internal",
            ) as client:
                response = await client.post(
                    f"{api_prefix}/tools/{tool.name}",
                    json=arguments,
                    headers=obo_headers,
                )
            if response.status_code >= 400:
                return f"Tool error ({response.status_code}): {response.text}"
            result = response.json()
            return result if isinstance(result, str) else _json.dumps(result)
        except Exception as e:
            # Return errors as text so the LLM can reason about them
            # instead of crashing the agent loop
            return f"Tool error: {e}"

    # Build strict JSON schema from apx-agent's tool schema
    params_schema = _to_strict_schema(tool.input_schema)

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=params_schema,
        on_invoke_tool=_on_invoke,
        strict_json_schema=True,
    )


def _to_sub_agent_tool(
    tool: AgentTool,
    client: Any,
) -> Any:
    """Wrap a sub-agent tool to call via DatabricksOpenAI Responses API.

    Uses ``model="apps/<app-name>"`` for automatic OBO token forwarding
    through the Supervisor gateway, instead of raw HTTP POST.
    """
    from agents.tool import FunctionTool

    app_name = _url_to_app_name(tool.sub_agent_url or "")

    async def _on_invoke(ctx_sdk: Any, args_json: str) -> str:
        try:
            arguments = _json.loads(args_json) if args_json else {}
            message = arguments.get("message", _json.dumps(arguments))

            if app_name:
                try:
                    response = await client.responses.create(
                        model=f"apps/{app_name}",
                        input=[{"type": "message", "role": "user", "content": message}],
                    )
                    return response.output_text
                except Exception as e:
                    logger.warning(
                        "DatabricksOpenAI call to apps/%s failed (%s), falling back to direct HTTP",
                        app_name, e,
                    )

            # Fallback: direct HTTP POST
            from httpx import AsyncClient as HttpxClient

            async with HttpxClient(timeout=120.0) as http_client:
                resp = await http_client.post(
                    f"{(tool.sub_agent_url or '').rstrip('/')}/responses",
                    json={"input": [{"role": "user", "content": message}]},
                )
            if resp.status_code >= 400:
                return f"Sub-agent error ({resp.status_code}): {resp.text}"
            data = resp.json()
            try:
                return data["output"][0]["content"][0]["text"]
            except (KeyError, IndexError):
                return _json.dumps(data)
        except Exception as e:
            return f"Sub-agent error: {e}"

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send to the agent"},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        on_invoke_tool=_on_invoke,
        strict_json_schema=True,
    )


def _to_strict_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Convert an apx-agent tool input schema to OpenAI strict JSON schema format.

    Ensures ``additionalProperties: false`` is set on all object types,
    which the OpenAI Agents SDK requires for strict mode.
    """
    if not schema:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

    result = dict(schema)
    if result.get("type") == "object":
        result.setdefault("additionalProperties", False)
        result.setdefault("required", list(result.get("properties", {}).keys()))
        # Recurse into nested object properties
        if "properties" in result:
            result["properties"] = {
                k: _to_strict_schema(v) if isinstance(v, dict) and v.get("type") == "object" else v
                for k, v in result["properties"].items()
            }
    return result


def _url_to_app_name(url: str) -> str | None:
    """Extract Databricks App name from URL.

    Pattern: https://<app-name>-<workspace-id>.cloud.databricksapps.com
    or:      https://<app-name>.workspace.databricksapps.com
    """
    if not url or "databricksapps.com" not in url:
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # App name is everything before the first dash followed by digits
        # e.g., "data-triage-agent-7474657313075170" → "data-triage-agent"
        parts = host.split(".")
        if parts:
            name_with_id = parts[0]
            # Strip trailing workspace ID (long numeric suffix)
            segments = name_with_id.split("-")
            # Find where the numeric workspace ID starts
            for i in range(len(segments) - 1, 0, -1):
                if segments[i].isdigit() and len(segments[i]) > 8:
                    return "-".join(segments[:i])
            return name_with_id
    except Exception:
        pass
    return None

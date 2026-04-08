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


def _to_function_tool(
    tool: AgentTool,
    request: Request,
    ctx: AgentContext,
) -> Any:
    """Wrap an apx-agent local tool as an OpenAI Agents SDK FunctionTool.

    Dispatch goes through the existing FastAPI ASGI route so that all
    Dependencies.* injection (OBO token, WorkspaceClient, etc.) works.
    """
    from agents import function_tool
    from httpx import ASGITransport, AsyncClient

    api_prefix = ctx.config.api_prefix

    async def _invoke(**kwargs: Any) -> str:
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
                json=kwargs,
                headers=obo_headers,
            )
        if response.status_code >= 400:
            return f"Tool error ({response.status_code}): {response.text}"
        result = response.json()
        return result if isinstance(result, str) else _json.dumps(result)

    # Apply the decorator with name/description overrides
    decorated = function_tool(
        name_override=tool.name,
        description_override=tool.description,
    )(_invoke)

    return decorated


def _to_sub_agent_tool(
    tool: AgentTool,
    client: Any,
) -> Any:
    """Wrap a sub-agent tool to call via DatabricksOpenAI Responses API.

    Uses ``model="apps/<app-name>"`` for automatic OBO token forwarding
    through the Supervisor gateway, instead of raw HTTP POST.
    """
    from agents import function_tool

    # Try to derive app name from URL: https://<app-name>.workspace.databricksapps.com
    app_name = _url_to_app_name(tool.sub_agent_url or "")

    async def _invoke(message: str) -> str:
        if app_name:
            # Use DatabricksOpenAI for proper OBO auth routing
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

        # Fallback: direct HTTP POST (existing behavior)
        from httpx import AsyncClient as HttpxClient

        async with HttpxClient(timeout=120.0) as http_client:
            resp = await http_client.post(
                f"{(tool.sub_agent_url or '').rstrip('/')}/invocations",
                json={"input": [{"role": "user", "content": message}]},
            )
        if resp.status_code >= 400:
            return f"Sub-agent error ({resp.status_code}): {resp.text}"
        data = resp.json()
        try:
            return data["output"][0]["content"][0]["text"]
        except (KeyError, IndexError):
            return _json.dumps(data)

    decorated = function_tool(
        name_override=tool.name,
        description_override=tool.description,
    )(_invoke)

    return decorated


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

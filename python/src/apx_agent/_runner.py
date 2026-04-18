"""Runner — bridges apx-agent tools to OpenAI Agents SDK Runner.run().

Converts apx-agent's typed tool functions into OpenAI Agents SDK
``FunctionTool`` instances and delegates the LLM loop to ``Runner.run()``
via ``DatabricksOpenAI``.

Tools are dispatched through the existing FastAPI ASGI routes so that
``Dependencies.*`` injection (OBO auth, WorkspaceClient, etc.) continues
to work — the SDK sees each tool as an opaque async function.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Request

from ._models import AgentContext, AgentTool, Message

logger = logging.getLogger(__name__)


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

    # Deduplicate by name — ctx.tools may contain duplicates from multiple agents
    seen: set[str] = set()
    deduped: list[AgentTool] = []
    for t in effective_tools:
        if t.name not in seen:
            seen.add(t.name)
            deduped.append(t)
    effective_tools = deduped

    # Convert apx-agent tools to SDK FunctionTool instances
    function_tools = [
        _to_function_tool(t, request, ctx)
        for t in effective_tools
        if not t.sub_agent_url
    ]

    # Convert sub-agent tools to SDK FunctionTool instances
    # These call DatabricksOpenAI.responses.create(model="apps/<name>") for OBO
    sub_agent_tools = [
        _to_sub_agent_tool(t, client, request)
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

    # Deduplicate
    seen: set[str] = set()
    deduped: list[AgentTool] = []
    for t in effective_tools:
        if t.name not in seen:
            seen.add(t.name)
            deduped.append(t)
    effective_tools = deduped

    function_tools = [
        _to_function_tool(t, request, ctx)
        for t in effective_tools
        if not t.sub_agent_url
    ]
    sub_agent_tools = [
        _to_sub_agent_tool(t, client, request)
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
        import time as _time

        arguments = _json.loads(args_json) if args_json else {}
        t0 = _time.monotonic()
        result_text = ""
        try:
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
                result_text = f"Tool error ({response.status_code}): {response.text}"
            else:
                result = response.json()
                result_text = result if isinstance(result, str) else _json.dumps(result)
        except Exception as e:
            result_text = f"Tool error: {e}"

        # Record trace for dev UI
        elapsed = int((_time.monotonic() - t0) * 1000)
        if hasattr(request.state, "tool_trace"):
            request.state.tool_trace.append({
                "name": tool.name,
                "args": arguments,
                "result": result_text[:500] if len(result_text) > 500 else result_text,
                "ms": elapsed,
            })
        else:
            request.state.tool_trace = [{
                "name": tool.name,
                "args": arguments,
                "result": result_text[:500] if len(result_text) > 500 else result_text,
                "ms": elapsed,
            }]

        return result_text

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
    request: Request | None = None,
) -> Any:
    """Wrap a sub-agent tool to call via DatabricksOpenAI Responses API.

    Uses ``model="apps/<app-name>"`` for automatic OBO token forwarding
    through the Supervisor gateway, instead of raw HTTP POST.
    Falls back to direct HTTP with OBO headers from the request.
    """
    import time as _time

    from agents.tool import FunctionTool

    app_name = _url_to_app_name(tool.sub_agent_url or "")

    async def _on_invoke(ctx_sdk: Any, args_json: str) -> str:
        t0 = _time.monotonic()
        result_text = ""
        try:
            arguments = _json.loads(args_json) if args_json else {}
            message = arguments.get("message", _json.dumps(arguments))

            if app_name:
                try:
                    # Use EasyInputMessage form (no "type": "message") so string
                    # content survives. With type="message" the Responses API
                    # expects content as a list of InputContent parts and drops
                    # a plain string.
                    response = await client.responses.create(
                        model=f"apps/{app_name}",
                        input=[{"role": "user", "content": message}],
                    )
                    result_text = response.output_text
                    return result_text
                except Exception as e:
                    logger.warning(
                        "DatabricksOpenAI call to apps/%s failed (%s), falling back to direct HTTP",
                        app_name, e,
                    )

            # Fallback: direct HTTP POST with OBO token
            # Databricks Apps accept OAuth tokens, not PATs.
            # The user's OBO token is in X-Forwarded-Access-Token (injected by Apps proxy).
            # Use it as Authorization for the sub-agent call.
            from httpx import AsyncClient as HttpxClient

            obo_token = ""
            if request:
                # Prefer the user's OBO token (works for app-to-app calls)
                obo_token = (
                    request.headers.get("X-Forwarded-Access-Token", "")
                    or request.headers.get("Authorization", "").replace("Bearer ", "")
                )
            # Fallback to workspace client auth
            if not obo_token:
                try:
                    from databricks.sdk import WorkspaceClient
                    ws_headers = WorkspaceClient().config.authenticate()
                    obo_token = ws_headers.get("Authorization", "").replace("Bearer ", "")
                except Exception:
                    pass

            obo_headers: dict[str, str] = {
                "Authorization": f"Bearer {obo_token}" if obo_token else "",
            }

            async with HttpxClient(timeout=120.0) as http_client:
                resp = await http_client.post(
                    f"{(tool.sub_agent_url or '').rstrip('/')}/responses",
                    json={"input": [{"role": "user", "content": message}]},
                    headers=obo_headers,
                )
            if resp.status_code >= 400:
                result_text = f"Sub-agent error ({resp.status_code}): {resp.text}"
                return result_text
            data = resp.json()
            try:
                result_text = data.get("output_text", "") or data["output"][0]["content"][0]["text"]
            except (KeyError, IndexError):
                result_text = _json.dumps(data)
            return result_text
        except Exception as e:
            result_text = f"Sub-agent error: {e}"
            return result_text
        finally:
            # Record trace for dev UI
            elapsed = int((_time.monotonic() - t0) * 1000)
            if request and hasattr(request, 'state'):
                trace_entry = {
                    "name": f"🔗 {tool.name}" if app_name else tool.name,
                    "args": {"message": message[:200]} if 'message' in dir() else {},
                    "result": result_text[:500] if result_text and len(result_text) > 500 else (result_text or ""),
                    "ms": elapsed,
                }
                if hasattr(request.state, "tool_trace"):
                    request.state.tool_trace.append(trace_entry)
                else:
                    request.state.tool_trace = [trace_entry]

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

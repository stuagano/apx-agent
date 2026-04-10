"""Agent types — BaseAgent and all composition patterns."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ._models import (
    AgentContext,
    AgentTool,
    AfterToolHook,
    BeforeToolHook,
    InputGuardrailFn,
    Message,
    OutputGuardrailFn,
    _ToolFn,
)
from ._inspection import (
    _inspect_tool_fn,
    _make_input_model,
    _make_route_handler,
    _schema_for_model,
    _schema_for_return,
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """Abstract base for all agent types.

    Subclass to create custom orchestration patterns, or use the built-in
    ``LlmAgent`` (alias: ``Agent``), ``SequentialAgent``, and ``ParallelAgent``.
    """

    async def run(self, messages: list[Message], request: Request) -> str:
        """Run and return the final text response."""
        raise NotImplementedError

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        """Yield text chunks as the agent produces them.

        The default implementation runs to completion and yields the result
        as a single chunk. Override for true token streaming.
        """
        yield await self.run(messages, request)

    def get_tool_routers(self) -> list[APIRouter]:
        """Return FastAPI routers for this agent's tool endpoints."""
        return []

    def collect_tools(self) -> list[AgentTool]:
        """Return AgentTool descriptors for all local tools in this agent tree."""
        return []

    async def fetch_remote_tools(self) -> list[AgentTool]:
        """Fetch AgentTool descriptors from remote sub-agents (A2A)."""
        return []


class LlmAgent(BaseAgent):
    """LLM-powered agent with tool calling via Mosaic AI Model Serving.

    Typed tool functions are registered at construction time. Parameters typed
    as ``Dependencies.*`` are injected by FastAPI and excluded from the schema;
    all other typed parameters become tool inputs derived from their type hints.

    ``instructions`` sets a system prompt prepended to every conversation.
    When omitted, falls back to ``instructions`` in ``[tool.apx.agent]`` in
    pyproject.toml. Use the constructor param to override per-agent within a
    ``SequentialAgent`` or ``ParallelAgent`` composition.

    Example::

        def query_genie(question: str, space_id: str, ws: Dependencies.UserClient) -> str:
            \"\"\"Answer a question using a Genie Space.\"\"\"
            return ws.genie.ask(space_id=space_id, question=question).answer or ""

        agent = LlmAgent(
            tools=[query_genie],
            instructions="You are a helpful data analyst. Always cite the source.",
        )
    """

    def __init__(
        self,
        tools: list[_ToolFn],
        sub_agents: list[str] | None = None,
        instructions: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_iterations: int | None = None,
        before_tool: BeforeToolHook | None = None,
        after_tool: AfterToolHook | None = None,
        input_guardrails: list[InputGuardrailFn] | None = None,
        output_guardrails: list[OutputGuardrailFn] | None = None,
        context_window_tokens: int | None = None,
    ) -> None:
        self._tool_fns = tools
        self._sub_agent_urls = sub_agents or []
        self._instructions = instructions
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._before_tool = before_tool
        self._after_tool = after_tool
        self._input_guardrails = input_guardrails or []
        self._output_guardrails = output_guardrails or []
        self._context_window_tokens = context_window_tokens

        # Pre-analyze all functions at construction time
        self._analyzed: list[tuple[_ToolFn, dict[str, Any], list[str], type[BaseModel] | None]] = []
        for fn in tools:
            plain_params, dep_names = _inspect_tool_fn(fn)
            input_model = _make_input_model(fn, plain_params)
            self._analyzed.append((fn, plain_params, dep_names, input_model))

    async def _apply_input_guardrails(self, messages: list[Message]) -> str | None:
        for guard in self._input_guardrails:
            result = (await guard(messages)) if inspect.iscoroutinefunction(guard) else guard(messages)  # type: ignore[arg-type]
            if result is not None:
                return result
        return None

    async def _apply_output_guardrails(self, text: str) -> str | None:
        for guard in self._output_guardrails:
            result = (await guard(text)) if inspect.iscoroutinefunction(guard) else guard(text)  # type: ignore[arg-type]
            if result is not None:
                return result
        return None

    async def run(self, messages: list[Message], request: Request) -> str:
        from ._runner import run_via_sdk

        if rejection := await self._apply_input_guardrails(messages):
            return rejection
        text = await run_via_sdk(
            messages, request,
            tools=self.collect_tools(),
            instructions=self._instructions,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            max_iterations=self._max_iterations,
        )
        if replacement := await self._apply_output_guardrails(text):
            return replacement
        return text

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        from ._runner import stream_via_sdk

        if rejection := await self._apply_input_guardrails(messages):
            yield rejection
            return

        full_text = ""
        async for chunk in stream_via_sdk(
            messages, request,
            tools=self.collect_tools(),
            instructions=self._instructions,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            max_iterations=self._max_iterations,
        ):
            full_text += chunk
            yield chunk

        if replacement := await self._apply_output_guardrails(full_text):
            yield f"\n\n[Guardrail override: {replacement}]"

    def build_router(self) -> APIRouter:
        """Build an APIRouter with a POST route for each tool."""
        router = APIRouter()
        for fn, plain_params, dep_names, input_model in self._analyzed:
            handler = _make_route_handler(fn, input_model, dep_names)
            router.add_api_route(
                f"/tools/{fn.__name__}",
                handler,
                methods=["POST"],
                operation_id=fn.__name__,
                summary=fn.__doc__ or fn.__name__,
                response_model=None,
            )
        return router

    def get_tool_routers(self) -> list[APIRouter]:
        return [self.build_router()]

    def collect_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name=fn.__name__,
                description=(fn.__doc__ or "").strip(),
                input_schema=_schema_for_model(input_model),
                output_schema=_schema_for_return(fn),
            )
            for fn, _, _, input_model in self._analyzed
        ]

    async def fetch_remote_tools(self) -> list[AgentTool]:
        """Fetch agent cards from sub-agent URLs and build tools from them.

        Uses the workspace client's auth headers to authenticate with
        Databricks Apps (which require OAuth/bearer tokens).
        """
        import os

        from databricks.sdk import WorkspaceClient
        from httpx import AsyncClient

        # Get auth headers from the workspace client for app-to-app calls
        try:
            ws = WorkspaceClient()
            auth_headers = ws.config.authenticate()
        except Exception:
            auth_headers = {}

        tools: list[AgentTool] = []
        async with AsyncClient(timeout=10.0) as client:
            for raw_url in self._sub_agent_urls:
                if raw_url.startswith("$"):
                    var_name = raw_url.lstrip("$").strip("{}")
                    url = os.environ.get(var_name, "")
                    if not url:
                        logger.warning(f"sub_agent env var {var_name} not set — skipping")
                        continue
                else:
                    url = raw_url
                card_url = f"{url.rstrip('/')}/.well-known/agent.json"
                try:
                    response = await client.get(card_url, headers=auth_headers)
                    response.raise_for_status()
                    card = response.json()
                except Exception as e:
                    logger.warning(f"Failed to fetch agent card from {card_url}: {e}")
                    continue

                raw_name = card.get("name", url.split("/")[-1])
                tool_name = raw_name.replace("-", "_").replace(" ", "_")
                tools.append(AgentTool(
                    name=tool_name,
                    description=card.get("description", f"Agent at {url}"),
                    input_schema={
                        "type": "object",
                        "properties": {"message": {"type": "string", "description": "Message to send"}},
                        "required": ["message"],
                    },
                    output_schema={"type": "string"},
                    sub_agent_url=url.rstrip("/"),
                ))
                logger.info(f"Registered sub-agent '{tool_name}' from {url}")

        return tools


# Backwards-compatible alias
Agent = LlmAgent


class _FinishLoopBody(BaseModel):
    reason: str = "Task complete"


class LoopAgent(BaseAgent):
    """Runs a sub-agent repeatedly until it calls ``finish_loop()`` or ``max_iterations`` is reached."""

    FINISH_TOOL = "finish_loop"

    def __init__(self, agent: LlmAgent, max_iterations: int = 5) -> None:
        self._inner = agent
        self._max_iterations = max_iterations

    def collect_tools(self) -> list[AgentTool]:
        tools = self._inner.collect_tools()
        tools.append(AgentTool(
            name=self.FINISH_TOOL,
            description="Signal that the iterative task is complete and return the final result.",
            input_schema={
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Why the task is complete"}},
                "required": [],
            },
        ))
        return tools

    def get_tool_routers(self) -> list[APIRouter]:
        routers = self._inner.get_tool_routers()
        router = APIRouter()

        async def finish_loop_handler(request: Request, body: _FinishLoopBody) -> str:
            request.state.loop_done = True
            return body.reason

        finish_loop_handler.__name__ = self.FINISH_TOOL
        router.add_api_route(
            f"/tools/{self.FINISH_TOOL}",
            finish_loop_handler,
            methods=["POST"],
            operation_id=self.FINISH_TOOL,
            summary="Signal loop completion",
            response_model=None,
        )
        routers.append(router)
        return routers

    async def run(self, messages: list[Message], request: Request) -> str:
        from ._runner import run_via_sdk

        context = list(messages)
        result = ""
        request.state.loop_done = False
        all_tools = self.collect_tools()

        for _ in range(self._max_iterations):
            result = await run_via_sdk(
                context, request,
                tools=all_tools,
                instructions=self._inner._instructions,
                temperature=self._inner._temperature,
                max_tokens=self._inner._max_tokens,
                max_iterations=self._inner._max_iterations,
            )
            if getattr(request.state, "loop_done", False):
                break
            context.append(Message(role="assistant", content=result))

        return result

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        yield await self.run(messages, request)

    async def fetch_remote_tools(self) -> list[AgentTool]:
        return await self._inner.fetch_remote_tools()


class SequentialAgent(BaseAgent):
    """Runs agents in order, each receiving the previous agent's output as context."""

    def __init__(self, agents: list[BaseAgent], instructions: str = "") -> None:
        if not agents:
            raise ValueError("SequentialAgent requires at least one agent")
        self._agents = agents
        self._instructions = instructions

    def _prepend_instructions(self, messages: list[Message]) -> list[Message]:
        if not self._instructions:
            return list(messages)
        return [Message(role="system", content=self._instructions), *messages]

    async def run(self, messages: list[Message], request: Request) -> str:
        context = self._prepend_instructions(messages)
        result = ""
        for i, sub in enumerate(self._agents):
            if i > 0:
                # Append previous result and a continuation prompt so the
                # conversation ends with a user message (required by some models)
                context.append(Message(role="assistant", content=result))
                context.append(Message(role="user", content="Continue with the next investigation step based on the findings above."))
            result = await sub.run(context, request)
        return result

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        """Stream all steps — emit each step's output as it completes.

        This keeps the SSE connection alive during long pipelines (6+ steps)
        by yielding text between steps instead of waiting for everything to
        finish before streaming the last step.
        """
        context = self._prepend_instructions(messages)
        total = len(self._agents)
        for i, sub in enumerate(self._agents):
            step_num = i + 1
            if i > 0:
                context.append(Message(role="assistant", content=result))
                context.append(Message(role="user", content="Continue with the next investigation step based on the findings above."))

            # Emit step header so the user sees progress
            yield f"\n\n---\n**Step {step_num}/{total}**\n\n"

            # Stream this step if it supports streaming, otherwise run and yield
            if hasattr(sub, 'stream') and sub is self._agents[-1]:
                # Only truly stream the last step (token-by-token)
                result = ""
                async for chunk in sub.stream(context, request):
                    result += chunk
                    yield chunk
            else:
                result = await sub.run(context, request)
                yield result

    def get_tool_routers(self) -> list[APIRouter]:
        routers: list[APIRouter] = []
        for sub in self._agents:
            routers.extend(sub.get_tool_routers())
        return routers

    def collect_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for sub in self._agents:
            tools.extend(sub.collect_tools())
        return tools

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for sub in self._agents:
            tools.extend(await sub.fetch_remote_tools())
        return tools


class ParallelAgent(BaseAgent):
    """Runs all agents concurrently with the same input and merges their responses."""

    def __init__(self, agents: list[BaseAgent], instructions: str = "") -> None:
        if not agents:
            raise ValueError("ParallelAgent requires at least one agent")
        self._agents = agents
        self._instructions = instructions

    def _prepend_instructions(self, messages: list[Message]) -> list[Message]:
        if not self._instructions:
            return list(messages)
        return [Message(role="system", content=self._instructions), *messages]

    async def run(self, messages: list[Message], request: Request) -> str:
        import asyncio

        context = self._prepend_instructions(messages)
        results = await asyncio.gather(*[sub.run(context, request) for sub in self._agents])
        return "\n\n".join(str(r) for r in results)

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        yield await self.run(messages, request)

    def get_tool_routers(self) -> list[APIRouter]:
        routers: list[APIRouter] = []
        for sub in self._agents:
            routers.extend(sub.get_tool_routers())
        return routers

    def collect_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for sub in self._agents:
            tools.extend(sub.collect_tools())
        return tools

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for sub in self._agents:
            tools.extend(await sub.fetch_remote_tools())
        return tools


class _TransferBody(BaseModel):
    context: str = ""


class RouterAgent(BaseAgent):
    """Routes to one of several sub-agents based on a single LLM routing call."""

    def __init__(
        self,
        agents: list[tuple[str, str, BaseAgent]],
        instructions: str = "",
    ) -> None:
        if not agents:
            raise ValueError("RouterAgent requires at least one agent")
        self._routes = agents
        self._instructions = instructions

    def _transfer_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": f"transfer_to_{name}",
                    "description": description,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name, description, _ in self._routes
        ]

    async def _select_route(self, messages: list[Message], request: Request) -> str | None:
        """One model serving call to pick a route. Returns agent name or None (fallback)."""
        from databricks.sdk import WorkspaceClient
        from httpx import AsyncClient

        ctx: AgentContext = request.app.state.agent_context
        ws: WorkspaceClient = request.app.state.workspace_client

        auth_headers = ws.config.authenticate()
        endpoint_url = f"{ws.config.host.rstrip('/')}/serving-endpoints/{ctx.config.model}/invocations"

        system_content = " ".join(filter(None, [
            self._instructions,
            "Based on the user's request, select the most appropriate agent by calling the transfer function.",
        ]))
        payload_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            *[{"role": m.role, "content": m.content} for m in messages],
        ]

        async with AsyncClient() as client:
            try:
                response = await client.post(
                    endpoint_url,
                    json={"messages": payload_messages, "tools": self._transfer_tool_schemas()},
                    headers=auth_headers,
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning(f"RouterAgent routing call failed ({exc}) — falling back to first agent")
                return None

        tool_calls = data["choices"][0]["message"].get("tool_calls", [])
        if not tool_calls:
            return None

        tool_name: str = tool_calls[0]["function"]["name"]
        for name, _, _ in self._routes:
            if tool_name == f"transfer_to_{name}":
                logger.info(f"RouterAgent → {name}")
                return name
        return None

    async def run(self, messages: list[Message], request: Request) -> str:
        chosen = await self._select_route(messages, request)
        agent_map = {name: agent for name, _, agent in self._routes}
        target = agent_map.get(chosen or "", self._routes[0][2])
        return await target.run(messages, request)

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        chosen = await self._select_route(messages, request)
        agent_map = {name: agent for name, _, agent in self._routes}
        target = agent_map.get(chosen or "", self._routes[0][2])
        async for chunk in target.stream(messages, request):
            yield chunk

    def collect_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for _, _, agent in self._routes:
            tools.extend(agent.collect_tools())
        return tools

    def get_tool_routers(self) -> list[APIRouter]:
        routers: list[APIRouter] = []
        for _, _, agent in self._routes:
            routers.extend(agent.get_tool_routers())
        return routers

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for _, _, agent in self._routes:
            tools.extend(await agent.fetch_remote_tools())
        return tools


class HandoffAgent(BaseAgent):
    """Multi-agent system where each agent can hand off control to another mid-conversation."""

    TRANSFER_PREFIX = "transfer_to_"

    def __init__(
        self,
        agents: dict[str, LlmAgent],
        start: str,
        max_handoffs: int = 5,
    ) -> None:
        if start not in agents:
            raise ValueError(f"HandoffAgent start='{start}' not found in agents dict")
        self._agents = agents
        self._start = start
        self._max_handoffs = max_handoffs

    def _transfer_tools_for(self, current_name: str) -> list[AgentTool]:
        return [
            AgentTool(
                name=f"{self.TRANSFER_PREFIX}{name}",
                description=f"Hand off to the {name} agent.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "context": {
                            "type": "string",
                            "description": "Brief context for the next agent about what has been done so far.",
                        }
                    },
                    "required": [],
                },
            )
            for name in self._agents
            if name != current_name
        ]

    def get_tool_routers(self) -> list[APIRouter]:
        routers: list[APIRouter] = []
        for agent in self._agents.values():
            routers.extend(agent.get_tool_routers())

        router = APIRouter()
        for name in self._agents:
            target_name = name

            async def transfer_handler(request: Request, body: _TransferBody, _name: str = target_name) -> str:
                request.state.handoff_to = _name
                return f"Transferring to {_name}"

            transfer_handler.__name__ = f"{self.TRANSFER_PREFIX}{name}"
            router.add_api_route(
                f"/tools/{self.TRANSFER_PREFIX}{name}",
                transfer_handler,
                methods=["POST"],
                operation_id=f"{self.TRANSFER_PREFIX}{name}",
                summary=f"Hand off to {name}",
                response_model=None,
            )
        routers.append(router)
        return routers

    def collect_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for agent in self._agents.values():
            tools.extend(agent.collect_tools())
        return tools

    async def run(self, messages: list[Message], request: Request) -> str:
        from ._runner import run_via_sdk

        current_name = self._start
        context = list(messages)
        result = ""

        for _ in range(self._max_handoffs + 1):
            agent = self._agents[current_name]
            request.state.handoff_to = None

            own_tools = agent.collect_tools() + self._transfer_tools_for(current_name)

            result = await run_via_sdk(
                context, request,
                tools=own_tools,
                instructions=agent._instructions,
                temperature=agent._temperature,
                max_tokens=agent._max_tokens,
                max_iterations=agent._max_iterations,
            )

            handoff_target: str | None = getattr(request.state, "handoff_to", None)
            if not handoff_target or handoff_target not in self._agents:
                break

            logger.info(f"HandoffAgent: {current_name} → {handoff_target}")
            context.append(Message(role="assistant", content=result))
            current_name = handoff_target

        return result

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        yield await self.run(messages, request)

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for agent in self._agents.values():
            tools.extend(await agent.fetch_remote_tools())
        return tools

"""Agent types — BaseAgent and all composition patterns."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ._models import (
    AfterAgentCallback,
    AfterModelHook,
    AfterToolHook,
    AgentContext,
    AgentTool,
    BeforeAgentCallback,
    BeforeModelHook,
    BeforeToolHook,
    InputGuardrailFn,
    MemoryBackendConfig,
    Message,
    OnModelErrorCallback,
    OnToolErrorCallback,
    OutputGuardrailFn,
    SessionBackendConfig,
    _ToolFn,
    message_input_schema,
    normalize_memory_knob,
    structured_input_schema,
)
from ._inspection import (
    _inspect_tool_fn,
    _make_input_model,
    _make_route_handler,
    _schema_for_model,
    _schema_for_return,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

logger = logging.getLogger(__name__)

# Sub-agent card fetch (#440): bounded retry at startup, lazy repair on first
# use. Boot order in a fleet is not controllable, so a peer that is still
# starting refuses connections for a few seconds — retry cheap failures with
# short backoff, but never hold serving startup past the budget.
_CARD_FETCH_ATTEMPTS = 3
_CARD_FETCH_BACKOFF_S = 0.5
_CARD_FETCH_BUDGET_S = 5.0


def _card_structured_input_schema(card: dict[str, Any]) -> dict[str, Any] | None:
    """The peer card's ``inputSchema`` when it defines the whole-agent interface.

    A config-declared sub-agent becomes ONE delegate tool addressing the peer
    as a whole. Only when the card advertises exactly one skill with a usable
    structured ``inputSchema`` is that schema the agent's interface (#442);
    multi-skill peers keep the free-text ``{message}`` wrapper — their own
    LLM routes across skills internally.
    """
    skills = card.get("skills")
    if not isinstance(skills, list) or len(skills) != 1:
        return None
    skill = skills[0]
    if not isinstance(skill, dict):
        return None
    return structured_input_schema(skill.get("inputSchema"))


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
    ``tools`` may be omitted — an orchestrator whose only capabilities are
    remote ``sub_agents`` (constructor or config-declared) has no local tools.

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
        tools: list[_ToolFn] | None = None,
        sub_agents: list[str] | None = None,
        instructions: str = "",
        instruction: str = "",
        description: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_iterations: int | None = None,
        before_tool: BeforeToolHook | None = None,
        after_tool: AfterToolHook | None = None,
        before_model: BeforeModelHook | None = None,
        after_model: AfterModelHook | None = None,
        before_tool_callback: BeforeToolHook | None = None,
        after_tool_callback: AfterToolHook | None = None,
        before_model_callback: BeforeModelHook | None = None,
        after_model_callback: AfterModelHook | None = None,
        before_agent_callback: BeforeAgentCallback | None = None,
        after_agent_callback: AfterAgentCallback | None = None,
        on_model_error_callback: OnModelErrorCallback | None = None,
        on_tool_error_callback: OnToolErrorCallback | None = None,
        input_guardrails: list[InputGuardrailFn] | None = None,
        output_guardrails: list[OutputGuardrailFn] | None = None,
        context_window_tokens: int | None = None,
        name: str | None = None,
        memory: str = "off",
        output_key: str | None = None,
    ) -> None:
        # tools is optional (#449): an orchestrator whose only capabilities are
        # config-declared sub_agents has no local tools. None → a fresh list
        # per instance (never a shared mutable default).
        tools = [] if tools is None else tools
        self._tool_fns = tools
        self._sub_agent_urls = sub_agents or []
        # Sub-agent URLs already materialized as callable tools (idempotency
        # guard for repeated fetch_remote_tools calls). See #436.
        self._materialized_sub_agent_urls: set[str] = set()
        # URL → the degraded AgentTool descriptor registered when the card
        # fetch failed at startup; repaired lazily on first use (#440).
        self._degraded_sub_agents: dict[str, AgentTool] = {}
        self._instructions = instructions or instruction
        # G3: when set, the agent's final text is written to the shared state
        # channel under this key (readable by later steps via {key} templating).
        self._output_key = output_key
        self._description = description
        self._name = name
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._before_tool = before_tool_callback or before_tool
        self._after_tool = after_tool_callback or after_tool
        self._before_model = before_model_callback or before_model
        self._after_model = after_model_callback or after_model
        self._before_agent_callback = before_agent_callback
        self._after_agent_callback = after_agent_callback
        self._on_model_error_callback = on_model_error_callback
        self._on_tool_error_callback = on_tool_error_callback
        self._input_guardrails = input_guardrails or []
        self._output_guardrails = output_guardrails or []
        self._context_window_tokens = context_window_tokens
        self.memory_config: MemoryBackendConfig | None
        self.session_config: SessionBackendConfig | None
        self.memory_config, self.session_config = normalize_memory_knob(
            memory,
            catalog=getattr(self, "catalog", None),
            schema=getattr(self, "schema", None),
            name=self._name,
        )

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

    async def _invoke_callback(self, callback: Any, *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def run(self, messages: list[Message], request: Request) -> str:
        from ._compile_run import run_via_compile

        await self._invoke_callback(self._before_agent_callback, messages)
        if rejection := await self._apply_input_guardrails(messages):
            return rejection
        text = await run_via_compile(
            self, messages, request, instructions=self._instructions
        )
        if replacement := await self._apply_output_guardrails(text):
            return replacement
        await self._invoke_callback(self._after_agent_callback, text)
        return text

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        from ._compile_run import stream_via_compile

        await self._invoke_callback(self._before_agent_callback, messages)
        if rejection := await self._apply_input_guardrails(messages):
            yield rejection
            return

        full_text = ""
        async for chunk in stream_via_compile(
            self, messages, request, instructions=self._instructions
        ):
            full_text += chunk
            yield chunk

        # NOTE: In streaming mode, earlier tokens have already been delivered to the
        # client. The guardrail replacement is appended rather than substituted.
        # Callers requiring full replacement should use run() instead of stream().
        if replacement := await self._apply_output_guardrails(full_text):
            yield f"\n\n---\n**[Content filtered]** {replacement}"
        await self._invoke_callback(self._after_agent_callback, full_text)

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

    def _register_tool(self, fn: _ToolFn) -> None:
        """Append a tool post-construction, keeping _tool_fns and _analyzed in sync.

        Run-time compile reads _tool_fns; collect_tools()/build_router() read
        _analyzed. Both must grow together or a tool added after __init__ is
        invisible to the A2A card / MCP surface / per-tool routes.
        """
        self._tool_fns.append(fn)
        plain_params, dep_names = _inspect_tool_fn(fn)
        input_model = _make_input_model(fn, plain_params)
        self._analyzed.append((fn, plain_params, dep_names, input_model))

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

        Besides returning the card descriptors (the A2A/MCP advertisement
        surface), each sub-agent is also **materialized as a callable tool**
        in ``_tool_fns`` via ``_ensure_sub_agent_tool`` — the compiled graph
        builds its tool set from ``_tool_fns`` only, so without this step the
        card advertised delegation the LLM could never perform (#436).

        A failed card fetch is retried a few times within a small budget
        (fleet boot races — #440); if it still fails, a degraded fallback
        descriptor is registered and repaired lazily on the tool's first
        invocation (see ``_upgrade_sub_agent``).
        """
        from databricks.sdk import WorkspaceClient
        from httpx import AsyncClient

        from ._env import resolve_env_var

        # Get auth headers from the workspace client for app-to-app calls
        try:
            ws = WorkspaceClient()
            auth_headers = ws.config.authenticate()
        except Exception:
            auth_headers = {}

        tools: list[AgentTool] = []
        async with AsyncClient(timeout=10.0) as client:
            for raw_url in self._sub_agent_urls:
                url = resolve_env_var(raw_url)
                if not url:
                    logger.warning(f"sub_agent env var {raw_url} not set — skipping")
                    continue
                base_url = url.rstrip("/")
                card = await self._fetch_sub_agent_card(client, base_url, auth_headers)

                degraded = self._degraded_sub_agents.get(base_url)
                structured: dict[str, Any] | None = None
                if card is not None:
                    description = card.get("description", f"Agent at {url}")
                    if degraded is not None:
                        # A repeated fetch found the previously-degraded peer
                        # alive. The callable delegate keeps its startup name
                        # (the compiled graph binds by function name) — repair
                        # the description in place and reuse that name here.
                        # The delegate was also registered with the {message}
                        # wrapper, so the descriptor must keep advertising it:
                        # advertised and callable stay in lockstep.
                        self._upgrade_sub_agent(base_url, description)
                        tool_name = degraded.name
                    else:
                        raw_name = card.get("name", url.split("/")[-1])
                        tool_name = raw_name.replace("-", "_").replace(" ", "_")
                        # Carry the card's real input schema instead of
                        # collapsing every peer to {message: string} (#442).
                        structured = _card_structured_input_schema(card)
                else:
                    # Derive name from URL — the sub-agent is still callable at
                    # runtime via the user's OBO token even if the card fetch
                    # fails (e.g. Databricks Apps OAuth proxy).
                    parts = url.rstrip("/").split("/")[-1].split(".")[0].split("-")[0:-1]
                    raw_name = "-".join(parts) if parts else url.split("/")[-1]
                    description = f"Remote agent at {url} (card fetch failed — tool is still callable)"
                    tool_name = raw_name.replace("-", "_").replace(" ", "_")

                descriptor = AgentTool(
                    name=tool_name,
                    description=description,
                    input_schema=structured if structured is not None else message_input_schema(),
                    output_schema={"type": "string"},
                    sub_agent_url=base_url,
                )
                tools.append(descriptor)
                # Advertised AND callable must come from the same loop: register
                # the matching delegate tool so the compiled graph can invoke it.
                self._ensure_sub_agent_tool(
                    tool_name, description, base_url, input_schema=structured
                )
                if card is None and base_url in self._materialized_sub_agent_urls:
                    self._degraded_sub_agents.setdefault(base_url, descriptor)
                    logger.warning(
                        "sub-agent %s: card fetch failed after %d attempts — "
                        "registered degraded stub %r; will repair on first use",
                        base_url,
                        _CARD_FETCH_ATTEMPTS,
                        tool_name,
                    )
                else:
                    logger.info(f"Registered sub-agent '{tool_name}' from {url}")

        return tools

    async def _fetch_sub_agent_card(
        self, client: AsyncClient, base_url: str, auth_headers: dict[str, str]
    ) -> dict[str, Any] | None:
        """GET a sub-agent's A2A card, retrying fast failures within a budget.

        Boot order in a fleet is not controllable: a peer that is still
        starting refuses connections for a few seconds (#440). Cheap failures
        (connection refused) are retried with short backoff; a slow failure
        (timeout) burns the budget and stops the loop, so one dead peer never
        holds serving startup for more than roughly ``_CARD_FETCH_BUDGET_S``
        plus a single request timeout.
        """
        card_url = f"{base_url}/.well-known/agent.json"
        started = time.monotonic()
        for attempt in range(1, _CARD_FETCH_ATTEMPTS + 1):
            try:
                response = await client.get(card_url, headers=auth_headers)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    f"Failed to fetch agent card from {card_url} "
                    f"(attempt {attempt}/{_CARD_FETCH_ATTEMPTS}): {e}"
                )
            if (
                attempt == _CARD_FETCH_ATTEMPTS
                or time.monotonic() - started >= _CARD_FETCH_BUDGET_S
            ):
                break
            await asyncio.sleep(_CARD_FETCH_BACKOFF_S * attempt)
        return None

    def _upgrade_sub_agent(self, base_url: str, description: str) -> None:
        """Repair a degraded sub-agent entry once its card became fetchable (#440).

        Upgrades the advertised descriptor and the delegate's LLM-facing
        ``__doc__`` in place (``collect_tools`` and the compiled graph both
        read ``__doc__`` live). The tool NAME stays as registered at startup:
        the compiled graph binds tools by function name and the A2A card
        snapshot is frozen at startup, so renaming would desync advertised
        from callable.
        """
        if not description:
            return
        descriptor = self._degraded_sub_agents.pop(base_url, None)
        if descriptor is None:
            return
        descriptor.description = description
        for fn in self._tool_fns:
            if fn.__name__ == descriptor.name:
                fn.__doc__ = description
        logger.info(
            "sub-agent %s: card fetch recovered — tool %r description repaired "
            "(tool name is fixed at startup; the A2A card snapshot refreshes "
            "on restart)",
            base_url,
            descriptor.name,
        )

    def _ensure_sub_agent_tool(
        self,
        name: str,
        description: str,
        base_url: str,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        """Register the callable delegate tool for a sub-agent URL (idempotent).

        The A2A card advertises sub-agents from ``fetch_remote_tools``; the
        compiled LangGraph builds its tools from ``_tool_fns``. Before #436
        those never met — the card promised delegation the graph could not
        perform, and the orchestrator LLM hallucinated the sub-agent's answer.
        Registering here (from the same loop that builds the card descriptor)
        keeps the two surfaces derived from one source of truth.

        ``input_schema`` (the peer card's structured ``inputSchema``, already
        vetted by ``structured_input_schema``) makes the delegate expose the
        peer's real typed parameters instead of one ``message`` string (#442).

        Card-fetch failures never reach this method as errors: the caller
        passes the degraded name/description, and the delegate itself returns
        a clear ``"sub-agent at <url> unreachable: ..."`` string if invoked
        while the peer is down — startup never hard-fails on a dead peer.
        The ``on_card`` hook reports the peer's card back the moment any
        invocation observes it, repairing a degraded registration (#440).
        """
        if base_url in self._materialized_sub_agent_urls:
            # Retroactively stamp an already-registered delegate so Topology
            # can hide it from uses-tool after a hot-apply / older process.
            for fn in self._tool_fns:
                if getattr(fn, "__name__", None) == name:
                    fn.__apx_sub_agent_url__ = base_url  # type: ignore[attr-defined]
            return
        if any(fn.__name__ == name for fn in self._tool_fns):
            logger.warning(
                "sub-agent tool %r collides with an existing tool name — the "
                "sub-agent at %s is advertised but keeps the existing tool's "
                "implementation. Rename one of them.",
                name,
                base_url,
            )
            return
        from ._agent_tool import remote_agent_tool  # noqa: PLC0415 — avoid import cycle

        tool = remote_agent_tool(
            base_url,
            name=name,
            description=description,
            input_schema=input_schema,
            on_card=lambda card: self._upgrade_sub_agent(base_url, card.description),
        )
        # Topology uses this to avoid double-drawing the same peer as both
        # uses-tool and delegates-to (hot-apply materializes a callable tool
        # AND keeps the URL in ``_sub_agent_urls``).
        tool.__apx_sub_agent_url__ = base_url  # type: ignore[attr-defined]
        self._register_tool(tool)
        self._materialized_sub_agent_urls.add(base_url)


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
        # The LoopAgent compile already encodes finish_loop semantics and
        # max_iterations as conditional edges in the LangGraph; we just need
        # to compile + invoke. The outer Python loop is no longer needed.
        from ._compile_run import run_via_compile

        return await run_via_compile(
            self, messages, request, instructions=self._inner._instructions
        )

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        from ._compile_run import stream_via_compile

        async for chunk in stream_via_compile(
            self, messages, request, instructions=self._inner._instructions
        ):
            yield chunk

    async def fetch_remote_tools(self) -> list[AgentTool]:
        return await self._inner.fetch_remote_tools()


# Internal continuation turn inserted between SequentialAgent steps so the
# conversation handed to the next step ends with a user message. Prefill-strict
# serving endpoints (e.g. Claude) reject a request whose conversation ends with
# an assistant message. Shared by SequentialAgent.run()/.stream() and the
# compiled-graph path (_compile._sequential_continuation) so the two stay aligned.
SEQUENTIAL_CONTINUATION = "Continue with the next step based on the results above."


class SequentialAgent(BaseAgent):
    """Runs agents in order, each receiving the previous agent's output as context."""

    def __init__(
        self,
        agents: list[BaseAgent],
        instructions: str = "",
        name: str | None = None,
    ) -> None:
        if not agents:
            raise ValueError("SequentialAgent requires at least one agent")
        self._agents = agents
        self._instructions = instructions
        self._name = name

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
                context.append(Message(role="user", content=SEQUENTIAL_CONTINUATION))
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
        result = ""
        for i, sub in enumerate(self._agents):
            step_num = i + 1
            if i > 0:
                context.append(Message(role="assistant", content=result))
                context.append(Message(role="user", content=SEQUENTIAL_CONTINUATION))

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


def _normalize_router_agents(
    agents: list[tuple[str, str, "BaseAgent"]] | list["BaseAgent"],
) -> list[tuple[str, str, "BaseAgent"]]:
    """Normalize either form to ``[(name, description, agent), ...]``."""
    if not agents:
        raise ValueError("RouterAgent requires at least one agent")
    if isinstance(agents[0], tuple):
        return agents  # type: ignore[return-value]
    routes: list[tuple[str, "BaseAgent", "BaseAgent"]] = []
    for agent in agents:
        name = getattr(agent, "_name", None)
        if not name:
            raise ValueError(
                "RouterAgent: each agent must have name= set when passed as a list. "
                f"Got {agent!r} with no name."
            )
        desc = getattr(agent, "_description", "") or f"Routes to the {name} agent."
        routes.append((name, desc, agent))  # type: ignore[arg-type]
    return routes  # type: ignore[return-value]


class RouterAgent(BaseAgent):
    """Routes to one of several sub-agents based on a single LLM routing call.

    Accepts either an explicit ``(name, description, agent)`` triple list, or a
    plain ``list[BaseAgent]`` where each agent supplies its own ``name`` and
    ``description``::

        billing = Agent(
            name="billing",
            description="Handles billing questions and invoice disputes.",
            tools=[lookup_invoice],
        )
        support = Agent(
            name="support",
            description="Answers product and technical support questions.",
            tools=[search_docs],
        )

        router = RouterAgent(agents=[billing, support])
    """

    def __init__(
        self,
        agents: list[tuple[str, str, BaseAgent]] | list[BaseAgent],
        instructions: str = "",
    ) -> None:
        self._routes = _normalize_router_agents(agents)
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
        # The compile path encodes the routing decision as a graph node with
        # conditional edges — no manual HTTP call needed. ``_select_route``
        # stays around for callers that want the routing decision in isolation.
        from ._compile_run import run_via_compile

        return await run_via_compile(
            self, messages, request, instructions=self._instructions
        )

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        from ._compile_run import stream_via_compile

        async for chunk in stream_via_compile(
            self, messages, request, instructions=self._instructions
        ):
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


def _normalize_handoff_agents(
    agents: dict[str, "LlmAgent"] | list["LlmAgent"],
) -> dict[str, "LlmAgent"]:
    """Normalize either form to ``{name: agent}``."""
    if isinstance(agents, dict):
        return agents
    result: dict[str, "LlmAgent"] = {}
    for agent in agents:
        name = getattr(agent, "_name", None)
        if not name:
            raise ValueError(
                "HandoffAgent: each agent must have name= set when passed as a list. "
                f"Got {agent!r} with no name."
            )
        result[name] = agent
    return result


class HandoffAgent(BaseAgent):
    """Multi-agent system where each agent can hand off control to another mid-conversation.

    Accepts either a ``{name: agent}`` dict or a plain ``list[LlmAgent]``
    where each agent supplies its own ``name``::

        triage = Agent(name="triage", description="Classifies incoming requests.", tools=[...])
        billing = Agent(name="billing", description="Handles billing questions.", tools=[...])
        support = Agent(name="support", description="Answers product questions.", tools=[...])

        agent = HandoffAgent(agents=[triage, billing, support], start="triage")
    """

    TRANSFER_PREFIX = "transfer_to_"

    def __init__(
        self,
        agents: dict[str, LlmAgent] | list[LlmAgent],
        start: str | None = None,
        max_handoffs: int = 5,
    ) -> None:
        agents_dict = _normalize_handoff_agents(agents)
        if start is None:
            start = next(iter(agents_dict))
        if start not in agents_dict:
            raise ValueError(f"HandoffAgent start='{start}' not found in agents dict")
        self._agents = agents_dict
        self._start = start
        self._max_handoffs = max_handoffs

    def _transfer_tools_for(self, current_name: str) -> list[AgentTool]:
        return [
            AgentTool(
                name=f"{self.TRANSFER_PREFIX}{name}",
                description=(
                    getattr(sub, "_description", "") or f"Hand off to the {name} agent."
                ),
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
            for name, sub in self._agents.items()
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
        # The HandoffAgent compile encodes the transfer-tool routing as
        # conditional edges from a shared __check__ node, with ``max_handoffs``
        # enforced via a state counter. The whole outer Python loop is now
        # the LangGraph itself.
        from ._compile_run import run_via_compile

        return await run_via_compile(self, messages, request)

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        from ._compile_run import stream_via_compile

        async for chunk in stream_via_compile(self, messages, request):
            yield chunk

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for agent in self._agents.values():
            tools.extend(await agent.fetch_remote_tools())
        return tools


class KeywordRouter(BaseAgent):
    """Routes to one of several branches by substring match — zero LLM cost.

    Use when the route is determined by keywords in the user's latest message
    (e.g. "investigate" → investigation pipeline; everything else → general
    fallback). Pairs cheaply with branches that are themselves expensive
    (multi-step pipelines, LLM-heavy specialists) — the routing decision
    avoids the round trip ``RouterAgent`` would pay an LLM for.

    For semantic routing where the model should pick the branch, use
    :class:`RouterAgent` instead.

    Example::

        investigation_pipeline = SequentialAgent(agents=[step1, step2, step3])
        general_agent = Agent(instructions="...", tools=[...])

        agent = KeywordRouter(
            branches=[
                ("investigation", investigation_pipeline,
                 ["missing", "investigate", "root cause", "why is",
                  "failed", "stale", "data gap"]),
            ],
            default=general_agent,
        )
    """

    DEFAULT_LABEL = "__default__"

    def __init__(
        self,
        branches: list[tuple[str, BaseAgent, list[str]]],
        default: BaseAgent,
    ) -> None:
        if not branches:
            raise ValueError("KeywordRouter requires at least one branch")
        seen: set[str] = set()
        for name, _, _ in branches:
            if not name or name == self.DEFAULT_LABEL:
                raise ValueError(
                    f"Invalid branch name {name!r} (empty or reserved)"
                )
            if name in seen:
                raise ValueError(f"Duplicate branch name: {name!r}")
            seen.add(name)
        self._branches = branches
        self._default = default

    def match(self, text: str) -> str | None:
        """Return the branch name whose keywords appear in ``text``, else ``None``.

        Case-insensitive substring match. First branch with a hit wins, so
        list more specific branches first when keywords overlap.
        """
        lowered = text.lower()
        for name, _, keywords in self._branches:
            if any(kw.lower() in lowered for kw in keywords):
                return name
        return None

    def _select_agent(self, messages: list[Message]) -> BaseAgent:
        text = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "",
        )
        name = self.match(text)
        if name is None:
            return self._default
        for branch_name, agent, _ in self._branches:
            if branch_name == name:
                return agent
        return self._default

    async def run(self, messages: list[Message], request: Request) -> str:
        from ._compile_run import run_via_compile

        return await run_via_compile(self, messages, request, instructions="")

    async def stream(
        self, messages: list[Message], request: Request
    ) -> AsyncGenerator[str, None]:
        from ._compile_run import stream_via_compile

        async for chunk in stream_via_compile(
            self, messages, request, instructions=""
        ):
            yield chunk

    def collect_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for _, agent, _ in self._branches:
            tools.extend(agent.collect_tools())
        tools.extend(self._default.collect_tools())
        return tools

    def get_tool_routers(self) -> list[APIRouter]:
        routers: list[APIRouter] = []
        for _, agent, _ in self._branches:
            routers.extend(agent.get_tool_routers())
        routers.extend(self._default.get_tool_routers())
        return routers

    async def fetch_remote_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for _, agent, _ in self._branches:
            tools.extend(await agent.fetch_remote_tools())
        tools.extend(await self._default.fetch_remote_tools())
        return tools

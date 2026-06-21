"""Compile apx-agent declarative agents to a LangGraph runtime.

Translates apx-agent's BaseAgent tree into a LangGraph StateGraph that runs on
Databricks' supported primitives (LangGraph + databricks-langchain). Tools
expressed as plain typed Python functions are adapted into langchain
StructuredTools; FastAPI ``Dependencies.*`` parameters are resolved at compile
time and captured in closures so the LLM never sees them.

User-scoped OBO auth is preserved by passing a per-request WorkspaceClient into
``compile_to_langgraph``. Every compiled tool closes over that ws, so
downstream Databricks calls run as the calling user.

Supported agent types and their LangGraph topologies:

  * ``LlmAgent``        → ``langchain.agents.create_agent`` node
  * ``SequentialAgent`` → linear ``StateGraph`` (START → s_0 → ... → s_n → END)
  * ``ParallelAgent``   → fan-out / fan-in via ``add_messages`` reducer
  * ``LoopAgent``       → outer loop w/ ``finish_loop`` sentinel + iteration cap
  * ``RouterAgent``     → router decision node + conditional edges to targets
  * ``HandoffAgent``    → conditional edges driven by ``transfer_to_*`` tool calls

Requires ``langgraph`` (included in apx-agent's required dependencies).
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_args, get_type_hints

from fastapi import params

# Hoisted so TypedDicts defined inside compile functions (e.g. LoopState) can
# reference ``Annotated[list, add_messages]``. ``get_type_hints`` evaluates the
# class body in the module's globals later, not in the function's local scope.
from typing_extensions import Annotated, TypedDict

try:  # pragma: no cover — defensive guard
    from langgraph.graph.message import add_messages
except ImportError:  # pragma: no cover — let downstream code raise on use
    add_messages = None  # type: ignore[assignment]

from ._agents import (
    BaseAgent,
    HandoffAgent,
    KeywordRouter,
    LlmAgent,
    LoopAgent,
    ParallelAgent,
    RouterAgent,
    SequentialAgent,
)
from ._defaults import (
    _get_principal,
    _get_progress,
    _get_sql_runner,
    _get_user_client,
    _get_workspace_client,
    get_databricks_headers,
)
from ._mlflow_tracing import emit_progress
from ._inspection import _inspect_tool_fn, _make_input_model

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compile context
# ---------------------------------------------------------------------------


@dataclass
class CompileContext:
    """Per-compile context — bound to a single user/request.

    Build one per request (with the user's OBO WorkspaceClient), then call
    ``compile_to_langgraph(agent, ctx)`` to produce a CompiledStateGraph whose
    tools close over that ws. This is the closure pattern that preserves
    user-scoped auth through LangGraph's execution model.
    """

    ws: "WorkspaceClient"
    model: str
    headers: Any | None = None  # DatabricksAppsHeaders or None for local dev


# ---------------------------------------------------------------------------
# Dependency resolution registry
# ---------------------------------------------------------------------------


def _make_dep_resolvers(ctx: CompileContext) -> dict[Any, Any]:
    """Map FastAPI dependency callables to their resolved values for ``ctx``."""
    from ._sql import run_sql

    return {
        _get_workspace_client: ctx.ws,
        _get_user_client: ctx.ws,
        get_databricks_headers: ctx.headers,
        _get_sql_runner: (lambda q: run_sql(ctx.ws, q)),
        _get_principal: (ctx.headers.user_id if ctx.headers else None),  # E3b
        _get_progress: emit_progress,  # tool progress → trace span events
        # _get_request intentionally omitted: no FastAPI Request inside a
        # compiled graph. Tools needing the raw request can't be compiled
        # without lifting them; we fail loudly if encountered.
    }


def _resolve_deps_for_fn(fn: Any, ctx: CompileContext) -> dict[str, Any]:
    """Resolve all FastAPI dependency parameters of ``fn`` against ``ctx``."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    _, dep_names = _inspect_tool_fn(fn)
    resolvers = _make_dep_resolvers(ctx)

    resolved: dict[str, Any] = {}
    for dep_name in dep_names:
        annotation = hints.get(dep_name)
        if annotation is None:
            raise ValueError(
                f"Cannot resolve {dep_name!r}: missing type hint on tool {fn.__name__!r}"
            )
        depends_obj = next(
            (arg for arg in get_args(annotation) if isinstance(arg, params.Depends)),
            None,
        )
        if depends_obj is None or depends_obj.dependency is None:
            raise ValueError(
                f"Parameter {dep_name!r} of {fn.__name__!r} is not a FastAPI dependency"
            )
        target = depends_obj.dependency
        if target not in resolvers:
            raise ValueError(
                f"No compile-time resolver registered for {target.__qualname__!r} "
                f"(parameter {dep_name!r} of tool {fn.__name__!r}). "
                f"Register it in _make_dep_resolvers."
            )
        resolved[dep_name] = resolvers[target]
    return resolved


# ---------------------------------------------------------------------------
# Tool adapter: apx-agent typed fn → langchain StructuredTool
# ---------------------------------------------------------------------------


def _make_langchain_tool(fn: Any, ctx: CompileContext) -> Any:
    """Wrap an apx-agent tool function as a langchain ``StructuredTool``.

    Dependencies resolve against ``ctx`` and are captured in a closure; the
    LLM-visible schema contains only plain typed parameters.
    """
    from langchain_core.tools import StructuredTool

    plain_params, _ = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, plain_params)
    resolved_deps = _resolve_deps_for_fn(fn, ctx)
    is_async = inspect.iscoroutinefunction(fn)

    if is_async:
        async def _async_wrapper(**kwargs: Any) -> Any:
            return await fn(**kwargs, **resolved_deps)

        def _sync_bridge(**kwargs: Any) -> Any:
            # langgraph's *sync* graph.invoke() path (used by the Apps
            # /invocations and ChatAgent runtimes) calls tools synchronously —
            # and a coroutine-only StructuredTool raises "does not support sync
            # invocation". Bridge to the coroutine here so async tools (sql_tool,
            # genie_tool, uc_function_tool, ...) work in the sync path too.
            import asyncio

            async def _call() -> Any:
                return await fn(**kwargs, **resolved_deps)

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_call())
            # Already inside a running loop — run the coroutine in a worker thread.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(_call())).result()

        _async_wrapper.__name__ = _sync_bridge.__name__ = fn.__name__
        _async_wrapper.__doc__ = _sync_bridge.__doc__ = fn.__doc__
        return StructuredTool.from_function(
            func=_sync_bridge,
            coroutine=_async_wrapper,
            name=fn.__name__,
            description=(fn.__doc__ or fn.__name__).strip(),
            args_schema=input_model,
        )

    def _sync_wrapper(**kwargs: Any) -> Any:
        return fn(**kwargs, **resolved_deps)

    _sync_wrapper.__name__ = fn.__name__
    _sync_wrapper.__doc__ = fn.__doc__
    return StructuredTool.from_function(
        func=_sync_wrapper,
        name=fn.__name__,
        description=(fn.__doc__ or fn.__name__).strip(),
        args_schema=input_model,
    )


# ---------------------------------------------------------------------------
# Model factory — drops `temperature` for Anthropic-on-Databricks
# ---------------------------------------------------------------------------


def _build_chat_databricks(
    endpoint: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Any:
    """Build the ChatDatabricks for an agent's compile path.

    Delegates to the public ``get_llm`` factory, which routes by endpoint
    prefix and applies provider-specific quirk defenses (e.g., stripping
    ``temperature``/``top_p`` for GPT-5 family endpoints). See
    ``apx_agent._llm`` for the full provider-compat rationale.

    ``temperature``/``max_tokens`` are forwarded to the underlying client only
    when set, so ``LlmAgent`` generation knobs take effect. Endpoints that
    reject a knob (e.g. the GPT-5 reasoning family strips ``temperature``)
    still drop it defensively inside ``get_llm``.
    """
    from ._llm import get_llm

    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return get_llm(endpoint, **kwargs)


# ---------------------------------------------------------------------------
# Per-agent compilers
# ---------------------------------------------------------------------------


def _governance_exception_middleware() -> Any:
    """Middleware that converts governance exceptions into tool error results.

    ``before_tool`` guards (Watchdog reject, PolicyGate DENY/ASK) and
    cancellable tools signal via exceptions — ``PermissionError`` (incl.
    ``ApprovalRequired``) and ``ToolCancelled``. Without this middleware
    LangGraph's tool node re-raises them, which kills the WHOLE turn:
    the user sees a dead stream instead of the agent explaining the
    rejection and offering an alternative.

    Converting them to error ``ToolMessage``s keeps the loop alive — the
    LLM reads the reason (the exception message carries it) and can
    respond. Genuine bugs (TypeError, KeyError, ...) still propagate and
    fail loud.

    :returns: An ``AgentMiddleware`` for ``create_agent(middleware=[...])``.
    """
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage

    from ._cancellation import ToolCancelled

    @wrap_tool_call
    def _convert_governance_errors(request: Any, handler: Any) -> Any:
        try:
            return handler(request)
        except (PermissionError, ToolCancelled) as exc:
            return ToolMessage(
                content=f"Error: {exc}",
                tool_call_id=request.tool_call["id"],
                status="error",
            )

    return _convert_governance_errors


def _compile_llm_agent(agent: LlmAgent, ctx: CompileContext) -> Any:
    """Compile an ``LlmAgent`` into a ``create_agent`` runnable."""
    from langchain.agents import create_agent

    from ._callbacks import build_callback_handler

    tools = [_make_langchain_tool(fn, ctx) for fn in agent._tool_fns]
    llm = _build_chat_databricks(
        ctx.model,
        temperature=getattr(agent, "_temperature", None),
        max_tokens=getattr(agent, "_max_tokens", None),
    )
    runnable = create_agent(
        model=llm,
        tools=tools,
        system_prompt=agent._instructions or None,
        middleware=[_governance_exception_middleware()],
    )
    config: dict[str, Any] = {}
    handler = build_callback_handler(agent)
    if handler is not None:
        # LangChain's with_config propagates callbacks to every chain hop
        # inside the agent (LLM calls + tool calls), which is exactly the
        # surface our hooks want to observe.
        config["callbacks"] = [handler]
    max_iter = getattr(agent, "_max_iterations", None)
    if max_iter:
        # Each agent round is one LLM superstep plus (optionally) a tool
        # superstep; allow two graph supersteps per requested iteration so a
        # tool-calling turn isn't cut off mid-round, plus a small margin for
        # the terminal answer hop.
        config["recursion_limit"] = max_iter * 2 + 1
    if config:
        runnable = runnable.with_config(**config)
    return runnable


def _compile_sequential_agent(agent: SequentialAgent, ctx: CompileContext) -> Any:
    """Compile a ``SequentialAgent`` into a linear ``StateGraph``."""
    from langgraph.graph import END, START, MessagesState, StateGraph

    graph = StateGraph(MessagesState)
    node_names: list[str] = []

    for i, sub in enumerate(agent._agents):
        name = getattr(sub, "_name", None) or f"step_{i}"
        if name in node_names:
            name = f"{name}_{i}"  # disambiguate accidental collisions
        node_names.append(name)
        graph.add_node(name, _compile_any(sub, ctx))

    graph.add_edge(START, node_names[0])
    for src, dst in zip(node_names, node_names[1:]):
        graph.add_edge(src, dst)
    graph.add_edge(node_names[-1], END)

    return graph.compile()


def _compile_parallel_agent(agent: ParallelAgent, ctx: CompileContext) -> Any:
    """Compile a ``ParallelAgent`` into a fan-out / fan-in ``StateGraph``.

    All sub-agents fire concurrently (same superstep, all triggered by START)
    and their outputs merge via ``MessagesState``'s ``add_messages`` reducer.
    No explicit join node — LangGraph handles message accumulation natively.
    """
    from langgraph.graph import END, START, MessagesState, StateGraph

    graph = StateGraph(MessagesState)
    node_names: list[str] = []
    for i, sub in enumerate(agent._agents):
        name = getattr(sub, "_name", None) or f"branch_{i}"
        if name in node_names:
            name = f"{name}_{i}"
        node_names.append(name)
        graph.add_node(name, _compile_any(sub, ctx))
        graph.add_edge(START, name)
        graph.add_edge(name, END)
    return graph.compile()


def _build_synthetic_tool(name: str, description: str, marker: str) -> Any:
    """Build a langchain StructuredTool that just records 'I was called'.

    Used for control-flow sentinels (``finish_loop``, ``transfer_to_*``). The
    tool itself is a no-op returning a marker string; routing logic inspects
    the most recent AIMessage's tool_calls to detect that the LLM called it.
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _SyntheticInput(BaseModel):
        context: str = Field(default="", description="Optional context message")

    def _sentinel(context: str = "") -> str:
        return f"{marker}:{context}" if context else marker

    return StructuredTool.from_function(
        func=_sentinel,
        name=name,
        description=description,
        args_schema=_SyntheticInput,
    )


def _last_ai_tool_call_name(messages: list[Any]) -> str | None:
    """Return the name of the most recent AIMessage tool_call, or None."""
    from langchain_core.messages import AIMessage

    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            return msg.tool_calls[-1].get("name") if msg.tool_calls else None
    return None


def _build_subagent_input_messages(
    messages: list[Any], prior_agent_name: str, target_name: str
) -> list[Any]:
    """Build a clean message tail for a sub-agent receiving a handoff.

    Databricks-Claude's ``/chat/completions`` rejects conversations whose tail
    is an ``AIMessage`` ("assistant message prefill" is not supported). The
    accumulated graph state after a transfer ends with the prior sub-agent's
    ``AIMessage`` (the one that called ``transfer_to_<target>``), so we must
    not pass the raw state into the next sub-agent's ``create_agent`` call.

    The boring, definitely-accepted shape: pass the original user query
    (the first ``HumanMessage`` we ever saw) followed by a synthetic
    ``HumanMessage`` carrying the handoff context. Using a ``ToolMessage``
    here would trade one validation error ("prefill") for another
    ("orphan tool_result" — no matching ``tool_call_id``). Stay with
    plain user messages.

    Args:
        messages: The accumulated ``state["messages"]`` from the graph.
        prior_agent_name: Name of the sub-agent that just emitted the
            transfer call. Surfaced in the handoff-context message so the
            downstream agent has provenance.
        target_name: Name of the sub-agent that is about to run. Used in
            the handoff-context message for clarity.

    Returns:
        A short list ``[original_query, handoff_context]`` — no ``AIMessage``
        on the tail, no orphan tool messages. Falls back to the raw messages
        list if no ``HumanMessage`` is found (defensive — shouldn't happen
        in a normal invocation since the user query is always present).
    """
    from langchain_core.messages import HumanMessage

    original_query = next(
        (m for m in messages if isinstance(m, HumanMessage)),
        None,
    )
    if original_query is None:
        # Defensive: no user query in state — fall back to the raw list.
        # This shouldn't happen in practice (every graph.invoke starts with
        # a HumanMessage), but we don't want to silently break callers.
        return list(messages)

    handoff_context = HumanMessage(
        content=(
            f"[Routed from {prior_agent_name} to {target_name}] "
            f"Please help with the request above."
        ),
    )
    return [original_query, handoff_context]


def _infer_prior_agent_name(messages: list[Any], known_names: list[str]) -> str:
    """Best-effort recovery of the agent that issued the most recent handoff.

    The transfer is encoded in the most recent ``AIMessage`` tool call as
    ``transfer_to_<target>`` — that gives us the target but not the source.
    LangGraph state doesn't surface the source-node name to a node body, so
    we fall back to a generic label. Surfaced only in the synthetic handoff
    HumanMessage for provenance; not load-bearing for routing.
    """
    return "upstream agent"


def _compile_loop_agent(agent: LoopAgent, ctx: CompileContext) -> Any:
    """Compile a ``LoopAgent`` — runs the inner LlmAgent until ``finish_loop``
    is called or ``max_iterations`` is hit.

    Topology::

        START → inner_agent → check_done → (loop back) or END
                                  ↑___________|

    ``finish_loop`` is added to the inner agent's tools at compile time. The
    check_done node increments iteration count and inspects the most recent
    AIMessage's tool_calls for ``finish_loop``; the conditional edge routes
    accordingly.
    """
    from langgraph.graph import END, START, StateGraph
    from langchain.agents import create_agent

    inner = agent._inner
    max_iter = agent._max_iterations

    class LoopState(TypedDict):
        messages: Annotated[list, add_messages]
        iteration: int

    # Build the inner react agent with finish_loop appended to its tools.
    finish_tool = _build_synthetic_tool(
        name=LoopAgent.FINISH_TOOL,
        description=(
            "Signal that the iterative task is complete and exit the loop. "
            "Call this tool when no further iterations are needed."
        ),
        marker="LOOP_FINISHED",
    )
    inner_tools = [_make_langchain_tool(fn, ctx) for fn in inner._tool_fns] + [finish_tool]
    llm = _build_chat_databricks(ctx.model)
    inner_node = create_agent(
        model=llm, tools=inner_tools, system_prompt=inner._instructions or None
    )

    def _check_done_node(state: dict) -> dict[str, Any]:
        return {"iteration": state.get("iteration", 0) + 1}

    def _route(state: dict) -> str:
        if state.get("iteration", 0) >= max_iter:
            return END
        if _last_ai_tool_call_name(state["messages"]) == LoopAgent.FINISH_TOOL:
            return END
        return "agent"

    graph = StateGraph(LoopState)
    graph.add_node("agent", inner_node)
    graph.add_node("check", _check_done_node)  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
    graph.add_edge(START, "agent")
    graph.add_edge("agent", "check")
    graph.add_conditional_edges("check", _route, {"agent": "agent", END: END})
    return graph.compile()


def _compile_router_agent(agent: RouterAgent, ctx: CompileContext) -> Any:
    """Compile a ``RouterAgent`` — one LLM call picks a target, then runs it.

    Topology::

        START → router_decision → (conditional) → target_a OR target_b OR ... → END

    The router_decision node makes a single LLM call with bound
    ``transfer_to_<name>`` tools, returns the chosen name in state. The
    conditional edge dispatches to the matching sub-agent node.
    """
    from langchain_core.messages import SystemMessage
    from langgraph.graph import END, START, StateGraph

    class RouterState(TypedDict):
        messages: Annotated[list, add_messages]
        chosen: str

    routes = list(agent._routes)  # [(name, description, sub_agent), ...]
    route_names = [r[0] for r in routes]
    transfer_tools = [
        _build_synthetic_tool(
            name=f"transfer_to_{name}",
            description=description,
            marker=f"ROUTE:{name}",
        )
        for name, description, _ in routes
    ]
    fallback = route_names[0]

    def _router_decision(state: dict) -> dict[str, Any]:
        llm = _build_chat_databricks(ctx.model).bind_tools(transfer_tools)
        sys_msg = SystemMessage(content=(
            (agent._instructions or "").strip()
            + "\nBased on the user's request, select the most appropriate agent "
            "by calling exactly one transfer function."
        ))
        result = llm.invoke([sys_msg, *state["messages"]])
        chosen = fallback
        for tc in getattr(result, "tool_calls", None) or []:
            name = tc.get("name", "")
            if name.startswith("transfer_to_"):
                candidate = name[len("transfer_to_") :]
                if candidate in route_names:
                    chosen = candidate
                    break
        return {"chosen": chosen}

    graph = StateGraph(RouterState)
    graph.add_node("router", _router_decision)  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
    for name, _, sub in routes:
        graph.add_node(name, _compile_any(sub, ctx))
        graph.add_edge(name, END)
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda s: s["chosen"],
        {name: name for name in route_names},
    )
    return graph.compile()


def _compile_keyword_router(agent: KeywordRouter, ctx: CompileContext) -> Any:
    """Compile a ``KeywordRouter`` — zero-LLM substring match picks a branch.

    Topology::

        START → keyword_match → (conditional) → branch_a OR ... OR default → END

    The keyword_match node inspects the latest ``HumanMessage`` in state
    and writes the chosen branch name. No LLM call.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.graph import END, START, StateGraph

    class KeywordRouterState(TypedDict):
        messages: Annotated[list, add_messages]
        chosen: str

    branches = list(agent._branches)
    branch_names = [name for name, _, _ in branches]
    default_label = KeywordRouter.DEFAULT_LABEL

    def _keyword_match(state: dict) -> dict[str, Any]:
        text = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                content = msg.content
                text = content if isinstance(content, str) else str(content)
                break
        chosen = agent.match(text) or default_label
        return {"chosen": chosen}

    graph = StateGraph(KeywordRouterState)
    graph.add_node("router", _keyword_match)  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
    for name, sub, _ in branches:
        graph.add_node(name, _compile_any(sub, ctx))
        graph.add_edge(name, END)
    graph.add_node(default_label, _compile_any(agent._default, ctx))
    graph.add_edge(default_label, END)
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda s: s["chosen"],
        {**{n: n for n in branch_names}, default_label: default_label},
    )
    return graph.compile()


def _compile_handoff_agent(agent: HandoffAgent, ctx: CompileContext) -> Any:
    """Compile a ``HandoffAgent`` — each agent can transfer mid-conversation.

    Topology::

        START → start_agent → check → (transfer? → target_agent → check → ...) or END

    Each agent's tool list is augmented with ``transfer_to_<other>`` synthetic
    tools. After each agent runs, the check node inspects the last AIMessage
    for a transfer call; conditional edge routes to the named target or END.
    ``max_handoffs`` enforced via state counter.
    """
    from langgraph.graph import END, START, StateGraph
    from langchain.agents import create_agent

    agents = dict(agent._agents)  # {name: LlmAgent}
    names = list(agents.keys())
    max_handoffs = agent._max_handoffs
    start_name = agent._start
    prefix = HandoffAgent.TRANSFER_PREFIX

    class HandoffState(TypedDict):
        messages: Annotated[list, add_messages]
        handoffs: int

    llm = _build_chat_databricks(ctx.model)

    def _build_node(current_name: str) -> Any:
        inner = agents[current_name]
        own_tools = [_make_langchain_tool(fn, ctx) for fn in inner._tool_fns]
        transfer_tools = [
            _build_synthetic_tool(
                name=f"{prefix}{other}",
                description=f"Hand off to the {other} agent.",
                marker=f"HANDOFF:{other}",
            )
            for other in names
            if other != current_name
        ]
        react = create_agent(
            model=llm,
            tools=own_tools + transfer_tools,
            system_prompt=inner._instructions or None,
        )
        # The start agent receives the user's original input directly from
        # the graph (state["messages"] == [HumanMessage(query)]), so its
        # conversation tail is already user — no scrub needed.
        if current_name == start_name:
            return react

        # Every other sub-agent reaches this node only after a handoff —
        # i.e. the prior sub-agent's AIMessage with a transfer_to_* tool
        # call is now the tail of the accumulated state. Databricks-Claude
        # rejects that ("assistant message prefill"). Wrap the react agent
        # so its create_agent invocation sees a clean [HumanMessage, ...]
        # tail. The graph's outer state["messages"] keeps accumulating via
        # add_messages — we only scrub the input THIS node sends to the LLM,
        # and we return ONLY the newly produced messages so the synthetic
        # handoff_context HumanMessage doesn't leak into outer state (which
        # would surface as a 'user' role in the final ResponsesAgent output
        # items, failing pydantic validation downstream).
        def _wrapped(state: dict) -> dict[str, Any]:
            scrubbed = _build_subagent_input_messages(
                state["messages"],
                prior_agent_name=_infer_prior_agent_name(state["messages"], names),
                target_name=current_name,
            )
            inner_result = react.invoke({"messages": scrubbed})
            # react agents return {"messages": [...input, ...new]} — strip
            # the input prefix so add_messages only merges the genuinely-new
            # AIMessages / ToolMessages produced by THIS sub-agent.
            inner_messages = inner_result.get("messages", []) if isinstance(
                inner_result, dict
            ) else []
            new_only = inner_messages[len(scrubbed) :]
            return {"messages": new_only}

        return _wrapped

    def _check_handoff(state: dict) -> dict[str, Any]:
        return {"handoffs": state.get("handoffs", 0) + 1}

    def _route(state: dict) -> str:
        if state.get("handoffs", 0) >= max_handoffs + 1:
            return END
        last_call = _last_ai_tool_call_name(state["messages"]) or ""
        if last_call.startswith(prefix):
            target = last_call[len(prefix) :]
            if target in names:
                return target
        return END

    graph = StateGraph(HandoffState)
    graph.add_node("__check__", _check_handoff)  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
    for name in names:
        graph.add_node(name, _build_node(name))  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
        graph.add_edge(name, "__check__")
    graph.add_edge(START, start_name)
    graph.add_conditional_edges(
        "__check__",
        _route,
        {**{n: n for n in names}, END: END},
    )
    return graph.compile()


def _ai_message_text(message: Any) -> str:
    """Flatten a message's content to plain text (list/multimodal → text parts)."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _wrap_served_hooks(agent: LlmAgent, runnable: Any) -> Any:
    """Enforce input/output guardrails + before/after_agent callbacks on the
    served (compiled-graph) path, reusing the exact ``LlmAgent`` helpers so
    behavior matches ``LlmAgent.run``.

    No-op when the agent declares none — returns the runnable unchanged so
    non-guarded agents keep their current streaming behavior. Guarded agents
    buffer: the inner agent runs to completion inside one node so the output
    guard can see the full text (a streamed token can't be retracted — the
    documented G1 ceiling). The served paths stream with ``stream_mode="updates"``
    (node-granular), so a guarded agent simply surfaces as one node update.
    See ``docs/design/served-path-guards-and-identity.md`` (G1).
    """
    has_hooks = bool(
        agent._input_guardrails
        or agent._output_guardrails
        or agent._before_agent_callback is not None
        or agent._after_agent_callback is not None
    )
    if not has_hooks:
        return runnable

    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    async def _guarded(state: dict[str, Any]) -> dict[str, Any]:
        input_msgs = list(state["messages"])
        await agent._invoke_callback(agent._before_agent_callback, input_msgs)
        rejection = await agent._apply_input_guardrails(input_msgs)
        if rejection is not None:
            # Short-circuit: the agent never runs.
            return {"messages": [AIMessage(content=rejection)]}

        result = await runnable.ainvoke(state)
        new_msgs = list(result.get("messages", []))[len(input_msgs):]
        last_ai = next(
            (m for m in reversed(new_msgs) if isinstance(m, AIMessage)), None
        )
        text = _ai_message_text(last_ai) if last_ai is not None else ""

        replacement = await agent._apply_output_guardrails(text)
        if replacement is not None:
            # Matches LlmAgent.run: a replacement is returned as-is and the
            # after_agent callback is NOT fired.
            if last_ai is not None:
                new_msgs = [
                    AIMessage(content=replacement, id=last_ai.id) if m is last_ai else m
                    for m in new_msgs
                ]
            else:
                new_msgs = [AIMessage(content=replacement)]
            return {"messages": new_msgs}

        await agent._invoke_callback(agent._after_agent_callback, text)
        return {"messages": new_msgs}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", _guarded)  # type: ignore[arg-type]  # langgraph StateNode generic can't infer state->dict nodes
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    return graph.compile()


def _compile_any(agent: BaseAgent, ctx: CompileContext) -> Any:
    """Dispatch to the right per-agent compiler."""
    if isinstance(agent, LlmAgent):
        return _wrap_served_hooks(agent, _compile_llm_agent(agent, ctx))
    if isinstance(agent, SequentialAgent):
        return _compile_sequential_agent(agent, ctx)
    if isinstance(agent, ParallelAgent):
        return _compile_parallel_agent(agent, ctx)
    if isinstance(agent, LoopAgent):
        return _compile_loop_agent(agent, ctx)
    if isinstance(agent, RouterAgent):
        return _compile_router_agent(agent, ctx)
    if isinstance(agent, KeywordRouter):
        return _compile_keyword_router(agent, ctx)
    if isinstance(agent, HandoffAgent):
        return _compile_handoff_agent(agent, ctx)
    raise NotImplementedError(
        f"compile_to_langgraph: agent type {type(agent).__name__!r} not supported. "
        f"Supported: LlmAgent, SequentialAgent, ParallelAgent, LoopAgent, "
        f"RouterAgent, KeywordRouter, HandoffAgent."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_to_langgraph(
    agent: BaseAgent,
    *,
    ws: "WorkspaceClient | None",
    model: str,
    headers: Any | None = None,
) -> Any:
    """Compile an apx-agent declarative agent tree to a LangGraph runtime.

    Args:
        agent: The apx-agent ``BaseAgent`` to compile (currently ``LlmAgent``
            or ``SequentialAgent``; more types coming).
        ws: A ``WorkspaceClient`` that compiled tools will close over. For
            user-scoped auth, build per request from the OBO header
            (``X-Forwarded-Access-Token``). For service-principal scope, pass
            the default app SP client. This is the auth seam — what you pass
            here determines what identity the tools execute as.
        model: A Databricks serving endpoint (e.g.
            ``"databricks-claude-sonnet-4-6"``).
        headers: Optional ``DatabricksAppsHeaders``, surfaced to tools that
            declare ``Dependencies.Headers``.

    Returns:
        A LangGraph ``CompiledStateGraph``. Invoke with
        ``{"messages": [HumanMessage(content="...")]}`` or stream via
        ``.astream(..., stream_mode="updates", subgraphs=True)``.

    Example::

        from langchain_core.messages import HumanMessage
        from apx_agent import (
            Dependencies, LlmAgent, SequentialAgent, compile_to_langgraph,
        )

        def scan(lookback_hours: int, ws: Dependencies.UserClient) -> str:
            \"\"\"Scan demand clusters for the lookback window.\"\"\"
            ...

        pipeline = SequentialAgent(agents=[
            LlmAgent(name="scanner", tools=[scan], instructions="..."),
            ...
        ])

        # ws is the per-request OBO client — see _defaults._get_user_client
        graph = compile_to_langgraph(
            pipeline, ws=ws, model="databricks-claude-sonnet-4-6"
        )
        result = graph.invoke({"messages": [HumanMessage(content=prompt)]})
    """
    if ws is None:
        from ._defaults import _make_workspace_client

        ws = _make_workspace_client()
    ctx = CompileContext(ws=ws, model=model, headers=headers)
    return _compile_any(agent, ctx)

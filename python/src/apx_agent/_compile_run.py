"""Drop-in replacement for ``_runner.run_via_sdk`` that uses the compile path.

``BaseAgent.run()`` and ``BaseAgent.stream()`` used to delegate to
``run_via_sdk`` / ``stream_via_sdk`` (OpenAI Agents SDK + DatabricksOpenAI).
This module replaces that delegation with a compile-and-invoke path: the agent
compiles itself to a LangGraph runtime and the graph handles the LLM loop +
tool dispatch + control flow (loops, handoffs, routing).

Why this matters:

  * The OpenAI Agents SDK runtime was the apx-agent equivalent of the legacy
    ``/responses`` endpoint — a parallel runtime to the supported path
    (LangGraph + ``create_agent``). Keeping it as the internal default meant
    every ``.run()`` call ran on the unsupported runtime.
  * Compile-and-invoke routes every agent type — ``LlmAgent``,
    ``SequentialAgent``, ``ParallelAgent``, ``LoopAgent``, ``RouterAgent``,
    ``HandoffAgent`` — through the same code path as ``chat_agent_for`` and
    the ``/invocations`` endpoint. One runtime, one set of scar tissue.
  * MLflow tracing, MLflow ChatAgent compatibility, AI Playground / Review
    App / Agent Evaluation recognition — all "just work" because the runtime
    IS the supported one.

The legacy ``run_via_sdk`` / ``stream_via_sdk`` functions in ``_runner.py``
stay exported for callers that explicitly want the SDK path; this module is
what the agent classes themselves use by default.

OBO auth is preserved: ``_resolve_request_ws`` reads
``X-Forwarded-Access-Token`` from the request headers when available and
builds a per-request user-scoped ``WorkspaceClient``; otherwise falls back to
the app-level SP client at ``request.app.state.workspace_client``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ._models import Message

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from fastapi import Request

    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request WorkspaceClient resolver
# ---------------------------------------------------------------------------


def _resolve_request_ws(request: "Request") -> "WorkspaceClient":
    """Build a WorkspaceClient for this request — OBO if available, else SP.

    Mirrors ``_defaults._get_user_client`` but works with the raw request
    object instead of FastAPI's DI system (since the agent ``.run()`` API
    doesn't go through ``Depends()``).
    """
    from ._defaults import _make_workspace_client

    # OBO header path
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            token = headers.get("X-Forwarded-Access-Token")
        except Exception:
            token = None
        if token:
            host = os.environ.get("DATABRICKS_HOST")
            return _make_workspace_client(token=token, host=host)

    # SP path — app-level workspace client set by lifespan
    try:
        ws = request.app.state.workspace_client
        if ws is not None:
            return ws
    except Exception:
        pass

    # Last resort — default auth chain (CLI for local dev)
    return _make_workspace_client()


# ---------------------------------------------------------------------------
# Message conversion — apx-agent Message ↔ langchain BaseMessage
# ---------------------------------------------------------------------------


def _to_langchain(messages: list[Message], system_prompt: str = "") -> list[Any]:
    """Convert apx-agent ``Message`` list to a langchain ``BaseMessage`` list.

    Prepends ``system_prompt`` as a SystemMessage if non-empty AND no system
    message is already present in the list. Avoids double-prepending when
    SequentialAgent already prepended its own system instructions.
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    out: list[Any] = []
    has_system = any(m.role == "system" for m in messages)
    if system_prompt and not has_system:
        out.append(SystemMessage(content=system_prompt))

    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content or ""))
        elif m.role == "user":
            out.append(HumanMessage(content=m.content or ""))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content or ""))
        elif m.role == "tool":
            out.append(
                ToolMessage(content=m.content or "", tool_call_id=m.tool_call_id or "")
            )
        else:
            out.append(HumanMessage(content=m.content or ""))
    return out


def _final_text(messages: list[Any]) -> str:
    """Extract the final assistant text from a LangGraph result's messages."""
    from langchain_core.messages import AIMessage

    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    # Fallback — return last message content of any kind
    if messages:
        last = messages[-1]
        content = getattr(last, "content", "")
        return content if isinstance(content, str) else str(content)
    return ""


# ---------------------------------------------------------------------------
# run / stream entry points
# ---------------------------------------------------------------------------


async def run_via_compile(
    agent: "BaseAgent",
    input_messages: list[Message],
    request: "Request",
    instructions: str = "",
    **_kwargs: Any,
) -> str:
    """Compile ``agent`` against the request's per-call context, invoke, return text.

    Args:
        agent: The apx-agent ``BaseAgent`` to run. Compiled fresh per call so
            the tool closures bind to THIS request's WorkspaceClient.
        input_messages: The conversation so far in apx-agent's ``Message`` shape.
        request: The FastAPI request — provides app state (model endpoint,
            SP client) and OBO headers (user-scoped client).
        instructions: Optional system prompt to prepend. ``LlmAgent`` typically
            passes its ``_instructions``; the inner ``create_agent`` already
            wires this as ``system_prompt`` during compile, so we only inject
            it into the message list when caller explicitly passes one
            (e.g. SequentialAgent's prepended instructions).
        **_kwargs: Ignored here. Kept for signature compatibility with
            ``run_via_sdk``. Generation knobs (``temperature``, ``max_tokens``,
            ``max_iterations``) are read off the ``LlmAgent`` itself during
            ``compile_to_langgraph`` — see ``_compile._compile_llm_agent`` —
            not passed through this call.

    Returns:
        The final assistant text.
    """
    from ._compile import compile_to_langgraph

    model = _get_model(request)
    ws = _resolve_request_ws(request)

    compiled = compile_to_langgraph(agent, ws=ws, model=model)
    lc_messages = _to_langchain(input_messages, system_prompt=instructions)

    # Use the async graph API so the multi-step LLM + tool loop runs without
    # blocking the event loop (H1). Compiled LangGraph graphs always expose
    # ``ainvoke``; tool execution is already offloaded off-loop inside
    # ``_compile._make_langchain_tool``, so we never touch the sync ``invoke``.
    result = await compiled.ainvoke({"messages": lc_messages})
    return _final_text(result.get("messages", []))


async def stream_via_compile(
    agent: "BaseAgent",
    input_messages: list[Message],
    request: "Request",
    instructions: str = "",
    **_kwargs: Any,
) -> AsyncGenerator[str, None]:
    """Compile ``agent`` and stream text deltas from the graph's node updates.

    Yields one chunk per new AIMessage produced inside the compiled graph
    (matching the granularity of ``stream_mode="updates"`` in LangGraph).
    Tool-call AIMessages are skipped — only final-text AI messages stream.
    """
    from langchain_core.messages import AIMessage

    from ._compile import compile_to_langgraph

    model = _get_model(request)
    ws = _resolve_request_ws(request)

    compiled = compile_to_langgraph(agent, ws=ws, model=model)
    lc_messages = _to_langchain(input_messages, system_prompt=instructions)

    def _texts_from_chunk(chunk: Any) -> list[str]:
        """Extract user-visible text deltas from one stream_mode='updates' chunk."""
        texts: list[str] = []
        if not isinstance(chunk, dict):
            return texts
        for _node_name, node_output in chunk.items():
            if not isinstance(node_output, dict):
                continue
            for msg in node_output.get("messages", []) or []:
                if not isinstance(msg, AIMessage):
                    continue
                if getattr(msg, "tool_calls", None):
                    continue  # internal step, not user-visible text
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                if text:
                    texts.append(text)
        return texts

    # Stream via the async graph API so the event loop is never blocked (H1).
    # Compiled LangGraph graphs always expose ``astream``; tool execution is
    # already offloaded off-loop inside ``_compile``, so the sync ``stream`` is
    # never needed.
    async for chunk in compiled.astream({"messages": lc_messages}, stream_mode="updates"):
        for text in _texts_from_chunk(chunk):
            yield text


def _get_model(request: "Request") -> str:
    """Pull the model endpoint from the request's AgentContext, with fallback."""
    try:
        return request.app.state.agent_context.config.model
    except Exception:
        # Fallback: default model from AgentConfig schema
        from ._models import AgentConfig

        return AgentConfig.model_fields["model"].default  # type: ignore[union-attr]

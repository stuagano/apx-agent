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

OBO auth is preserved separately from the app service identity. A forwarded
token builds the per-request user client; without one the user client is
``None``. The initialized app client remains the service client in both cases.
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
# Per-request WorkspaceClient resolvers
# ---------------------------------------------------------------------------


def _resolve_request_user_ws(request: "Request") -> "WorkspaceClient | None":
    """Build the request's OBO user client, or return ``None`` without a token.

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
    return None


def _resolve_request_service_ws(request: "Request") -> "WorkspaceClient":
    """Return the initialized app service client or the default service client."""

    from ._defaults import _make_workspace_client

    try:
        ws = request.app.state.workspace_client
        if ws is not None:
            return ws
    except Exception:
        pass

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
            tool closures bind to this request's distinct user and service
            clients.
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
    # Drive the same LangGraphExecutor that stream_via_compile / the served
    # /invocations path use, so every entry point shares one runtime.  We drain
    # the event stream and return the final text from TurnComplete; an
    # ExecutorError surfaces as a RuntimeError rather than a silent empty
    # string (mirrors _responses_agent's executor consumer).
    from ._executor import ExecutorConfig, ExecutorError, TurnComplete
    from ._langgraph_executor import LangGraphExecutor

    model = _get_model(request)
    user_ws = _resolve_request_user_ws(request)
    service_ws = _resolve_request_service_ws(request)

    executor = LangGraphExecutor(
        agent, user_ws=user_ws, service_ws=service_ws, model=model
    )
    final = ""
    async for event in executor.run_turn(
        messages=input_messages,
        tools=[],
        system_prompt=instructions,
        config=ExecutorConfig(model=model),
    ):
        # TurnComplete.response is None for a tool-only turn with no text;
        # leave ``final`` as "" in that case (matches the old _final_text).
        if isinstance(event, TurnComplete) and event.response is not None:
            final = event.response
        elif isinstance(event, ExecutorError):
            raise RuntimeError(f"Executor error: {event.message}")
    return final


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
    from ._executor import ExecutorConfig, TextChunk
    from ._langgraph_executor import LangGraphExecutor

    model = _get_model(request)
    user_ws = _resolve_request_user_ws(request)
    service_ws = _resolve_request_service_ws(request)

    executor = LangGraphExecutor(
        agent, user_ws=user_ws, service_ws=service_ws, model=model
    )
    async for _event in executor.run_turn(
        messages=input_messages,
        tools=[],
        system_prompt=instructions,
        config=ExecutorConfig(model=model),
    ):
        if isinstance(_event, TextChunk) and _event.text:
            yield _event.text
        # TurnComplete and ExecutorError are not yielded — callers of
        # stream_via_compile expect str chunks only.


def _get_model(request: "Request") -> str:
    """Pull the model endpoint from the request's AgentContext, with fallback."""
    try:
        return request.app.state.agent_context.config.model
    except Exception:
        # Fallback: default model from AgentConfig schema
        from ._models import AgentConfig

        return AgentConfig.model_fields["model"].default  # type: ignore[union-attr]

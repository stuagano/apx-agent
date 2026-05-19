"""Wrap an apx-agent BaseAgent as an MLflow ``ChatAgent``.

The ``ChatAgent`` interface is the supported Mosaic AI agent contract: agents
that subclass it can be logged via ``mlflow.pyfunc.log_model``, registered to
Unity Catalog, served via Model Serving, and evaluated with Agent Evaluation.
The Review App and AI Playground both speak this protocol.

Public surface in this module:

  * ``chat_agent_for(agent, *, model)`` / ``compile_to_chat_agent(agent, *, model)``
    — return a lazy MLflow ChatAgent wrapping the apx-agent tree. The two
    names are interchangeable; ``compile_to_chat_agent`` is the canonical name
    matching the DSL story (apx-agent compiles to a ChatAgent).
  * ``log_agent(agent, *, model, registered_model_name, ...)`` — convenience
    that runs ``mlflow.pyfunc.log_model`` with the wrapped ChatAgent and the
    auto-derived ``resources=[...]`` list from ``mlflow_resources_for``. One
    call gets you a registered, deploy-ready model.

User-scoped OBO auth is preserved by passing the OBO token through
``custom_inputs={"user_token": "<token>"}``; the caller (a Databricks App's
route, typically) is responsible for forwarding the ``X-Forwarded-Access-Token``
header.

Hosting decision matrix:

  Databricks Apps host (recommended for user-scope):
      Read X-Forwarded-Access-Token in your FastAPI route, then either:
        (a) call ``compile_to_langgraph(agent, ws=user_ws, ...)`` directly
            (simpler, no MLflow overhead at runtime), or
        (b) instantiate ``ApxChatAgent`` and call ``predict(messages,
            custom_inputs={"user_token": token})`` (uniform interface, useful
            if the same app also wants the agent loggable to MLflow).

  Model Serving deployment (SP scope by default):
      Log the ApxChatAgent via mlflow.pyfunc.log_model, register in UC,
      deploy. Each request runs as the model's service principal unless the
      caller threads a user token through ``custom_inputs``.

Requires the ``langgraph`` and ``eval`` (mlflow) extras::

    pip install 'apx-agent[langgraph,eval]'
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Generator

from ._agents import BaseAgent
from ._compile import compile_to_langgraph
from ._mlflow_tracing import safe_span, set_span_outputs

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from mlflow.types.agent import (
        ChatAgentChunk,
        ChatAgentMessage,
        ChatAgentResponse,
        ChatContext,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth resolution — closure-based, identical scheme to _defaults._make_workspace_client
# ---------------------------------------------------------------------------


def _resolve_ws_for_request(custom_inputs: dict[str, Any] | None) -> "WorkspaceClient":
    """Build the per-request WorkspaceClient.

    Order:
      1. If ``custom_inputs["user_token"]`` is present → OBO (user-scope).
         Caller must also pass ``workspace_host`` or have ``DATABRICKS_HOST``
         set, since the token alone doesn't identify a workspace.
      2. Else fall back to ``_make_workspace_client`` (app SP via oauth-m2m if
         the env vars are set, else CLI auto-detect for local dev).
    """
    from ._defaults import _make_workspace_client

    if custom_inputs and custom_inputs.get("user_token"):
        token = custom_inputs["user_token"]
        host = (
            custom_inputs.get("workspace_host")
            or os.environ.get("DATABRICKS_HOST")
        )
        return _make_workspace_client(token=token, host=host)

    return _make_workspace_client()


# ---------------------------------------------------------------------------
# Message conversion: ChatAgentMessage ↔ langchain BaseMessage
# ---------------------------------------------------------------------------


def _to_langchain_messages(messages: list["ChatAgentMessage"]) -> list[Any]:
    """Convert MLflow ChatAgentMessage list to langchain BaseMessage list."""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    out: list[Any] = []
    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content or ""))
        elif m.role == "user":
            out.append(HumanMessage(content=m.content or ""))
        elif m.role == "assistant":
            tool_calls = []
            for tc in m.tool_calls or []:
                fn = (tc or {}).get("function", {}) if isinstance(tc, dict) else {}
                tool_calls.append({
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", {}),
                    "id": tc.get("id", "") if isinstance(tc, dict) else "",
                })
            out.append(AIMessage(content=m.content or "", tool_calls=tool_calls))
        elif m.role == "tool":
            out.append(ToolMessage(content=m.content or "", tool_call_id=m.tool_call_id or ""))
        else:
            logger.warning("Unknown message role %r — coercing to HumanMessage", m.role)
            out.append(HumanMessage(content=m.content or ""))
    return out


def _from_langchain_message(msg: Any, idx: int) -> "ChatAgentMessage":
    """Convert a single langchain BaseMessage to a ChatAgentMessage."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from mlflow.types.agent import ChatAgentMessage

    msg_id = getattr(msg, "id", None) or f"msg-{idx}"
    content = msg.content if isinstance(msg.content, str) else str(msg.content)

    if isinstance(msg, AIMessage):
        tool_calls = []
        for tc in msg.tool_calls or []:
            tool_calls.append({
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("args", {}),
                },
            })
        return ChatAgentMessage(
            role="assistant",
            content=content,
            id=msg_id,
            tool_calls=tool_calls or None,
        )
    if isinstance(msg, ToolMessage):
        return ChatAgentMessage(
            role="tool",
            content=content,
            id=msg_id,
            tool_call_id=msg.tool_call_id,
        )
    if isinstance(msg, SystemMessage):
        return ChatAgentMessage(role="system", content=content, id=msg_id)
    if isinstance(msg, HumanMessage):
        return ChatAgentMessage(role="user", content=content, id=msg_id)

    # Unknown type — pass through as assistant
    return ChatAgentMessage(role="assistant", content=content, id=msg_id)


# ---------------------------------------------------------------------------
# ApxChatAgent — the MLflow-compatible wrapper
# ---------------------------------------------------------------------------


def chat_agent_for(
    agent: BaseAgent,
    *,
    model: str,
    session_store: Any | None = None,
) -> Any:
    """Return an MLflow ``ChatAgent`` wrapping ``agent``.

    Args:
        agent: An apx-agent ``BaseAgent`` (currently ``LlmAgent`` or
            ``SequentialAgent`` — see ``compile_to_langgraph`` for the list).
        model: Databricks serving endpoint name passed through to compile.
        session_store: Optional ``SessionStore`` for multi-turn memory.
            When provided, the returned ChatAgent reads ``session_id`` from
            ``custom_inputs`` and uses it to load history before the LLM
            sees the new turn, then appends new messages and persists.
            When ``custom_inputs["session_id"]`` is absent, sessions are
            silently skipped (single-turn behavior preserved).

    Returns:
        An instance of an ``mlflow.pyfunc.ChatAgent`` subclass. Usable
        directly, or loggable via ``mlflow.pyfunc.log_model(python_model=...)``.

    The returned object is a lazy MLflow ChatAgent — instantiation does NOT
    compile the agent. Compilation happens per request inside ``predict`` so
    the per-request user-scoped WorkspaceClient can be threaded through.
    """
    from mlflow.pyfunc import ChatAgent
    from mlflow.types.agent import (
        ChatAgentChunk,
        ChatAgentMessage,
        ChatAgentResponse,
        ChatContext,
    )

    from ._session import Session, append_turn, load_or_create_session

    class _ApxChatAgent(ChatAgent):
        """MLflow ChatAgent backed by an apx-agent declarative tree."""

        def __init__(
            self,
            inner: BaseAgent,
            model_endpoint: str,
            session_store: Any | None = None,
        ) -> None:
            self._agent = inner
            self._model = model_endpoint
            self._session_store = session_store

        def _load_session(self, custom_inputs: dict[str, Any] | None) -> Session | None:
            if self._session_store is None or not custom_inputs:
                return None
            session_id = custom_inputs.get("session_id")
            if not session_id:
                return None
            return load_or_create_session(self._session_store, session_id)

        def _persist_turn(
            self,
            session: Session | None,
            *,
            input_messages: list[ChatAgentMessage],
            new_messages: list[ChatAgentMessage],
        ) -> None:
            if session is None or self._session_store is None:
                return
            input_dicts = [m.model_dump() for m in input_messages]
            new_dicts = [m.model_dump() for m in new_messages]
            append_turn(session, input_messages=input_dicts, new_messages=new_dicts)
            self._session_store.put(session)

        def predict(
            self,
            messages: list[ChatAgentMessage],
            context: ChatContext | None = None,
            custom_inputs: dict[str, Any] | None = None,
        ) -> ChatAgentResponse:
            session = self._load_session(custom_inputs)
            # If a session exists, prepend its history so the LLM sees prior
            # turns. The session itself owns the history; the user just sent
            # ``messages`` for this turn.
            prepended_messages: list[ChatAgentMessage] = list(messages)
            if session is not None and session.history:
                history_msgs = [
                    ChatAgentMessage(
                        role=m.get("role", "user"),
                        content=m.get("content", ""),
                        id=m.get("id"),
                        tool_calls=m.get("tool_calls"),
                        tool_call_id=m.get("tool_call_id"),
                    )
                    for m in session.history
                ]
                prepended_messages = history_msgs + prepended_messages

            span_attrs = {
                "apx.model": self._model,
                "apx.message_count": len(prepended_messages),
                "apx.user_scoped": bool(
                    custom_inputs and custom_inputs.get("user_token")
                ),
                "apx.session_id": (session.session_id if session else ""),
                "apx.streaming": False,
            }
            with safe_span(
                "ApxChatAgent.predict",
                span_type="AGENT",
                inputs={"messages": [m.model_dump() for m in prepended_messages]},
                attributes=span_attrs,
            ) as span:
                ws = _resolve_ws_for_request(custom_inputs)
                with safe_span(
                    "compile_to_langgraph", span_type="CHAIN",
                    attributes={"apx.model": self._model},
                ):
                    graph = compile_to_langgraph(
                        self._agent, ws=ws, model=self._model
                    )

                lc_input = _to_langchain_messages(prepended_messages)
                input_count = len(lc_input)
                with safe_span("graph.invoke", span_type="CHAIN") as inv_span:
                    result = graph.invoke({"messages": lc_input})
                    if inv_span is not None:
                        try:
                            inv_span.set_attribute(
                                "apx.output_message_count",
                                len(result["messages"]) - input_count,
                            )
                        except Exception:  # pragma: no cover
                            pass

                new_lc_messages = result["messages"][input_count:]
                new_messages = [
                    _from_langchain_message(m, idx)
                    for idx, m in enumerate(new_lc_messages)
                ]
                response = ChatAgentResponse(messages=new_messages)
                set_span_outputs(span, response.model_dump())

                # Persist the inbound turn + the new messages.
                self._persist_turn(
                    session,
                    input_messages=messages,
                    new_messages=new_messages,
                )

                return response

        def predict_stream(
            self,
            messages: list[ChatAgentMessage],
            context: ChatContext | None = None,
            custom_inputs: dict[str, Any] | None = None,
        ) -> Generator[ChatAgentChunk, None, None]:
            span_attrs = {
                "apx.model": self._model,
                "apx.message_count": len(messages),
                "apx.user_scoped": bool(
                    custom_inputs and custom_inputs.get("user_token")
                ),
                "apx.streaming": True,
            }
            with safe_span(
                "ApxChatAgent.predict_stream",
                span_type="AGENT",
                inputs={"messages": [m.model_dump() for m in messages]},
                attributes=span_attrs,
            ) as span:
                ws = _resolve_ws_for_request(custom_inputs)
                graph = compile_to_langgraph(
                    self._agent, ws=ws, model=self._model
                )
                lc_input = _to_langchain_messages(messages)
                emitted = 0

                # stream_mode="updates" yields {node_name: {"messages": [...new...]}}
                # per node completion — same pattern Rand's shortage_intel uses.
                for chunk in graph.stream(
                    {"messages": lc_input}, stream_mode="updates"
                ):
                    if not isinstance(chunk, dict):
                        continue
                    for _node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        for msg in node_output.get("messages", []) or []:
                            delta = _from_langchain_message(msg, emitted)
                            emitted += 1
                            yield ChatAgentChunk(delta=delta)
                if span is not None:
                    try:
                        span.set_attribute("apx.chunks_emitted", emitted)
                    except Exception:  # pragma: no cover
                        pass

    return _ApxChatAgent(agent, model, session_store=session_store)


# ---------------------------------------------------------------------------
# Public DSL-aligned names
# ---------------------------------------------------------------------------


def compile_to_chat_agent(
    agent: BaseAgent,
    *,
    model: str,
    session_store: Any | None = None,
) -> Any:
    """Canonical name for ``chat_agent_for`` — apx-agent compiles to a ChatAgent.

    Returns the same MLflow ChatAgent that ``chat_agent_for`` does. Use this
    name when the DSL framing matters at the call site::

        from apx_agent import Agent, compile_to_chat_agent
        chat = compile_to_chat_agent(my_agent, model="databricks-claude-sonnet-4-6")
    """
    return chat_agent_for(agent, model=model, session_store=session_store)


# ---------------------------------------------------------------------------
# mlflow.pyfunc.log_model convenience
# ---------------------------------------------------------------------------


def log_agent(
    agent: BaseAgent,
    *,
    model: str,
    registered_model_name: str | None = None,
    artifact_path: str = "agent",
    extra_resources: list[Any] | None = None,
    input_example: Any | None = None,
    pip_requirements: list[str] | None = None,
    experiment: str | None = None,
    **log_model_kwargs: Any,
) -> Any:
    """Log the apx-agent as an MLflow ChatAgent with auto-derived resources.

    Equivalent to::

        chat = compile_to_chat_agent(agent, model=model)
        resources = mlflow_resources_for(agent, model=model, extra=extra_resources)
        mlflow.pyfunc.log_model(
            artifact_path=artifact_path,
            python_model=chat,
            resources=resources,
            registered_model_name=registered_model_name,
            ...
        )

    Run inside an ``mlflow.start_run()`` context, or call ``mlflow.set_experiment``
    first; this function does not manage runs.

    Args:
        agent: The apx-agent ``BaseAgent`` to log.
        model: Databricks serving endpoint name for the LLM. Also added to the
            ``resources`` list as a ``DatabricksServingEndpoint``.
        registered_model_name: Optional UC three-part name to register the model
            under (e.g. ``"main.agents.data_triage"``). If omitted, the model is
            logged to the run but not registered in UC.
        artifact_path: Subdirectory in the run's artifact store. Default
            ``"agent"``.
        extra_resources: Extra ``ResourceSpec`` instances (or pre-built MLflow
            ``Databricks*`` resources) to append. Use this for resources that
            apx-agent can't auto-infer — e.g. a specific SQL warehouse the
            tools dispatch SQL through, a Vector Search index, a UC table the
            agent reads directly.
        input_example: Optional ``mlflow.types.agent`` example dict passed
            through to ``log_model``.
        pip_requirements: Optional pip requirements list passed through to
            ``log_model``. If omitted, MLflow auto-infers.
        experiment: Optional MLflow experiment name (path or numeric id).
            When set, ``mlflow.set_experiment(experiment)`` is called before
            logging so the run lands in that experiment. When omitted, the
            currently-active experiment (or MLflow's default) is used. Use
            this to keep each agent's runs in its own experiment.
        **log_model_kwargs: Anything else accepted by
            ``mlflow.pyfunc.log_model``.

    Returns:
        The ``ModelInfo`` object returned by ``mlflow.pyfunc.log_model``.
    """
    try:
        import mlflow
        import mlflow.pyfunc
    except ImportError as e:  # pragma: no cover — exercised only without mlflow
        raise ImportError(
            "mlflow is required to log an agent. "
            "Install with: pip install 'apx-agent[eval]'"
        ) from e

    if experiment:
        try:
            mlflow.set_experiment(experiment)
        except Exception as e:
            raise RuntimeError(
                f"mlflow.set_experiment({experiment!r}) failed: {e}. "
                f"For Databricks-hosted MLflow, experiment names are workspace "
                f"paths (e.g. '/Users/you@company.com/agents/my_agent')."
            ) from e

    from ._resources import ResourceSpec, mlflow_resources_for

    # Split extra_resources into specs (auto-materialised) and pre-built
    # mlflow resource objects (passed through verbatim).
    extra_specs: list[ResourceSpec] = []
    prebuilt_resources: list[Any] = []
    for item in extra_resources or []:
        if isinstance(item, ResourceSpec):
            extra_specs.append(item)
        else:
            prebuilt_resources.append(item)

    resources = mlflow_resources_for(agent, model=model, extra=extra_specs)
    resources = resources + prebuilt_resources

    chat_agent = compile_to_chat_agent(agent, model=model)

    return mlflow.pyfunc.log_model(
        artifact_path=artifact_path,
        python_model=chat_agent,
        resources=resources,
        registered_model_name=registered_model_name,
        input_example=input_example,
        pip_requirements=pip_requirements,
        **log_model_kwargs,
    )

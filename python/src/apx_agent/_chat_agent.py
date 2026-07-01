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

Requires the ``eval`` extra (mlflow)::

    pip install 'apx-agent[eval]'
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generator, cast

from ._agents import BaseAgent
from ._audit import AuditAttrs, set_audit_attrs
from ._compile import compile_to_langgraph
from ._conversation import (
    ConversationItem,
    ConversationStore,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    synthesize_conversation_title,
)
from ._mlflow_tracing import safe_span, set_span_outputs

if TYPE_CHECKING:
    from mlflow.types.agent import (
        ChatAgentChunk,
        ChatAgentMessage,
        ChatAgentResponse,
        ChatContext,
    )

logger = logging.getLogger(__name__)

# ── ConversationStore ↔ ChatAgentMessage conversion ──────────────────────────


def _chat_msg_to_new_items(
    msg: "ChatAgentMessage",
    model: str,
    response_id: str,
) -> list[NewConversationItem]:
    """Convert a single ChatAgentMessage to one or more NewConversationItem objects.

    Tool-call assistant messages expand to N ``function_call`` items sharing the
    same ``response_id``; plain text messages become a single ``message`` item;
    tool result messages (role ``"tool"``) become ``function_call_output`` items.

    :param msg: The MLflow ChatAgentMessage to convert.
    :param model: The model/endpoint name, e.g. ``"databricks-claude-sonnet-4-6"``.
        Required for assistant-role items.
    :param response_id: Logical turn ID linking input and output items,
        e.g. ``"turn_abc123"``.
    :returns: A list of :class:`NewConversationItem` objects (may be empty for
        unsupported roles).
    """
    if msg.role == "tool":
        return [NewConversationItem(
            type="function_call_output",
            response_id=response_id,
            data=FunctionCallOutputData(
                call_id=msg.tool_call_id or "",  # str field; ChatAgentMessage.tool_call_id is Optional
                output=msg.content or "",        # str field; ChatAgentMessage.content is Optional
            ),
        )]

    if msg.role == "assistant" and msg.tool_calls:
        items: list[NewConversationItem] = []
        for tc in msg.tool_calls:
            tc_d = tc.model_dump() if hasattr(tc, "model_dump") else tc
            if not isinstance(tc_d, dict):
                continue
            fn = tc_d.get("function") or {}
            if not isinstance(fn, dict):
                fn = {}
            items.append(NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent=model,
                    name=fn.get("name") or "",        # str field; tool_call dicts may omit name
                    arguments=fn.get("arguments") or "{}",  # str field; default to empty JSON obj
                    call_id=tc_d.get("id") or "",     # str field; tool_call dicts may omit id
                ),
            ))
        return items

    if msg.role not in ("user", "assistant"):
        logger.warning(
            "Skipping conversation item with unsupported role %r (only 'user', "
            "'assistant', 'tool' are stored)", msg.role
        )
        return []

    content_type = "output_text" if msg.role == "assistant" else "input_text"
    return [NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role=msg.role,  # type: ignore[arg-type]
            content=[{"type": content_type, "text": msg.content or ""}],  # str field
            agent=model if msg.role == "assistant" else None,
        ),
    )]


def _conv_items_to_chat_msgs(items: list[ConversationItem]) -> list["ChatAgentMessage"]:
    """Convert a list of ConversationItems to ChatAgentMessage objects for history replay.

    Consecutive ``function_call`` items sharing the same ``response_id`` are
    coalesced into a single assistant ChatAgentMessage with ``tool_calls`` set,
    matching the format the LangGraph chat loop expects.

    :param items: Ascending-ordered ConversationItems from the store.
    :returns: A list of :class:`ChatAgentMessage` objects suitable for prepending
        to the current turn's messages.
    """
    from mlflow.types.agent import ChatAgentMessage

    result: list[ChatAgentMessage] = []
    i = 0
    while i < len(items):
        item = items[i]
        if item.type == "message":
            data: MessageData = item.data  # type: ignore[assignment]
            text = "".join(
                b.get("text", "") for b in data.content
                if isinstance(b, dict) and "text" in b
            )
            result.append(ChatAgentMessage(role=data.role, content=text, id=item.id))
            i += 1
        elif item.type == "function_call":
            # Coalesce consecutive function_call items with the same response_id
            # into one assistant message so the LLM sees paired tool_call/tool_result.
            current_rid = item.response_id
            tool_calls: list[dict[str, Any]] = []
            while (
                i < len(items)
                and items[i].type == "function_call"
                and items[i].response_id == current_rid
            ):
                fc: FunctionCallData = items[i].data  # type: ignore[assignment]
                tool_calls.append({
                    "id": fc.call_id,
                    "type": "function",
                    "function": {"name": fc.name, "arguments": fc.arguments},
                })
                i += 1
            result.append(ChatAgentMessage(
                role="assistant",
                content="",
                id=items[i - 1].id,
                tool_calls=cast("Any", tool_calls) if tool_calls else None,
            ))
        elif item.type == "function_call_output":
            fco: FunctionCallOutputData = item.data  # type: ignore[assignment]
            result.append(ChatAgentMessage(
                role="tool",
                content=fco.output,
                id=item.id,
                tool_call_id=fco.call_id,
            ))
            i += 1
        else:
            i += 1  # skip reasoning, compaction, native_tool
    return result


@dataclass(frozen=True)
class _WsAndHeaders:
    ws: Any
    headers: Any


@dataclass(frozen=True)
class _ChatConvLoad:
    """Loaded or created conversation with its history as ChatAgentMessages.

    :param conversation_id: The conversation key from ``custom_inputs``,
        e.g. ``"session-abc123"``.
    :param messages: History messages already persisted for this conversation,
        converted to ``ChatAgentMessage`` format. Empty list for a new conversation.
    :param is_new: ``True`` when the conversation row was just created (first
        turn). Used to gate title synthesis so we only set it once.
    """

    conversation_id: str
    messages: list[Any]  # list[ChatAgentMessage]; Any avoids mlflow import at class-def time
    is_new: bool = False


# ---------------------------------------------------------------------------
# Auth resolution — closure-based, identical scheme to _defaults._make_workspace_client
# ---------------------------------------------------------------------------


def _resolve_ws_and_headers(
    custom_inputs: dict[str, Any] | None,
) -> _WsAndHeaders:
    """Resolve the per-request WorkspaceClient AND DatabricksAppsHeaders from
    a single :func:`extract_obo_headers` call, so ws-identity and
    memory-principal always come from the same source.

    Returns:
        ``(ws, headers)`` where ``headers`` is a :class:`DatabricksAppsHeaders`
        instance when ``custom_inputs`` carries a ``user_id``, else ``None``.
        Keeping ``headers=None`` when there is no identity preserves the
        existing null-principal behaviour for requests without a user context.
    """
    from pydantic import SecretStr

    from ._defaults import DatabricksAppsHeaders, _make_workspace_client
    from ._obo import extract_obo_headers

    # Model Serving has no HTTP-header source for identity — custom_inputs only.
    obo = extract_obo_headers(custom_inputs=custom_inputs)

    # Build the per-request WorkspaceClient (same logic as _resolve_ws_for_request).
    if obo.get("user_token"):
        ws = _make_workspace_client(
            token=obo["user_token"],
            host=obo.get("workspace_host"),
        )
    else:
        # G2: fail closed in the Apps multi-user runtime (unless SP fallback is
        # explicitly opted in) instead of silently running as the app SP.
        from ._obo import resolve_no_obo_or_raise
        resolve_no_obo_or_raise()
        ws = _make_workspace_client()

    # Build headers only when we have a user_id to avoid replacing None with an
    # all-None DatabricksAppsHeaders (which would change the compile-context
    # semantics for tools that check ``if ctx.headers``).
    headers: Any = None
    if obo.get("user_id"):
        token_raw = obo.get("user_token")
        headers = DatabricksAppsHeaders(
            host=obo.get("workspace_host"),
            user_name=None,
            user_id=obo.get("user_id"),
            user_email=obo.get("user_email"),
            request_id=None,
            token=SecretStr(token_raw) if token_raw else None,
        )

    return _WsAndHeaders(ws=ws, headers=headers)


# ---------------------------------------------------------------------------
# Message conversion: ChatAgentMessage ↔ langchain BaseMessage
# ---------------------------------------------------------------------------


def _coerce_tool_args(arguments: Any) -> dict[str, Any]:
    """Coerce a tool-call ``arguments`` value to the dict langchain requires.

    ``ChatAgentMessage`` tool calls carry ``arguments`` as a JSON string on the
    wire (and so does anything round-tripped through ``session.history``);
    langchain ``AIMessage.tool_calls[*].args`` validates as a dict and raises on
    a string. Parse JSON strings; fall back to an empty dict for unparseable or
    non-dict values rather than crashing the whole conversion.
    """
    import json

    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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
                # ``tc`` may be a pydantic ToolCall (m.tool_calls path) or a
                # plain dict (history-prepend path). Normalize to a dict so we
                # don't silently drop name/args/id — which would orphan the
                # following ToolMessage (Databricks-Claude rejects a tool_result
                # whose tool_call_id matches no AIMessage tool call).
                tc_d = tc.model_dump() if hasattr(tc, "model_dump") else tc
                if not isinstance(tc_d, dict):
                    tc_d = {}
                fn = tc_d.get("function", {})
                if not isinstance(fn, dict):
                    fn = {}
                tool_calls.append({
                    "name": fn.get("name", ""),
                    # langchain validates args as a dict and raises on a JSON
                    # string (the wire shape persisted to session history).
                    "args": _coerce_tool_args(fn.get("arguments", {})),
                    "id": tc_d.get("id", ""),
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
        import json
        tool_calls = []
        for tc in msg.tool_calls or []:
            # ChatAgentMessage requires `arguments` to be a JSON string;
            # langchain hands them back as a dict.
            args = tc.get("args", {})
            tool_calls.append({
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            })
        return ChatAgentMessage(
            role="assistant",
            content=content,
            id=msg_id,
            tool_calls=tool_calls or None,
        )
    if isinstance(msg, ToolMessage):
        # ChatAgentMessage requires both name and tool_call_id for tool msgs.
        return ChatAgentMessage(
            role="tool",
            content=content,
            id=msg_id,
            name=getattr(msg, "name", None) or "tool",
            tool_call_id=msg.tool_call_id or "",
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
    conversation_store: ConversationStore | None = None,
    agent_id: str | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Return an MLflow ``ChatAgent`` wrapping ``agent``.

    Args:
        agent: An apx-agent ``BaseAgent`` (currently ``LlmAgent`` or
            ``SequentialAgent`` — see ``compile_to_langgraph`` for the list).
        model: Databricks serving endpoint name passed through to compile.
        conversation_store: Optional :class:`ConversationStore` for multi-turn
            memory. When provided, the returned ChatAgent reads ``session_id``
            from ``custom_inputs`` (or ``conversation_id`` from ``context``,
            bridged in ``_invocations.py``) and uses it to load prior turns
            before the LLM sees the new turn, then appends new items and persists.
            When the key is absent, multi-turn memory is silently skipped.
        agent_id: Identifier bound to conversations created by this agent,
            e.g. ``"my-agent"``. Pass the SAME name readers filter by — the
            dev-UI History panel lists ``list_conversations(agent_id=
            config.name)``. Falls back to ``agent.name`` when omitted.
        checkpointer: Optional LangGraph checkpointer (BaseCheckpointSaver) for
            thread-scoped short-term memory.

    Short-term memory — two tiers, by what you wire:
        * **Durable** — pass a ``conversation_store`` (Lakebase/Delta). Prior
          turns are replayed and persisted across restarts/replicas. This is
          the durable path; a memory store is where durability lives.
        * **In-process (default)** — wire NEITHER a checkpointer nor a store and
          (for an ``LlmAgent``) this defaults to a process-scoped
          ``InMemorySaver``: the agent remembers across turns within one running
          replica, keyed by ``session_id``, but **forgets on restart and does
          not span replicas**. Configure a ``conversation_store`` for durability.
        These never stack: a ``conversation_store`` keeps its replay path and no
        checkpointer is injected (an in-process saver would lose its history).

    Returns:
        An instance of an ``mlflow.pyfunc.ChatAgent`` subclass. Usable
        directly, or loggable via ``mlflow.pyfunc.log_model(python_model=...)``.

    The returned object is a lazy MLflow ChatAgent — instantiation does NOT
    compile the agent. Compilation happens per request inside ``predict`` so
    the per-request user-scoped WorkspaceClient can be threaded through.
    """
    from mlflow.pyfunc import ChatAgent  # type: ignore[attr-defined]  # re-exported from mlflow.pyfunc.model; stub omits it
    from mlflow.types.agent import (
        ChatAgentChunk,
        ChatAgentMessage,
        ChatAgentResponse,
    )

    class _ApxChatAgent(ChatAgent):
        """MLflow ChatAgent backed by an apx-agent declarative tree."""

        def __init__(
            self,
            inner: BaseAgent,
            model_endpoint: str,
            conversation_store: ConversationStore | None = None,
            agent_id: str | None = None,
            checkpointer: Any | None = None,
        ) -> None:
            self._agent = inner
            self._model = model_endpoint
            self._conversation_store = conversation_store
            # Process-scoped LangGraph checkpointer (BaseCheckpointSaver) for
            # thread-scoped short-term memory. When set, the checkpointer owns
            # the transcript: we DON'T replay history (avoids double-context),
            # key state by the session id, and still persist to the
            # conversation store for the dev-UI / observability.
            self._checkpointer = checkpointer
            # Bound onto conversations at creation so agent-filtered readers
            # (dev-UI History panel) can see them.
            self._agent_id = agent_id if agent_id is not None else getattr(inner, "name", None)

        def _short_term_thread(
            self, custom_inputs: dict[str, Any] | None
        ) -> str | None:
            """Session id to key short-term memory by, or ``None`` for a
            stateless (history-replay) turn.

            Returns an id only when a checkpointer is wired AND the caller
            supplied a ``session_id`` — a checkpointer-compiled graph requires a
            ``thread_id``, and the served ``graph.invoke``/``stream`` path is not
            covered by the executor's guard, so a missing id must fall back to
            stateless rather than crash.
            """
            if self._checkpointer is None or not custom_inputs:
                return None
            return custom_inputs.get("session_id")

        def _resolve_model(self) -> str:
            """Return the model endpoint to use for this request.

            Honors the ``APX_AGENT_MODEL_OVERRIDE`` env var if set — enables
            ``hot_swap_model`` to change a deployed agent's LLM without
            re-logging the artifact. Falls back to the compile-time model
            otherwise.
            """
            return os.environ.get("APX_AGENT_MODEL_OVERRIDE") or self._model

        def _load_or_create_conversation(
            self,
            custom_inputs: dict[str, Any] | None,
        ) -> _ChatConvLoad | None:
            """Load the conversation and return its id and history.

            Returns ``None`` when no conversation store is configured or no
            session key is present in ``custom_inputs``. On backend failure,
            logs a warning and returns ``None`` (degrades to a sessionless turn).

            :param custom_inputs: Per-request custom inputs dict from the caller,
                e.g. ``{"session_id": "my_session"}``.
            :returns: A :class:`_ChatConvLoad` on success; ``None`` otherwise.
            """
            if self._conversation_store is None or not custom_inputs:
                return None
            conv_id = custom_inputs.get("session_id")
            if not conv_id:
                return None
            try:
                existing = self._conversation_store.get_conversation(conv_id)
                is_new = existing is None
                if is_new:
                    self._conversation_store.create_conversation(
                        id=conv_id, agent_id=self._agent_id
                    )
                page = self._conversation_store.list_items(
                    conv_id, order="asc", limit=10_000
                )
                history = _conv_items_to_chat_msgs(page.data)
                return _ChatConvLoad(
                    conversation_id=conv_id, messages=history, is_new=is_new
                )
            except Exception as exc:
                logger.warning(
                    "_load_or_create_conversation(%s) degraded to sessionless: %s",
                    conv_id, exc,
                )
                return None

        def _persist_conv_turn(
            self,
            conv_id: str | None,
            *,
            input_messages: list[ChatAgentMessage],
            new_messages: list[ChatAgentMessage],
            model: str,
            response_id: str,
            is_new: bool = False,
        ) -> None:
            """Append the inbound + outbound messages as ConversationStore items.

            No-ops when ``conv_id`` is ``None`` or no conversation store is set.
            On backend failure, logs a warning (degrades to ephemeral turn).

            :param conv_id: The conversation id to append to.
            :param input_messages: User-sent messages for this turn.
            :param new_messages: Agent-generated messages produced this turn.
            :param model: Model endpoint name, e.g. ``"databricks-claude-sonnet-4-6"``.
            :param response_id: Logical turn ID linking input and output items.
            :param is_new: ``True`` when the conversation was just created. When
                set, a title is synthesized from the first user message and
                persisted via ``update_conversation``.
            """
            if conv_id is None or self._conversation_store is None:
                return
            all_msgs = list(input_messages) + list(new_messages)
            new_items = [
                item
                for msg in all_msgs
                for item in _chat_msg_to_new_items(msg, model, response_id)
            ]
            try:
                self._conversation_store.append(conv_id, new_items)
                if is_new:
                    user_item = next(
                        (it for it in new_items
                         if it.type == "message" and isinstance(it.data, MessageData)
                         and it.data.role == "user"),
                        None,
                    )
                    if user_item is not None:
                        assert isinstance(user_item.data, MessageData)
                        title = synthesize_conversation_title(user_item.data.content)
                        if title:
                            self._conversation_store.update_conversation(
                                conv_id, title=title
                            )
            except Exception as exc:
                logger.warning(
                    "_persist_conv_turn(%s) failed — turn not saved: %s", conv_id, exc
                )

        def predict(
            self,
            messages: list[ChatAgentMessage],
            context: ChatContext | None = None,
            custom_inputs: dict[str, Any] | None = None,
        ) -> ChatAgentResponse:
            # Idempotent, never-raises: ensure the in-process trace-capture
            # SpanProcessor is attached so the dev-UI Trace detail can serve
            # this run from memory (FEVM blob egress is blocked).
            from ._trace_store import ensure_capture_processor
            ensure_capture_processor()

            conv = self._load_or_create_conversation(custom_inputs)
            conv_id = conv.conversation_id if conv is not None else None
            history_msgs = conv.messages if conv is not None else []

            # Short-term memory: when a checkpointer is active it holds the
            # transcript, so send ONLY the new turn (no replay → no double
            # context) and key state by thread_id. Otherwise replay history.
            thread_id = self._short_term_thread(custom_inputs)
            checkpointer = self._checkpointer if thread_id else None
            lg_config = {"configurable": {"thread_id": thread_id}} if thread_id else None
            effective_messages: list[ChatAgentMessage] = (
                list(messages)
                if thread_id
                else (history_msgs + list(messages) if history_msgs else list(messages))
            )

            user_token_provided = bool(
                custom_inputs and custom_inputs.get("user_token")
            )
            effective_model = self._resolve_model()
            with safe_span(
                "ApxChatAgent.predict",
                span_type="AGENT",
                inputs={"messages": [m.model_dump() for m in effective_messages]},
                attributes={
                    AuditAttrs.OPERATION: "predict",
                    AuditAttrs.MODEL_ENDPOINT: effective_model,
                    AuditAttrs.MODEL_INPUT_MESSAGES: len(effective_messages),
                    AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                    AuditAttrs.SESSION_ID: conv_id or thread_id,
                    AuditAttrs.MODEL_STREAMING: False,
                },
            ) as span:
                _auth = _resolve_ws_and_headers(custom_inputs)
                with safe_span(
                    "compile_to_langgraph", span_type="CHAIN",
                    attributes={AuditAttrs.MODEL_ENDPOINT: effective_model},
                ):
                    graph = compile_to_langgraph(
                        self._agent, ws=_auth.ws, model=effective_model,
                        headers=_auth.headers,
                        **({"checkpointer": checkpointer} if checkpointer else {}),
                    )

                lc_input = _to_langchain_messages(effective_messages)
                input_count = len(lc_input)
                # With a checkpointer, invoke returns the FULL thread state
                # (prior history + this turn's input + output), not just
                # input+output — so slice the new output past the prior state
                # too, or turn 2+ would echo prior turns. (Mirrors
                # _responses_agent; the #333 test only checked get_state.)
                pre_count = 0
                if checkpointer is not None:
                    try:
                        pre_count = len(graph.get_state(lg_config).values.get("messages", []))
                    except Exception:
                        pre_count = 0
                with safe_span("graph.invoke", span_type="CHAIN") as inv_span:
                    result = graph.invoke(
                        {"messages": lc_input},
                        **({"config": lg_config} if lg_config else {}),
                    )
                    set_audit_attrs(
                        inv_span,
                        model_input_messages=input_count,
                    )

                new_lc_messages = result["messages"][pre_count + input_count:]
                new_messages = [
                    _from_langchain_message(m, idx)
                    for idx, m in enumerate(new_lc_messages)
                ]
                response = ChatAgentResponse(messages=new_messages)
                set_span_outputs(span, response.model_dump())

                self._persist_conv_turn(
                    conv_id,
                    input_messages=messages,
                    new_messages=new_messages,
                    model=effective_model,
                    response_id=str(uuid.uuid4()),
                    is_new=conv.is_new if conv is not None else False,
                )

                return response

        def predict_stream(
            self,
            messages: list[ChatAgentMessage],
            context: ChatContext | None = None,
            custom_inputs: dict[str, Any] | None = None,
        ) -> Generator[ChatAgentChunk, None, None]:
            # See predict — idempotent capture-processor install (never raises).
            from ._trace_store import ensure_capture_processor
            ensure_capture_processor()

            conv = self._load_or_create_conversation(custom_inputs)
            conv_id = conv.conversation_id if conv is not None else None
            history_msgs = conv.messages if conv is not None else []

            # Short-term memory: see predict — checkpointer active → send only
            # the new turn (no replay) and key state by thread_id.
            thread_id = self._short_term_thread(custom_inputs)
            checkpointer = self._checkpointer if thread_id else None
            lg_config = {"configurable": {"thread_id": thread_id}} if thread_id else None
            effective_messages: list[ChatAgentMessage] = (
                list(messages)
                if thread_id
                else (history_msgs + list(messages) if history_msgs else list(messages))
            )

            user_token_provided = bool(
                custom_inputs and custom_inputs.get("user_token")
            )
            effective_model = self._resolve_model()
            with safe_span(
                "ApxChatAgent.predict_stream",
                span_type="AGENT",
                inputs={"messages": [m.model_dump() for m in effective_messages]},
                attributes={
                    AuditAttrs.OPERATION: "predict_stream",
                    AuditAttrs.MODEL_ENDPOINT: effective_model,
                    AuditAttrs.MODEL_INPUT_MESSAGES: len(effective_messages),
                    AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                    AuditAttrs.SESSION_ID: conv_id or thread_id,
                    AuditAttrs.MODEL_STREAMING: True,
                },
            ) as span:
                _auth = _resolve_ws_and_headers(custom_inputs)
                graph = compile_to_langgraph(
                    self._agent, ws=_auth.ws, model=effective_model,
                    headers=_auth.headers,
                    **({"checkpointer": checkpointer} if checkpointer else {}),
                )
                lc_input = _to_langchain_messages(effective_messages)
                emitted = 0
                new_messages: list[ChatAgentMessage] = []

                # stream_mode="updates" yields {node_name: {"messages": [...new...]}}
                # per node completion — same pattern Rand's shortage_intel uses.
                for chunk in graph.stream(
                    {"messages": lc_input}, stream_mode="updates",
                    **({"config": lg_config} if lg_config else {}),
                ):
                    if not isinstance(chunk, dict):
                        continue
                    for _node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        for msg in node_output.get("messages", []) or []:
                            delta = _from_langchain_message(msg, emitted)
                            emitted += 1
                            new_messages.append(delta)
                            yield ChatAgentChunk(delta=delta)
                if span is not None:
                    try:
                        span.set_attribute("apx.chunks_emitted", emitted)
                    except Exception:  # pragma: no cover
                        pass

                # Persist the inbound turn + the new messages — mirrors
                # ``predict`` so streaming multi-turn conversations remember
                # prior turns instead of forgetting them.
                self._persist_conv_turn(
                    conv_id,
                    input_messages=messages,
                    new_messages=new_messages,
                    model=effective_model,
                    response_id=str(uuid.uuid4()),
                    is_new=conv.is_new if conv is not None else False,
                )

    # Short-term memory is on by default for a served LlmAgent: when the caller
    # wired neither a checkpointer NOR a (durable) conversation store to own the
    # transcript, default to a process-scoped InMemorySaver. This makes an
    # otherwise-stateless agent remember within a ``session_id`` — and it's
    # non-regressing: agents WITH a durable conversation store keep their replay
    # path untouched (swapping in an in-process saver would lose history on
    # restart). Only LlmAgent — composite agents can't take a checkpointer
    # (``compile_to_langgraph`` raises). InMemory is per-process; a durable
    # cross-restart backend (Lakebase) is tracked in #329.
    if checkpointer is None and conversation_store is None:
        from ._agents import LlmAgent  # noqa: PLC0415

        if isinstance(agent, LlmAgent):
            from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415

            checkpointer = InMemorySaver()
            logger.info(
                "Short-term memory: in-process (InMemorySaver) for agent %r — "
                "remembers within a replica per session_id, but resets on "
                "restart and does not span replicas. Configure "
                "[tool.apx.agent.session] (a conversation store) for durable "
                "cross-restart memory.",
                getattr(agent, "name", None),
            )

    return _ApxChatAgent(
        agent, model, conversation_store=conversation_store, agent_id=agent_id,
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------------------
# Public DSL-aligned names
# ---------------------------------------------------------------------------


def compile_to_chat_agent(
    agent: BaseAgent,
    *,
    model: str,
    conversation_store: ConversationStore | None = None,
    agent_id: str | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Canonical name for ``chat_agent_for`` — apx-agent compiles to a ChatAgent.

    Returns the same MLflow ChatAgent that ``chat_agent_for`` does. Use this
    name when the DSL framing matters at the call site::

        from apx_agent import Agent, compile_to_chat_agent
        chat = compile_to_chat_agent(my_agent, model="databricks-claude-sonnet-4-6")
    """
    return chat_agent_for(
        agent, model=model, conversation_store=conversation_store, agent_id=agent_id,
        checkpointer=checkpointer,
    )


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

    from ._wiring import finalize_agent

    # Finalize BEFORE resource derivation AND before compile capture: config
    # tools must appear in the logged resources AND in the agent the served
    # model compiles at predict time. log_agent is public (notebooks/Coworker
    # call it directly), so this single site covers all log/deploy paths.
    finalize_agent(agent, pyproject_path=None)  # reads cwd pyproject.toml

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
        input_example=cast("Any", input_example),
        pip_requirements=pip_requirements,
        **log_model_kwargs,
    )

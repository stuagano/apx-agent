"""LangGraph-backed executor for apx-agent harness abstraction.

Wraps :func:`apx_agent._compile.compile_to_langgraph` and adapts its
LangGraph ``CompiledStateGraph`` output to the uniform
:class:`~apx_agent._executor.ExecutorEvent` streaming interface.

The compiled graph is cached per resolved model string so repeated calls from
the same executor instance do not pay the compilation cost more than once.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ._executor import (
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    TurnComplete,
    content_to_text,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


def _final_text_from_messages(messages: list[Any]) -> str:
    """Extract the last non-tool-call assistant text from a list of LangChain messages.

    Iterates in reverse to find the most-recent :class:`~langchain_core.messages.AIMessage`
    that carries no tool calls.  Falls back to the content of the very last message
    if no such AI message is found.

    :param messages: A list of LangChain ``BaseMessage`` objects as returned by
        a compiled LangGraph ``ainvoke`` or collected from ``astream`` chunks.
    :returns: The final assistant text as a plain string.  Returns an empty
        string when *messages* is empty.
    """
    try:
        from langchain_core.messages import AIMessage
    except ImportError:  # pragma: no cover
        # If langchain_core is absent we cannot inspect message types; return last content.
        if messages:
            last = messages[-1]
            content = getattr(last, "content", "")
            return content if isinstance(content, str) else str(content)
        return ""

    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    if messages:
        last = messages[-1]
        content = getattr(last, "content", "")
        return content if isinstance(content, str) else str(content)
    return ""


def _texts_from_chunk(chunk: Any) -> list[str]:
    """Extract user-visible text deltas from one ``stream_mode='updates'`` chunk.

    Each chunk is a ``dict`` keyed by LangGraph node name.  Each value is a
    ``dict`` with a ``"messages"`` key holding a list of LangChain
    ``BaseMessage`` objects.  Only :class:`~langchain_core.messages.AIMessage`
    objects without ``tool_calls`` are considered user-visible.

    :param chunk: A single chunk from a LangGraph ``astream(..., stream_mode='updates')``
        call.  Non-dict values are silently ignored.
    :returns: A list of non-empty text strings extracted from the chunk.  May
        be empty if the chunk contains only tool-call messages or non-message
        content.
    """
    try:
        from langchain_core.messages import AIMessage
    except ImportError:  # pragma: no cover
        return []

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
            text = content_to_text(msg.content)
            if text:
                texts.append(text)
    return texts


def _to_langchain_messages(messages: list[Any], system_prompt: str | None = None) -> list[Any]:
    """Convert a mixed list of messages to LangChain ``BaseMessage`` objects.

    Accepts either :class:`~apx_agent._models.Message` Pydantic objects or plain
    ``dict`` values with ``role`` and ``content`` keys.  Plain dicts are first
    wrapped into :class:`~apx_agent._models.Message` so the existing
    ``_compile_run._to_langchain`` conversion logic is reused.

    :param messages: Conversation history.  Each element is either an
        :class:`~apx_agent._models.Message` instance or a ``dict`` with at
        least ``role`` and ``content`` keys.
    :param system_prompt: Optional system prompt to prepend when no system
        message already exists in *messages*.  Pass ``None`` to skip.
    :returns: A list of LangChain ``BaseMessage`` objects ready to pass to a
        compiled LangGraph graph.
    """
    from ._compile_run import _to_langchain
    from ._models import Message

    normalized: list[Message] = []
    for m in messages:
        if isinstance(m, Message):
            normalized.append(m)
        elif isinstance(m, dict):
            normalized.append(
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content", ""),
                    id=m.get("id"),
                    name=m.get("name"),
                    tool_call_id=m.get("tool_call_id"),
                )
            )
        else:
            # Already a LangChain BaseMessage — pass through as-is via a wrapper
            # that _to_langchain can handle by treating as "user" fallback.
            # In practice callers won't mix LangChain messages here, but be safe.
            normalized.append(Message(role="user", content=str(getattr(m, "content", m))))

    return _to_langchain(normalized, system_prompt=system_prompt or "")


class LangGraphExecutor:
    """Executor that wraps a compiled LangGraph graph.

    Compiles an apx-agent ``BaseAgent`` to a LangGraph ``CompiledStateGraph``
    on first use (lazy compilation) and caches the result per resolved model
    string.  Streaming is attempted via ``astream``; if that raises (e.g. in
    test scenarios where the compiled graph is a ``MagicMock``), the executor
    falls back to ``ainvoke``.

    :param agent: The apx-agent :class:`~apx_agent._agents.BaseAgent` to wrap.
        This is the declarative agent spec — not a compiled graph.
    :param ws: The per-request :class:`~databricks.sdk.WorkspaceClient` to
        bind into tool closures during compilation.  Pass ``None`` to use the
        default SDK auth chain.
    :param model: Default model endpoint name.  May be overridden per turn via
        :attr:`~apx_agent._executor.ExecutorConfig.model`.

    Example::

        from apx_agent import LlmAgent
        executor = LangGraphExecutor(LlmAgent(), ws=None, model="databricks-claude-sonnet-4-6")
        async for event in executor.run_turn([Message(role="user", content="hi")], [], "", None):
            print(event)
    """

    def __init__(
        self,
        agent: "BaseAgent",
        ws: "WorkspaceClient | None",
        model: str | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        """Initialise a LangGraphExecutor without compiling the graph yet.

        :param agent: The declarative apx-agent spec to compile on first use.
        :param ws: Per-request WorkspaceClient for OBO auth.  ``None`` falls
            back to the SDK default auth chain inside
            :func:`~apx_agent._compile.compile_to_langgraph`.
        :param model: Default model endpoint name.  Overridable per-turn via
            :class:`~apx_agent._executor.ExecutorConfig`.
        :param checkpointer: Optional LangGraph checkpointer for thread-scoped
            short-term memory.  Must be **process-scoped** (shared across
            per-request executors) to persist across turns; when set, each
            :meth:`run_turn` requires a ``thread_id`` in its config.
        """
        self._agent = agent
        self._ws = ws
        self._model = model
        self._checkpointer = checkpointer
        # Cache: model_key (str) -> compiled graph
        self._compiled_cache: dict[str, Any] = {}

    def handles_tools_internally(self) -> bool:
        """Return ``True`` — LangGraph's ToolNode dispatches tool calls internally.

        Callers must **not** attempt to intercept :class:`~apx_agent._executor.ToolCallRequest`
        events from this executor; the full tool loop runs inside the graph.

        :returns: Always ``True``.
        """
        return True

    def supports_streaming(self) -> bool:
        """Return ``True`` — LangGraph supports ``astream`` with token-level updates.

        :returns: Always ``True``.
        """
        return True

    def _get_compiled(self, model: str) -> Any:
        """Return a cached compiled graph, compiling on first access for *model*.

        Uses a lazy import of ``compile_to_langgraph`` so that test patches
        targeting ``apx_agent._compile.compile_to_langgraph`` take effect even
        when this executor is constructed before the patch is installed.

        :param model: The resolved model endpoint name to compile against.
        :returns: A LangGraph ``CompiledStateGraph`` (or equivalent mock in
            tests).
        """
        if model not in self._compiled_cache:
            # Lazy import — MUST stay inside this method so tests can patch
            # ``apx_agent._compile.compile_to_langgraph`` at the module level
            # and have the patch visible here.
            from ._compile import compile_to_langgraph

            self._compiled_cache[model] = compile_to_langgraph(
                self._agent, ws=self._ws, model=model, checkpointer=self._checkpointer
            )
        return self._compiled_cache[model]

    async def run_turn(
        self,
        messages: list[Any],
        tools: list[Any],
        system_prompt: str,
        config: ExecutorConfig | None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Execute one agent turn and yield events as they become available.

        Attempts streaming via ``astream``; falls back to ``ainvoke`` if
        ``astream`` raises (e.g. when the compiled graph is a test mock that
        only implements ``ainvoke``).

        Always yields a :class:`~apx_agent._executor.TurnComplete` as the last
        event (or :class:`~apx_agent._executor.ExecutorError` on failure).

        :param messages: Conversation history as a list of
            :class:`~apx_agent._models.Message` instances or plain dicts with
            ``role`` / ``content`` keys.
        :param tools: Ignored — LangGraph handles tool dispatch internally.
            Pass an empty list.
        :param system_prompt: Optional system prompt.  Prepended when no system
            message already exists in *messages*.
        :param config: Per-turn generation config.  Uses
            :attr:`~LangGraphExecutor._model` when
            :attr:`~apx_agent._executor.ExecutorConfig.model` is ``None``.
        :returns: An async iterator of :class:`~apx_agent._executor.ExecutorEvent`
            objects ending with :class:`~apx_agent._executor.TurnComplete`.
        """
        resolved_model = (config.model if config and config.model else None) or self._model or ""

        thread_id = config.thread_id if config else None
        # A compiled-in checkpointer REQUIRES a thread_id at invoke time, or
        # LangGraph raises mid-turn. Couple them explicitly with a clear error.
        if self._checkpointer is not None and not thread_id:
            raise ValueError(
                "LangGraphExecutor was given a checkpointer (short-term memory) "
                "but ExecutorConfig.thread_id is unset — pass the conversation id "
                "as thread_id so state can be keyed to the thread."
            )
        # None config is harmless on a checkpointer-less graph (ignored).
        lg_config = {"configurable": {"thread_id": thread_id}} if thread_id else None

        try:
            compiled = self._get_compiled(resolved_model)
            lc_messages = _to_langchain_messages(messages, system_prompt=system_prompt)

            # Try streaming first.  If astream raises (e.g. the graph is a
            # synchronous-only stub in a test), fall back to ainvoke.
            # The streaming path emits TextChunk events; ainvoke emits none.
            streamed_ok = False
            collected_texts: list[str] = []

            try:
                async for chunk in compiled.astream(
                    {"messages": lc_messages}, stream_mode="updates", config=lg_config
                ):
                    for text in _texts_from_chunk(chunk):
                        collected_texts.append(text)
                        yield TextChunk(text=text)
                streamed_ok = True
            except (TypeError, AttributeError, NotImplementedError):
                # astream not available on this graph — fall through to ainvoke.
                pass

            if streamed_ok:
                # astream ran the graph exactly once.  Use the last emitted
                # text (matches the semantics of _final_text — last non-tool
                # AIMessage) rather than joining all chunks, which would
                # concatenate intermediate + final messages for multi-node
                # graphs (Sequential, Loop, Router, Handoff, …).
                yield TurnComplete(response=collected_texts[-1] if collected_texts else None)
            else:
                # astream raised (graph is a sync-only stub or the provider
                # doesn't support streaming) — fall back to ainvoke once.
                result = await compiled.ainvoke(
                    {"messages": lc_messages}, config=lg_config
                )
                final_text = _final_text_from_messages(result.get("messages", []))
                yield TurnComplete(response=final_text)

        except Exception as exc:
            logger.exception("LangGraphExecutor.run_turn failed: %s", exc)
            yield ExecutorError(message=str(exc), retryable=False)

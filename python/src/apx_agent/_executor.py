"""Executor protocol and event types for apx-agent harness abstraction."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass
class ExecutorEvent:
    """Base class for all events emitted by an :class:`Executor`.

    All concrete event types inherit from this class.  Callers can use
    ``isinstance(event, SomeSubtype)`` to dispatch on the event kind without
    depending on string tags.
    """


@dataclass
class TextChunk(ExecutorEvent):
    """A streaming text delta produced by the model.

    :param text: The text fragment produced in this chunk.  May be a single
        token or a small batch of tokens depending on the provider.
        ``None`` when the chunk carries no text (rare; callers should skip).
    :returns: N/A — dataclass constructor.

    Example::

        chunk = TextChunk(text="Hello, ")
        assert chunk.text == "Hello, "
    """

    text: str | None = None


@dataclass
class ToolCallRequest(ExecutorEvent):
    """The model is requesting a tool call.

    Emitted when the executor handles tool dispatch externally (i.e. when
    :meth:`Executor.handles_tools_internally` returns ``False``).  The caller
    is expected to execute the tool and feed the result back via a subsequent
    turn.

    :param name: The name of the tool to call.
    :param args: The keyword arguments to pass to the tool, as a plain dict.
        Defaults to an empty dict.
    :param call_id: An opaque identifier correlating this request with its
        eventual :class:`ToolCallComplete`.  Defaults to an empty string.
    :returns: N/A — dataclass constructor.
    """

    name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class ToolCallComplete(ExecutorEvent):
    """A tool call has finished.

    :param name: The name of the tool that was called.
    :param call_id: The opaque identifier from the corresponding
        :class:`ToolCallRequest`.  Defaults to an empty string.
    :param result: The return value from the tool.  May be any serialisable
        Python value.  ``None`` when the tool returned nothing or when the
        result is not yet available.
    :param error: Human-readable error message if the tool raised an exception,
        otherwise ``None``.
    :returns: N/A — dataclass constructor.
    """

    name: str | None = None
    call_id: str | None = None
    result: Any = None
    error: str | None = None


@dataclass
class TurnComplete(ExecutorEvent):
    """The model turn has finished and no further streaming events will follow.

    :param response: The final assistant text produced during this turn, or
        ``None`` if the turn produced no textual output (e.g. a tool-only turn).
    :param usage: Optional token-usage statistics as a plain dict, e.g.
        ``{"input_tokens": 42, "output_tokens": 17}``.  ``None`` when the
        provider does not report usage.
    :returns: N/A — dataclass constructor.
    """

    response: str | None = None
    usage: dict[str, Any] | None = None


@dataclass
class ExecutorError(ExecutorEvent):
    """A non-recoverable (or optionally retryable) error from the executor.

    :param message: Human-readable description of the error.
    :param retryable: ``True`` if the caller may safely retry the same turn
        (e.g. transient network error), ``False`` if the turn should be
        abandoned (e.g. bad model output format).  Defaults to ``False``.
    :returns: N/A — dataclass constructor.
    """

    message: str | None = None
    retryable: bool = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ExecutorConfig:
    """Per-turn generation configuration passed to :meth:`Executor.run_turn`.

    Fields not set here fall back to executor-level defaults.

    :param model: The model endpoint name or identifier to use for this turn.
        ``None`` means "use the executor's own default model".
    :param temperature: Sampling temperature in [0, 1].  ``0.0`` is
        deterministic (greedy decoding).  Defaults to ``0.0``.
    :param max_tokens: Maximum number of tokens to generate in the response.
        Defaults to ``100_000``.
    :returns: N/A — dataclass constructor.

    Example::

        cfg = ExecutorConfig(model="databricks-claude-sonnet-4-6", temperature=0.2)
        assert cfg.max_tokens == 100_000
    """

    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 100_000
    thread_id: str | None = None
    """Conversation/thread id for thread-scoped short-term memory. When a
    checkpointer is wired into the executor, this keys the persisted graph
    state so a later turn resumes the same thread. ``None`` runs stateless."""


# ---------------------------------------------------------------------------
# Executor base class
# ---------------------------------------------------------------------------


class Executor:
    """Abstract base class for harness executors.

    An executor wraps an underlying agent runtime (LangGraph, OpenAI Agents
    SDK, a remote service, …) and exposes a uniform async streaming interface
    based on :class:`ExecutorEvent`.

    Concrete implementations override :meth:`run_turn` and optionally override
    :meth:`handles_tools_internally` and :meth:`supports_streaming`.

    Example subclass::

        class EchoExecutor(Executor):
            async def run_turn(self, messages, tools, system_prompt, config):
                yield TurnComplete(response="echo")
    """

    async def run_turn(
        self,
        messages: list[Any],
        tools: list[Any],
        system_prompt: str,
        config: ExecutorConfig | None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Execute one agent turn and yield events as they become available.

        Implementations must yield at least one :class:`TurnComplete` (or
        :class:`ExecutorError`) as the final event of the turn.

        :param messages: Conversation history in the format understood by the
            underlying runtime.  For :class:`LangGraphExecutor` this is a list
            of ``apx_agent.Message`` objects (or plain dicts with ``role`` and
            ``content`` keys).
        :param tools: Tool definitions available to the model for this turn.
            Ignored by :class:`LangGraphExecutor` (which handles tools
            internally); callers may pass an empty list for those executors.
        :param system_prompt: Optional system prompt to prepend when the
            messages list contains no existing system message.  Pass an empty
            string to skip.
        :param config: Per-turn generation config.  ``None`` means "use the
            executor's own defaults".
        :returns: An asynchronous iterator of :class:`ExecutorEvent` objects.
            The last event is always :class:`TurnComplete` or
            :class:`ExecutorError`.
        :raises NotImplementedError: Always — subclasses must override.
        """
        raise NotImplementedError("Subclasses must implement run_turn")
        # The ``yield`` below is never reached; it exists solely to make
        # Python recognise this as an async generator function so that
        # subclasses inheriting from it can use ``yield`` too.
        yield  # type: ignore[misc]  # pragma: no cover

    def handles_tools_internally(self) -> bool:
        """Return ``True`` when the executor's runtime dispatches tool calls.

        When ``True``, the caller must **not** attempt to intercept and execute
        tool calls from :class:`ToolCallRequest` events — the underlying
        runtime already handles the full tool loop.  LangGraph-backed executors
        return ``True``.

        When ``False`` (the default), the caller is responsible for executing
        tool calls and feeding results back.

        :returns: ``False`` by default.
        """
        return False

    def supports_streaming(self) -> bool:
        """Return ``True`` when the executor can emit incremental text chunks.

        Callers may use this hint to decide whether to render a streaming UI.
        Executors that only support batch invocation return ``False``.

        :returns: ``False`` by default.
        """
        return False

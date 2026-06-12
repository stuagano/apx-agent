"""Cancellable tools — interrupt long-running tool calls mid-flight.

Closes roadmap gap 1.2 (omniagents ``CancellableFunctionTool`` parity) with
the apx-agent twist the watchdog integration enables: **a Watchdog
violation anywhere in the session cancels every in-flight tool call.**
A runaway SQL extraction doesn't get to finish just because the violation
was detected on a different hook.

Pieces:

* :class:`CancelToken` — thread-safe one-shot cancellation flag with a
  reason.
* :class:`CancellationRegistry` — tracks the tokens of currently-running
  tool calls; ``cancel_all(reason)`` is the kill switch.
* :func:`cancellable` — decorator that makes a tool function cancellable.
  Stacks under :func:`~apx_agent._tool.tool`::

      registry = CancellationRegistry()

      @tool
      @cancellable(registry=registry, timeout_s=300)
      def run_extraction(table: str) -> str:
          ...

* :class:`ToolCancelled` — raised in the agent loop when a running tool
  is cancelled; the LLM sees it as the tool error and can tell the user.

``WatchdogGuard`` accepts ``cancel_registry=`` and calls
``cancel_all`` whenever a decision comes back ``reject`` — see
:mod:`._watchdog`.

Cancellation semantics (Python can't kill threads):

* The wrapped function runs in a daemon worker thread; the calling thread
  polls for completion, cancellation, and timeout.
* On cancellation the caller raises :class:`ToolCancelled` immediately.
  The worker thread keeps running to completion in the background unless
  the ``canceller`` callback actually interrupts the underlying work —
  pass one whenever the remote side supports it (e.g. SQL statement
  cancellation, HTTP client abort). Its result is discarded either way.
* Cooperative tools can also accept the token directly: a wrapped
  function with a ``cancel_token`` parameter receives the
  :class:`CancelToken` and may check ``is_cancelled`` between work
  batches to stop early.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# How often the supervising thread re-checks cancellation/timeout while the
# worker runs. 100 ms keeps cancellation latency invisible to a human while
# adding negligible idle overhead.
_DEFAULT_POLL_INTERVAL_S = 0.1


class ToolCancelled(RuntimeError):
    """A running tool call was cancelled before it completed.

    Raised in the agent loop (the thread that invoked the tool). The agent
    sees it as a tool error and can surface the reason to the user.

    :param tool_name: The cancelled tool, e.g. ``"run_extraction"``.
    :param reason: Why it was cancelled, e.g.
        ``"Watchdog violation: PII detected in session"`` or
        ``"timed out after 300s"``.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(
            f"Tool {tool_name!r} was cancelled: {reason}. "
            f"Do not assume the operation completed."
        )


class CancelToken:
    """Thread-safe one-shot cancellation flag.

    One token per in-flight tool call. The running side polls
    :attr:`is_cancelled` (or waits on :meth:`wait`); the cancelling side
    calls :meth:`cancel` once with a reason. Subsequent ``cancel`` calls
    are no-ops (the first reason wins).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        """``True`` once :meth:`cancel` has been called."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """The cancellation reason, or ``None`` if not cancelled.

        :returns: The reason passed to the first :meth:`cancel` call,
            e.g. ``"Watchdog violation: cost budget exceeded"``.
        """
        return self._reason

    def cancel(self, reason: str) -> None:
        """Flip the token to cancelled (idempotent — first reason wins).

        :param reason: Why the work should stop,
            e.g. ``"Watchdog violation: PII detected"``.
        """
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled or *timeout* elapses.

        :param timeout: Max seconds to wait. ``None`` waits forever.
        :returns: ``True`` if the token was cancelled, ``False`` on timeout.
        """
        return self._event.wait(timeout)


class CancellationRegistry:
    """Tracks the CancelTokens of currently-running tool calls.

    One registry per agent/session scope. Tools register their token on
    start and unregister on completion; :meth:`cancel_all` is the kill
    switch wired to governance triggers (Watchdog violations, session
    teardown, an operator endpoint).

    Thread-safe — tools run in worker threads while the trigger fires
    from a hook on the agent-loop thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: set[CancelToken] = set()

    def register(self, token: CancelToken) -> None:
        """Track *token* as an in-flight tool call.

        :param token: The running call's :class:`CancelToken`.
        """
        with self._lock:
            self._tokens.add(token)

    def unregister(self, token: CancelToken) -> None:
        """Stop tracking *token* (the call finished or was cancelled).

        Unknown tokens are ignored — unregister after ``cancel_all``
        already cleared the set must not raise.

        :param token: The token to drop.
        """
        with self._lock:
            self._tokens.discard(token)

    @property
    def active_count(self) -> int:
        """Number of in-flight tool calls currently tracked."""
        with self._lock:
            return len(self._tokens)

    def cancel_all(self, reason: str) -> int:
        """Cancel every in-flight tool call.

        :param reason: Propagated to each token (and into each call's
            :class:`ToolCancelled`), e.g.
            ``"Watchdog violation: prompt injection detected"``.
        :returns: How many tokens were newly cancelled (already-cancelled
            tokens don't count).
        """
        with self._lock:
            tokens = list(self._tokens)
        cancelled = 0
        for token in tokens:
            if not token.is_cancelled:
                token.cancel(reason)
                cancelled += 1
        if cancelled:
            logger.warning(
                "CancellationRegistry: cancelled %d in-flight tool call(s): %s",
                cancelled, reason,
            )
        return cancelled


def _accepts_cancel_token(fn: Callable[..., Any]) -> bool:
    """Whether *fn* declares a ``cancel_token`` parameter (cooperative mode).

    :param fn: The tool function being wrapped.
    :returns: ``True`` when the signature has a ``cancel_token`` param.
    """
    try:
        return "cancel_token" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def cancellable(
    fn: _F | None = None,
    *,
    registry: CancellationRegistry | None = None,
    canceller: Callable[[], None] | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> Any:
    """Make a tool function cancellable mid-flight.

    The wrapped function runs in a daemon worker thread while the calling
    thread supervises: when the call's :class:`CancelToken` is cancelled
    (via ``registry.cancel_all`` or directly) or ``timeout_s`` elapses,
    the wrapper raises :class:`ToolCancelled` immediately and invokes
    ``canceller`` (when given) to interrupt the underlying work.

    Stacks under ``@tool`` — ``functools.wraps`` preserves the name,
    docstring, and signature that drive schema generation::

        registry = CancellationRegistry()

        @tool
        @cancellable(registry=registry, timeout_s=300)
        def run_extraction(table: str) -> str:
            \"\"\"Extract TABLE to the staging volume.\"\"\"
            ...

    Cooperative mode: declare a ``cancel_token`` parameter to receive the
    token and stop early between work batches (the supervisor still
    enforces the hard timeout / external cancel)::

        @cancellable(registry=registry)
        def crawl(pages: int, cancel_token: CancelToken | None = None) -> str:
            for page in range(pages):
                if cancel_token is not None and cancel_token.is_cancelled:
                    return "stopped early"
                ...

    :param fn: The tool function (bare ``@cancellable`` form).
    :param registry: Registry tracking this tool's in-flight calls —
        wire the SAME instance into ``WatchdogGuard(cancel_registry=...)``
        for violation-triggered cancellation. ``None`` means calls are
        only cancellable via ``timeout_s``.
    :param canceller: Zero-arg callback invoked once on cancellation to
        interrupt the underlying work, e.g.
        ``lambda: ws.statement_execution.cancel_execution(stmt_id)``.
        Without it the worker thread runs to completion in the
        background and its result is discarded.
    :param timeout_s: Hard wall-clock limit for one call, e.g. ``300``.
        ``None`` means no timeout.
    :param poll_interval_s: Supervisor poll cadence in seconds. The
        default (0.1) bounds cancellation latency at ~100 ms.
    :returns: The wrapped function (or a decorator when called with
        keyword arguments only).
    """

    def _decorate(target: _F) -> _F:
        cooperative = _accepts_cancel_token(target)

        @functools.wraps(target)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            token = CancelToken()
            if registry is not None:
                registry.register(token)

            if cooperative:
                kwargs.setdefault("cancel_token", token)

            done = threading.Event()
            # Single-slot holders for the worker's outcome. A dataclass
            # would be heavier than warranted for this private plumbing.
            outcome: dict[str, Any] = {}

            def _run() -> None:
                try:
                    outcome["result"] = target(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 — re-raised on the caller thread
                    outcome["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(
                target=_run,
                name=f"cancellable-{getattr(target, '__name__', 'tool')}",
                daemon=True,
            )
            worker.start()

            import time as _time
            deadline = (_time.monotonic() + timeout_s) if timeout_s is not None else None

            try:
                while True:
                    if done.wait(poll_interval_s):
                        if "error" in outcome:
                            raise outcome["error"]
                        return outcome["result"]
                    if token.is_cancelled:
                        _fire_canceller(canceller, target)
                        raise ToolCancelled(
                            getattr(target, "__name__", "tool"),
                            token.reason or "cancelled",
                        )
                    if deadline is not None and _time.monotonic() >= deadline:
                        token.cancel(f"timed out after {timeout_s}s")
                        _fire_canceller(canceller, target)
                        raise ToolCancelled(
                            getattr(target, "__name__", "tool"),
                            f"timed out after {timeout_s}s",
                        )
            finally:
                if registry is not None:
                    registry.unregister(token)

        return _wrapper  # type: ignore[return-value]

    if fn is not None:
        # Bare @cancellable form
        return _decorate(fn)
    return _decorate


def _fire_canceller(canceller: Callable[[], None] | None, target: Any) -> None:
    """Invoke the user's canceller callback, logging (not raising) failures.

    A broken canceller must not mask the :class:`ToolCancelled` the agent
    loop needs to see — the cancellation already happened; the callback is
    best-effort cleanup of the remote side.

    :param canceller: The zero-arg interrupt callback, or ``None``.
    :param target: The wrapped tool function (for the log message).
    """
    if canceller is None:
        return
    try:
        canceller()
    except Exception:
        logger.exception(
            "canceller for %s raised — remote work may still be running",
            getattr(target, "__name__", "tool"),
        )

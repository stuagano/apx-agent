"""Bridge sync agent handlers onto the asyncio event loop without blocking it.

Why this exists
---------------

``mlflow.genai.agent_server.AgentServer`` dispatches registered handlers
from ``async def`` routes like this::

    if inspect.iscoroutinefunction(func):
        result = await func(request)
    else:
        result = func(request)          # sync call ON the event loop

and for streaming::

    for chunk in func(request):         # sync generator iterated ON the loop
        yield ...

A sync handler therefore **blocks the entire event loop** for its full
duration — measured: a concurrent request waited 66 s behind a 60 s tool
call. One slow tool stalls every session on the replica, including the
dev UI and health checks.

The fix is on our side: register *async* handlers that move the sync work
to a worker thread. ``AgentServer`` awaits coroutine functions and
async-iterates async generators, so the loop stays free.

Streaming has a subtlety: MLflow/OpenTelemetry span context is managed
*inside* the compiled generator via contextvars. Resuming the generator
on a different pool thread per ``next()`` breaks attach/detach pairing
("token was created in a different Context"). So the stream bridge runs
the WHOLE sync generator on one dedicated thread and hands chunks to the
event loop through a queue.

Usage (this is what the Apps scaffold's ``start_server.py`` generates)::

    from apx_agent import make_async_invoke, make_async_stream

    @invoke()
    async def non_streaming(request):
        return await make_async_invoke(_invoke_fn)(request)

    streaming = stream()(make_async_stream(_stream_fn))
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, AsyncGenerator, Callable, Generator

logger = logging.getLogger(__name__)

# Bounded handoff queue for the stream bridge: big enough that the worker
# rarely waits on a healthy consumer, small enough to apply backpressure
# instead of buffering an unbounded response in memory.
_STREAM_QUEUE_MAXSIZE = 256

# Queue sentinel marking normal generator exhaustion. An object() identity
# token can't collide with any chunk value.
_DONE = object()

# How often a blocked _put re-checks whether the consumer has gone away. Small
# enough that a disconnect frees the worker promptly, large enough to avoid busy
# spinning while it waits for a healthy consumer to drain a full queue.
_PUT_POLL_INTERVAL_S = 0.1


def make_async_invoke(
    invoke_fn: Callable[[Any], Any],
) -> Callable[[Any], Any]:
    """Wrap a sync invoke handler as a coroutine function.

    ``AgentServer`` sees a coroutine function and awaits it; the sync
    agent work runs in the default executor thread pool, leaving the
    event loop free to serve other requests.

    :param invoke_fn: The sync handler, e.g. the ``non_streaming``
        function from :func:`~apx_agent._responses_agent.compile_to_responses_agent`.
    :returns: ``async def handler(request)`` returning ``invoke_fn``'s
        result unchanged.
    """

    async def _async_invoke(request: Any) -> Any:
        return await asyncio.to_thread(invoke_fn, request)

    # Preserve the name AgentServer logs for the handler.
    _async_invoke.__name__ = getattr(invoke_fn, "__name__", "non_streaming")
    return _async_invoke


def make_async_stream(
    stream_fn: Callable[[Any], Generator[Any, None, None]],
) -> Callable[[Any], AsyncGenerator[Any, None]]:
    """Wrap a sync generator handler as an async generator function.

    The sync generator runs to completion on ONE dedicated worker thread
    (keeping MLflow/OTel span context-attach and -detach on the same
    thread); chunks cross to the event loop through a bounded queue, so
    slow consumers apply backpressure instead of buffering the response.

    Exceptions raised inside the sync generator are re-raised from the
    async generator on the event loop — ``AgentServer``'s error handling
    sees them exactly as it would from a plain sync handler.

    :param stream_fn: The sync generator handler, e.g. the ``streaming``
        function from :func:`~apx_agent._responses_agent.compile_to_responses_agent`.
    :returns: ``async def handler(request)`` yielding the same chunks.
    """

    async def _async_stream(request: Any) -> AsyncGenerator[Any, None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        # Set when the consumer abandons the stream (client disconnect) so
        # the worker stops pulling chunks instead of producing into a void.
        consumer_gone = threading.Event()

        def _put(item: Any) -> bool:
            """Hand one item to the loop, honoring queue backpressure.

            ``put_nowait`` would drop backpressure; a plain blocking put
            can't work on an asyncio.Queue from a thread. Schedule the
            put on the loop and wait for it from the worker thread — but
            re-check ``consumer_gone`` while waiting so a client disconnect on a
            FULL queue frees the worker instead of blocking it forever (#379).

            Returns ``True`` if delivered, ``False`` if the consumer went away.
            """
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not consumer_gone.is_set():
                try:
                    future.result(timeout=_PUT_POLL_INTERVAL_S)
                    return True
                except concurrent.futures.TimeoutError:
                    continue
            future.cancel()  # consumer abandoned the stream — don't wait to enqueue
            return False

        def _worker() -> None:
            gen = stream_fn(request)
            try:
                for chunk in gen:
                    # Stop as soon as the consumer disconnects — either detected
                    # up front or surfaced by _put returning False on a full queue.
                    if consumer_gone.is_set() or not _put(chunk):
                        logger.debug("stream consumer gone — stopping worker early")
                        return
                _put(_DONE)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the loop
                try:
                    _put(exc)
                except Exception:
                    # Loop already closed (shutdown race) — nothing to tell.
                    logger.debug("could not deliver stream error; loop closed")
            finally:
                # Close the compiled generator so its DB connections, tool handles
                # and OTel span context are released even on early disconnect.
                close = getattr(gen, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        logger.debug("stream generator close failed", exc_info=True)

        worker = threading.Thread(
            target=_worker,
            name=f"stream-bridge-{getattr(stream_fn, '__name__', 'stream')}",
            daemon=True,
        )
        worker.start()

        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            consumer_gone.set()

    _async_stream.__name__ = getattr(stream_fn, "__name__", "streaming")
    return _async_stream

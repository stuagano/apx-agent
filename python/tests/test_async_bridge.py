"""Tests for _async_bridge.py — sync handlers off the event loop.

Covers:
  1. make_async_invoke: result pass-through, exception propagation, and
     the headline claim — a slow sync handler does NOT block the event
     loop (event-gated concurrency proof, no sleeps-and-hope).
  2. make_async_stream: chunk order, exception propagation, single-thread
     execution of the sync generator (the MLflow/OTel context invariant),
     loop liveness while the generator is mid-chunk, and early consumer
     abandonment stopping the worker.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Any, Generator

import pytest

from apx_agent._async_bridge import make_async_invoke, make_async_stream

# Generous timeout — only hit when the code under test deadlocks.
_WAIT_S = 5.0


# ---------------------------------------------------------------------------
# make_async_invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_passes_result_through():
    handler = make_async_invoke(lambda request: {"echo": request})
    assert await handler({"x": 1}) == {"echo": {"x": 1}}


@pytest.mark.asyncio
async def test_invoke_propagates_exceptions():
    def boom(request: Any) -> Any:
        raise ValueError("inner failure")

    handler = make_async_invoke(boom)
    # The ORIGINAL exception must surface so AgentServer's error handling
    # (HTTP 500 with the message) behaves exactly as with a sync handler.
    with pytest.raises(ValueError, match="inner failure"):
        await handler({})


@pytest.mark.asyncio
async def test_invoke_is_coroutine_function():
    import inspect

    # AgentServer dispatches on iscoroutinefunction — if this is False the
    # handler runs sync on the loop and the whole fix is void.
    assert inspect.iscoroutinefunction(make_async_invoke(lambda r: r))


@pytest.mark.asyncio
async def test_slow_invoke_does_not_block_event_loop():
    """The headline: another coroutine runs WHILE the sync handler blocks."""
    started = threading.Event()
    release = threading.Event()

    def slow_handler(request: Any) -> str:
        started.set()                # signal: handler is running on a worker
        release.wait(_WAIT_S)        # block until the test releases it
        return "slow done"

    handler = make_async_invoke(slow_handler)
    slow_task = asyncio.create_task(handler({}))

    # GATE: the handler must actually be blocking before we test liveness.
    assert await asyncio.to_thread(started.wait, _WAIT_S), "handler never started"

    # Liveness probe: with the old sync registration this coroutine could
    # not run until the handler returned (the 66s serialization bug).
    probe_ran = asyncio.Event()

    async def probe() -> None:
        probe_ran.set()

    await asyncio.wait_for(asyncio.create_task(probe()), timeout=1.0)
    assert probe_ran.is_set(), "event loop was blocked by the sync handler"

    release.set()
    assert await asyncio.wait_for(slow_task, timeout=_WAIT_S) == "slow done"


# ---------------------------------------------------------------------------
# make_async_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_chunks_in_order():
    def gen(request: Any) -> Generator[int, None, None]:
        yield from range(5)

    handler = make_async_stream(gen)
    chunks = [c async for c in handler({})]
    # Order and completeness — a queue bug would drop or reorder chunks.
    assert chunks == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_stream_propagates_generator_exception():
    def gen(request: Any) -> Generator[int, None, None]:
        yield 1
        raise RuntimeError("mid-stream failure")

    handler = make_async_stream(gen)
    received: list[int] = []
    with pytest.raises(RuntimeError, match="mid-stream failure"):
        async for chunk in handler({}):
            received.append(chunk)
    # Chunks before the failure must still have been delivered.
    assert received == [1]


@pytest.mark.asyncio
async def test_stream_runs_generator_on_single_thread():
    """The MLflow/OTel invariant: every resume of the sync generator must
    happen on the SAME thread, or span context attach/detach pairs break."""
    thread_ids: list[int] = []

    def gen(request: Any) -> Generator[int, None, None]:
        for i in range(10):
            thread_ids.append(threading.get_ident())
            yield i

    handler = make_async_stream(gen)
    _ = [c async for c in handler({})]
    # 10 resumes, exactly one distinct thread id. Two or more would mean
    # per-chunk thread hopping — the bug this bridge exists to avoid.
    assert len(thread_ids) == 10
    assert len(set(thread_ids)) == 1
    # And it is NOT the event-loop thread.
    assert thread_ids[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_stream_does_not_block_event_loop_between_chunks():
    """Loop stays live while the sync generator is stuck inside a chunk."""
    in_chunk = threading.Event()
    release = threading.Event()

    def gen(request: Any) -> Generator[str, None, None]:
        yield "first"
        in_chunk.set()              # signal: now blocking mid-generation
        release.wait(_WAIT_S)       # simulates a 60s tool call
        yield "second"

    handler = make_async_stream(gen)
    agen = handler({})
    assert await agen.__anext__() == "first"

    # GATE: the generator is genuinely blocked inside its second chunk.
    assert await asyncio.to_thread(in_chunk.wait, _WAIT_S)

    # Liveness probe — with AgentServer's sync-for iteration this would
    # hang until release (the dev-UI/other-session stall).
    probe_done = asyncio.Event()

    async def probe() -> None:
        probe_done.set()

    await asyncio.wait_for(asyncio.create_task(probe()), timeout=1.0)
    assert probe_done.is_set(), "event loop was blocked between chunks"

    release.set()
    assert await asyncio.wait_for(agen.__anext__(), timeout=_WAIT_S) == "second"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=_WAIT_S)


@pytest.mark.asyncio
async def test_stream_worker_stops_when_consumer_abandons():
    """Closing the async generator early must stop the worker pulling
    chunks — a disconnected client must not drive a 10k-chunk generator
    to completion in the background."""
    produced: list[int] = []
    stopped = threading.Event()

    def gen(request: Any) -> Generator[int, None, None]:
        try:
            for i in range(10_000):
                produced.append(i)
                yield i
        finally:
            stopped.set()

    handler = make_async_stream(gen)
    agen = handler({})
    # Consume a couple of chunks, then abandon the stream.
    assert await agen.__anext__() == 0
    assert await agen.__anext__() == 1
    await agen.aclose()

    # The worker must observe consumer_gone and stop early. Event-gated:
    # wait for the generator's finally block, then check how far it got.
    assert await asyncio.to_thread(stopped.wait, _WAIT_S), "worker never stopped"
    # Strictly fewer than all chunks — the early-stop actually fired.
    # (A small overrun is expected: chunks already queued plus one
    # in-flight put when consumer_gone is set.)
    assert len(produced) < 10_000


# ContextVar the test sets on the event loop and reads inside the worker. Stands
# in for mlflow's request-headers ContextVar, which carries the OBO
# X-Forwarded-Access-Token the served agent needs.
_probe_ctxvar: contextvars.ContextVar[str] = contextvars.ContextVar(
    "apx_test_probe", default="MISSING"
)


@pytest.mark.asyncio
async def test_stream_propagates_contextvars_to_worker():
    """Request-scoped ContextVars set on the event loop MUST be visible inside
    the worker thread. mlflow stashes the request headers (and thus the OBO
    X-Forwarded-Access-Token) in a ContextVar set on the loop before the handler
    runs; a raw threading.Thread starts with an EMPTY context, so without
    copy_context the sync generator reads the default and apx fails closed on the
    streaming endpoint — the deployed agent returns no data."""
    _probe_ctxvar.set("token-abc")
    seen: list[str] = []

    def gen(request: Any) -> Generator[int, None, None]:
        seen.append(_probe_ctxvar.get())
        yield 1

    handler = make_async_stream(gen)
    _ = [c async for c in handler({})]
    assert seen == ["token-abc"], f"contextvar lost crossing into worker: {seen}"


@pytest.mark.asyncio
async def test_stream_is_asyncgen_function():
    import inspect

    # AgentServer dispatches on isasyncgenfunction — False would mean the
    # sync-for path runs the generator on the loop (the bug).
    assert inspect.isasyncgenfunction(make_async_stream(lambda r: iter([])))

"""Claim-vs-reality: the stream worker must exit on client disconnect (#379).

make_async_stream's worker delivers chunks to the loop via _put, which blocks in
``future.result()`` when the handoff queue (maxsize 256) is full. ``consumer_gone``
was only checked at the top of the producer for-loop, never while blocked in
_put. So when a client disconnected mid-stream with the queue full, the worker
hung forever holding the compiled generator (DB connections, tool handles,
unclosed OTel span context) open — leaking a daemon thread + connections per
disconnect until the replica exhausted its pool. (This bridge is what the #353
serving fix routes streaming through.)

This reconciles the *claim* ("the worker stops when the consumer goes away")
against reality: after the client disconnects on a full queue, the worker thread
terminates promptly instead of blocking in _put.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

import apx_agent._async_bridge as ab
from apx_agent._async_bridge import make_async_stream


def _bridge_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("stream-bridge-")]


@pytest.mark.unit
def test_worker_exits_when_consumer_disconnects_on_full_queue(monkeypatch) -> None:
    # raising=False so the discriminator is the thread-hang assertion below, not
    # the presence of the constant (which the pre-fix code lacks).
    monkeypatch.setattr(ab, "_PUT_POLL_INTERVAL_S", 0.01, raising=False)  # notice disconnect fast

    def _gen(_req: Any):
        # Way more than the 256-item queue, so the worker fills it and blocks in
        # _put while the consumer (below) drains only a single item.
        for i in range(10_000):
            yield i

    async def _drive() -> None:
        astream = make_async_stream(_gen)(None)
        await astream.__anext__()  # start the worker + take one item
        await asyncio.sleep(0.1)  # let it race ahead and block on the full queue
        await astream.aclose()  # client disconnect → consumer_gone

    asyncio.run(_drive())

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _bridge_threads():
        time.sleep(0.02)
    assert not _bridge_threads(), (
        "stream-bridge worker hung in _put after the consumer disconnected"
    )

"""Claim-vs-reality: served routes must not block the event loop (#353).

``/invocations``, ``/responses`` and the A2A ``message/send`` handler are
``async def`` but run a synchronous agent turn (``chat_agent.predict`` /
``predict_stream`` / ``_invoke_fn``). Calling that sync work *directly* on the
event loop freezes the whole replica for the turn's duration — a concurrent
request, the dev UI, and the ``/health`` probe all stall behind one slow tool
call (measured 66s in ``_async_bridge``'s own notes).

A functional "the route returns 200" test passes even on the blocking code.
This reconciles the *claim* (the server handles concurrent requests) against
*reality* with a ``threading.Barrier(2)``: the stubbed agent turn blocks until
**two** turns are in flight at once. If the handler offloads to a worker thread
(the fix), two concurrent requests both reach the barrier, it releases, both
return 200. If the handler runs the turn on the loop (the bug), the first turn
blocks the loop, the second request never starts, the barrier times out, and
the requests fail — deterministic RED, no wall-clock threshold.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apx_agent import AgentConfig, LlmAgent, create_app

# The barrier releases only when both concurrent turns arrive. Kept short so a
# genuine block fails fast; the thread join is given generous headroom so a
# *slow* (but non-blocking) machine doesn't false-RED.
_BARRIER_TIMEOUT = 4.0
_JOIN_TIMEOUT = 20.0


def _trivial_tool(query: str) -> str:
    """Echo the query back."""
    return f"got: {query}"


def _chat_response() -> Any:
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

    return ChatAgentResponse(
        messages=[ChatAgentMessage(role="assistant", content="ok", id="m1")]
    )


def _chat_chunk() -> Any:
    from mlflow.types.agent import ChatAgentChunk, ChatAgentMessage

    return ChatAgentChunk(delta=ChatAgentMessage(role="assistant", content="ok", id="m1"))


@pytest.fixture
def barrier_app():
    """create_app whose every ChatAgent blocks predict/predict_stream on a shared
    barrier, so a test can prove two turns run concurrently (off the loop)."""
    agent = LlmAgent(tools=[_trivial_tool])
    config = AgentConfig(name="test-agent", model="databricks-claude-sonnet-4-6")
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

    def _blocking_predict(*_a: Any, **_k: Any) -> Any:
        barrier.wait()
        return _chat_response()

    def _blocking_predict_stream(*_a: Any, **_k: Any) -> Any:
        barrier.wait()
        return iter([_chat_chunk()])

    from apx_agent import _chat_agent as _ca_module

    original_factory = _ca_module.chat_agent_for

    def _spy_factory(agent_arg: Any, *, model: Any, conversation_store=None, agent_id=None, checkpointer=None) -> Any:
        ca = original_factory(agent_arg, model=model)
        # Stub EVERY built agent (the /invocations mount and the A2A mount each
        # build their own) so all served paths share the one barrier.
        ca.predict = MagicMock(side_effect=_blocking_predict)
        ca.predict_stream = MagicMock(side_effect=_blocking_predict_stream)
        return ca

    with patch.object(_ca_module, "chat_agent_for", side_effect=_spy_factory), patch(
        "apx_agent._wiring._make_workspace_client"
    ) as mock_ws:
        mock_ws.return_value = MagicMock(name="ws")
        # Patches must stay live while requests run, so yield inside the context.
        yield create_app(agent, config=config)


def _fire_two(app: Any, do_post: Callable[[httpx.AsyncClient], Awaitable[int]]) -> list[Any]:
    """Fire two do_post() requests concurrently on ONE event loop; return both
    outcomes (status code, or the raised exception).

    ``asyncio.gather`` on a single loop puts both handler coroutines genuinely
    in flight at once — the production condition. If a handler runs its turn on
    the loop (the bug), the first turn's blocking work freezes this loop and the
    second request can't advance, so the barrier times out (BrokenBarrierError).
    The manual lifespan mounts the protocol routes on this same loop.
    """

    async def _main() -> list[Any]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                return await asyncio.gather(do_post(ac), do_post(ac), return_exceptions=True)

    return asyncio.run(_main())


# Success is asserted on turn *semantics*, not HTTP status: the stream generator
# and the A2A handler both catch a failed turn and still return 200 (an error SSE
# frame / a failed Task), so a status-only check false-passes when the barrier
# breaks. Each _post returns True only if the turn genuinely completed.


@pytest.mark.unit
def test_invocations_nonstream_serves_two_turns_concurrently(barrier_app) -> None:
    async def _post(ac: httpx.AsyncClient) -> bool:
        r = await ac.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        )
        msgs = r.json().get("messages") if r.status_code == 200 else None
        return bool(msgs) and msgs[0].get("content") == "ok"

    a, b = _fire_two(barrier_app, _post)
    assert a is True and b is True, f"turns serialized (loop blocked): {a!r}, {b!r}"


@pytest.mark.unit
def test_invocations_stream_serves_two_turns_concurrently(barrier_app) -> None:
    async def _post(ac: httpx.AsyncClient) -> bool:
        r = await ac.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        return r.status_code == 200 and '"content":"ok"' in r.text and '"error"' not in r.text

    a, b = _fire_two(barrier_app, _post)
    assert a is True and b is True, f"stream turns serialized (loop blocked): {a!r}, {b!r}"


@pytest.mark.unit
def test_a2a_message_send_serves_two_turns_concurrently(barrier_app) -> None:
    async def _post(ac: httpx.AsyncClient) -> bool:
        r = await ac.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                        "messageId": "u1",
                    }
                },
            },
        )
        result = r.json().get("result", {}) if r.status_code == 200 else {}
        return result.get("status", {}).get("state") == "completed"

    a, b = _fire_two(barrier_app, _post)
    assert a is True and b is True, f"A2A turns serialized (loop blocked): {a!r}, {b!r}"

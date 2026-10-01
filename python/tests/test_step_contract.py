"""Runtime boundaries for opt-in step policies."""

import asyncio
import json
import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from apx_agent._cancellation import ToolCancelled
from apx_agent._step_contract import (
    StepTimeoutError,
    StepUnavailableError,
    check_unavailable,
    escalation_message,
    get_escalation,
    has_deadline,
    invoke_with_timeout,
    run_sync_tool,
    unavailable_middleware,
)


def test_deadline_and_original_timeout_are_distinct():
    async def slow(state):
        await asyncio.sleep(1)
        return state

    async def own_timeout(state):
        raise TimeoutError("upstream")

    with pytest.raises(StepTimeoutError):
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=slow), {}, 0.01))
    with pytest.raises(TimeoutError, match="upstream") as exc:
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=own_timeout), {}, 1))
    assert type(exc.value) is TimeoutError


def test_sync_worker_does_not_delay_executor_shutdown_and_copies_context():
    released = threading.Event()
    entered = threading.Event()
    identity = ContextVar("step_test_identity", default="missing")
    observed = []

    def hung():
        observed.append(identity.get())
        entered.set()
        released.wait(2)

    async def invoke(state):
        identity.set("caller")
        await asyncio.to_thread(run_sync_tool, hung)
        return state

    start = time.monotonic()
    try:
        with pytest.raises(StepTimeoutError):
            asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=invoke), {}, 0.02))
        assert time.monotonic() - start < 0.5
        assert entered.is_set()
        assert observed == ["caller"]
    finally:
        released.set()


def test_external_cancellation_and_tool_cancellation_survive():
    async def cancel(state):
        raise asyncio.CancelledError

    def cancelled():
        raise ToolCancelled("tool", "governance")

    async def invoke(state):
        await asyncio.to_thread(run_sync_tool, cancelled)
        return state

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=cancel), {}, 1))
    with pytest.raises(ToolCancelled, match="governance"):
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=invoke), {}, 1))


def test_deadline_resets_and_catches_synchronous_overrun():
    async def overrun(state):
        assert has_deadline()
        time.sleep(0.02)
        return state

    async def scenario():
        assert not has_deadline()
        with pytest.raises(StepTimeoutError):
            await invoke_with_timeout(SimpleNamespace(ainvoke=overrun), {}, 0.005)
        assert not has_deadline()
        assert run_sync_tool(lambda value: value, 4) == 4

    asyncio.run(scenario())


def test_nested_deadline_never_extends_parent_budget():
    async def inner(state):
        await asyncio.sleep(1)
        return state

    async def outer(state):
        return await invoke_with_timeout(SimpleNamespace(ainvoke=inner), state, 10)

    started = time.monotonic()
    with pytest.raises(StepTimeoutError):
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=outer), {}, 0.01))
    assert time.monotonic() - started < 0.5


def test_tool_cancellation_after_deadline_does_not_override_timeout():
    def cancelled():
        time.sleep(0.1)
        raise ToolCancelled("tool", "governance")

    async def invoke(state):
        # An error arriving after the supervisor timed out is a discarded result.
        run_sync_tool(cancelled)
        return state

    with pytest.raises(StepTimeoutError):
        asyncio.run(invoke_with_timeout(SimpleNamespace(ainvoke=invoke), {}, 0.005))


def test_unavailable_is_exact_and_safe():
    check_unavailable({"nested": {"availability": "unavailable"}})
    check_unavailable("availability=unavailable")
    with pytest.raises(StepUnavailableError) as exc:
        check_unavailable({"availability": "unavailable", "error": "SECRET"}, "bad secret!")
    assert "SECRET" not in str(exc.value)
    assert exc.value.capability == "agent"


@pytest.mark.parametrize("command", [False, True])
def test_both_middleware_hooks_intercept_structured_tool_failure(command):
    middleware = unavailable_middleware()
    request = SimpleNamespace(tool_call={"name": "lookup"})
    message = ToolMessage(content=json.dumps({"availability": "unavailable"}), tool_call_id="1")
    result = Command(update={"messages": [message]}) if command else message

    async def handler(request):
        return result

    with pytest.raises(StepUnavailableError):
        middleware.wrap_tool_call(request, lambda request: result)
    with pytest.raises(StepUnavailableError):
        asyncio.run(middleware.awrap_tool_call(request, handler))


def test_packet_round_trip_and_strict_marked_validation():
    payload = dict(status="escalated", availability="unavailable", capability="lookup",
                   error="Step unavailable", reason="unavailable", failed_step=["chain", "lookup"],
                   evidence=[{"step": ["chain", "before"], "output_key": "before", "data": {"count": 2}}])
    message = escalation_message(payload)
    assert get_escalation(message) == payload
    assert json.loads(message.content) == payload
    message.additional_kwargs["apx_data_parts"][0]["data"]["failed_step"] = "wrong"
    with pytest.raises(ValueError, match="Invalid sequential escalation"):
        get_escalation(message)


@pytest.mark.parametrize("evidence", [[{"data": {}}], [{"step": "bad", "output_key": None, "data": {}}]])
def test_malformed_evidence_is_rejected(evidence):
    with pytest.raises(ValueError):
        escalation_message(dict(capability="lookup", error="Unavailable", reason="unavailable",
                                failed_step=["lookup"], evidence=evidence))


def test_content_blocks_use_safe_declared_capability():
    middleware = unavailable_middleware()
    request = SimpleNamespace(tool_call={"name": "lookup"})
    message = ToolMessage(content=[{"type": "text", "text": json.dumps({
        "availability": "unavailable", "capability": "genie", "error": "secret",
    })}], tool_call_id="1")
    with pytest.raises(StepUnavailableError) as exc:
        middleware.wrap_tool_call(request, lambda request: message)
    assert exc.value.capability == "genie"
    with pytest.raises(StepUnavailableError) as exc:
        check_unavailable({"availability": "unavailable", "capability": "SECRET token"}, "lookup")
    assert exc.value.capability == "lookup"

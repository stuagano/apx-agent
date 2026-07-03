"""Claim-vs-reality: a configured watchdog that errors must fail CLOSED (#371).

`WatchdogClient.evaluate` returned `action="allow"` on any transport failure —
and the MCP transport itself swallowed every exception into `{"action":"allow"}`,
including the `RuntimeError` `asyncio.run` raises when invoked inside a running
event loop. So a misconfigured/unreachable governance endpoint silently
permitted every gated operation with no violation recorded. The docstring
promised "the runtime decides its own fail-open vs fail-closed policy on top",
but no runtime layer implemented a closed path.

This reconciles the *claim* ("governance is enforced") against reality: a
transport error (or a non-decision response) now yields `reject` by default,
with an explicit `fail_closed=False` opt-out; and the loop-safe runner lets the
transport work from inside a running loop instead of erroring into allow.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from apx_agent._watchdog import WatchdogClient, _run_coro_blocking


def _boom(_req: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("watchdog unreachable")


@pytest.mark.unit
def test_transport_error_fails_closed_by_default() -> None:
    client = WatchdogClient(transport=_boom)
    decision = client.evaluate(operation="tool_call", context={})
    assert decision.action == "reject", "a configured watchdog that errors must fail closed"


@pytest.mark.unit
def test_fail_closed_false_restores_allow() -> None:
    client = WatchdogClient(transport=_boom, fail_closed=False)
    decision = client.evaluate(operation="tool_call", context={})
    assert decision.action == "allow", "the fail_closed=False escape hatch must restore fail-open"


@pytest.mark.unit
def test_non_decision_response_fails_closed() -> None:
    # A transport that returns something that isn't a decision dict is not a
    # clear allow — treat it as closed by default.
    client = WatchdogClient(transport=lambda _req: "not a dict")
    assert client.evaluate(operation="tool_call", context={}).action == "reject"


@pytest.mark.unit
def test_noop_transport_still_allows() -> None:
    # No configured watchdog (default noop) must NOT start rejecting traffic.
    assert WatchdogClient().evaluate(operation="tool_call", context={}).action == "allow"


@pytest.mark.unit
def test_run_coro_blocking_works_inside_running_loop() -> None:
    # The MCP transport runs an async call via this helper; a naive asyncio.run
    # would raise RuntimeError inside a running loop and get misclassified as a
    # watchdog outage. The helper must run the coroutine off the active loop.
    async def _driver() -> str:
        return _run_coro_blocking(lambda: asyncio.sleep(0, result="ok"), timeout=5.0)

    assert asyncio.run(_driver()) == "ok"

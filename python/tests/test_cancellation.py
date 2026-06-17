"""Tests for _cancellation.py — cancellable tools + watchdog kill switch.

Covers:
  1. CancelToken semantics: one-shot, first-reason-wins, wait().
  2. CancellationRegistry: register/unregister lifecycle, cancel_all count.
  3. cancellable() wrapper: pass-through results, exception propagation,
     mid-flight cancellation (deterministic, event-gated), timeout,
     canceller invocation, cooperative token injection, @tool stacking.
  4. WatchdogGuard(cancel_registry=...): a reject decision cancels
     in-flight tool calls; an allow decision leaves them running.

Concurrency tests use explicit threading.Event gates: the tool signals
when it has started (so the test KNOWS it's in-flight before cancelling)
and blocks on a release event the test controls. No sleeps-and-hope.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from apx_agent._cancellation import (
    CancellationRegistry,
    CancelToken,
    ToolCancelled,
    cancellable,
)

# Generous join timeout — only hit when the code under test deadlocks.
_JOIN_TIMEOUT_S = 5.0
# Fast supervisor poll for tests: bounds cancellation latency at ~20 ms.
_POLL_S = 0.02


# ---------------------------------------------------------------------------
# CancelToken
# ---------------------------------------------------------------------------


def test_token_starts_uncancelled():
    token = CancelToken()
    assert token.is_cancelled is False
    assert token.reason is None


def test_token_cancel_is_one_shot_first_reason_wins():
    token = CancelToken()
    token.cancel("first")
    token.cancel("second")
    assert token.is_cancelled is True
    # First reason must win — a later cancel (e.g. timeout racing a
    # watchdog kill) must not overwrite why the call actually died.
    assert token.reason == "first"


def test_token_wait_returns_on_cancel():
    token = CancelToken()
    token.cancel("go")
    # Already-cancelled token returns immediately.
    assert token.wait(timeout=0.01) is True


def test_token_wait_times_out_when_uncancelled():
    token = CancelToken()
    assert token.wait(timeout=0.01) is False


# ---------------------------------------------------------------------------
# CancellationRegistry
# ---------------------------------------------------------------------------


def test_registry_tracks_and_releases_tokens():
    reg = CancellationRegistry()
    t1, t2 = CancelToken(), CancelToken()
    reg.register(t1)
    reg.register(t2)
    assert reg.active_count == 2
    reg.unregister(t1)
    assert reg.active_count == 1
    # Unknown / double unregister must be a no-op, not an error.
    reg.unregister(t1)
    assert reg.active_count == 1


def test_registry_cancel_all_counts_only_newly_cancelled():
    reg = CancellationRegistry()
    t1, t2, t3 = CancelToken(), CancelToken(), CancelToken()
    for t in (t1, t2, t3):
        reg.register(t)
    t1.cancel("already dead")
    # 2 = t2 + t3; t1 was already cancelled and must not be re-counted.
    assert reg.cancel_all("kill switch") == 2
    assert t2.reason == "kill switch"
    assert t3.reason == "kill switch"
    assert t1.reason == "already dead"


def test_registry_cancel_all_empty_is_zero():
    assert CancellationRegistry().cancel_all("nothing running") == 0


# ---------------------------------------------------------------------------
# cancellable() wrapper
# ---------------------------------------------------------------------------


def test_cancellable_passes_through_result_and_unregisters():
    reg = CancellationRegistry()

    @cancellable(registry=reg, poll_interval_s=_POLL_S)
    def add(a: int, b: int) -> int:
        return a + b

    # Result must round-trip through the worker thread unchanged.
    assert add(2, 3) == 5
    # The call's token must be released after completion — leaked tokens
    # would make cancel_all kill calls that already finished.
    assert reg.active_count == 0


def test_cancellable_propagates_tool_exceptions():
    @cancellable(poll_interval_s=_POLL_S)
    def boom() -> None:
        raise ValueError("inner failure")

    # The ORIGINAL exception type must surface (not ToolCancelled, not a
    # wrapper) so existing error handling in the agent loop still matches.
    with pytest.raises(ValueError, match="inner failure"):
        boom()


def test_cancellable_cancel_mid_flight():
    """Deterministic mid-flight cancel: gate on started, cancel, assert."""
    reg = CancellationRegistry()
    started = threading.Event()
    release = threading.Event()

    @cancellable(registry=reg, poll_interval_s=_POLL_S)
    def long_tool() -> str:
        started.set()              # signal: we are in-flight
        release.wait(_JOIN_TIMEOUT_S)  # block until the test releases us
        return "finished"

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = long_tool()
        except BaseException as exc:  # noqa: BLE001 — captured for assertion
            holder["error"] = exc

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    # GATE: the tool must actually be running before we cancel — without
    # this the test could cancel before registration (testing nothing).
    assert started.wait(_JOIN_TIMEOUT_S), "tool never started"

    cancelled = reg.cancel_all("operator kill")
    # Exactly the one in-flight call must have been cancelled.
    assert cancelled == 1

    caller.join(_JOIN_TIMEOUT_S)
    assert not caller.is_alive(), "caller thread did not return after cancel"
    release.set()  # let the orphaned worker thread finish cleanly

    err = holder.get("error")
    assert isinstance(err, ToolCancelled), f"expected ToolCancelled, got {holder}"
    # The reason must carry through so the LLM/user learns WHY it died.
    assert err.reason == "operator kill"
    assert err.tool_name == "long_tool"
    # Token released even on the cancel path.
    assert reg.active_count == 0


def test_cancellable_timeout():
    release = threading.Event()

    @cancellable(timeout_s=0.1, poll_interval_s=_POLL_S)
    def stuck() -> str:
        release.wait(_JOIN_TIMEOUT_S)
        return "never"

    with pytest.raises(ToolCancelled) as exc_info:
        stuck()
    release.set()
    # The timeout must be identifiable in the reason.
    assert "timed out" in exc_info.value.reason


def test_canceller_invoked_on_cancel():
    """The canceller callback fires so remote work (SQL stmt, HTTP) can
    actually be interrupted, not just abandoned."""
    reg = CancellationRegistry()
    started = threading.Event()
    release = threading.Event()
    canceller_calls: list[bool] = []

    def interrupt_remote() -> None:
        canceller_calls.append(True)
        release.set()  # the "remote interrupt" actually unblocks the work

    @cancellable(registry=reg, canceller=interrupt_remote, poll_interval_s=_POLL_S)
    def remote_work() -> str:
        started.set()
        release.wait(_JOIN_TIMEOUT_S)
        return "done"

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = remote_work()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    assert started.wait(_JOIN_TIMEOUT_S)

    reg.cancel_all("violation")
    caller.join(_JOIN_TIMEOUT_S)

    assert isinstance(holder.get("error"), ToolCancelled)
    # Canceller ran exactly once — double-firing could double-cancel a
    # remote statement or double-close a connection.
    assert canceller_calls == [True]


def test_canceller_failure_does_not_mask_toolcancelled():
    started = threading.Event()
    release = threading.Event()
    reg = CancellationRegistry()

    def broken_canceller() -> None:
        raise RuntimeError("canceller exploded")

    @cancellable(registry=reg, canceller=broken_canceller, poll_interval_s=_POLL_S)
    def work() -> str:
        started.set()
        release.wait(_JOIN_TIMEOUT_S)
        return "done"

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = work()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    assert started.wait(_JOIN_TIMEOUT_S)
    reg.cancel_all("kill")
    caller.join(_JOIN_TIMEOUT_S)
    release.set()

    # The agent loop must still see ToolCancelled — a broken cleanup
    # callback must not swallow the cancellation signal.
    assert isinstance(holder.get("error"), ToolCancelled)


def test_cooperative_mode_injects_token():
    seen: list[Any] = []

    @cancellable(poll_interval_s=_POLL_S)
    def coop(x: int, cancel_token: CancelToken | None = None) -> int:
        seen.append(cancel_token)
        return x * 2

    assert coop(4) == 8
    # A real CancelToken must have been injected for the cooperative
    # early-exit pattern to be possible at all.
    assert len(seen) == 1
    assert isinstance(seen[0], CancelToken)


def test_stacks_under_tool_decorator():
    from apx_agent import get_tool_metadata, tool

    reg = CancellationRegistry()

    @tool
    @cancellable(registry=reg, poll_interval_s=_POLL_S)
    def lookup(account_id: str) -> str:
        """Look up an account by ID."""
        return f"account {account_id}"

    # @tool metadata attaches to the wrapper; name/doc survive wraps().
    assert get_tool_metadata(lookup) is not None
    assert lookup.__name__ == "lookup"
    assert "Look up an account" in lookup.__doc__
    # And the call still works end-to-end through both decorators.
    assert lookup("a-1") == "account a-1"


# ---------------------------------------------------------------------------
# WatchdogGuard kill switch
# ---------------------------------------------------------------------------


def _scripted_watchdog(action: str, reason: str | None = None):
    """A WatchdogClient whose transport always returns the given action."""
    from apx_agent import WatchdogClient

    def transport(request: dict[str, Any]) -> dict[str, Any]:
        if request.get("type") == "violation_report":
            return {}
        out: dict[str, Any] = {"action": action}
        if reason:
            out["reason"] = reason
        return out

    return WatchdogClient(transport=transport)


def test_watchdog_reject_cancels_inflight_tool():
    """The headline integration: a violation on ANY hook kills running tools."""
    from apx_agent import WatchdogGuard

    reg = CancellationRegistry()
    started = threading.Event()
    release = threading.Event()

    @cancellable(registry=reg, poll_interval_s=_POLL_S)
    def long_extract() -> str:
        started.set()
        release.wait(_JOIN_TIMEOUT_S)
        return "extracted"

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = long_extract()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    # GATE: extraction is genuinely in-flight before the violation fires.
    assert started.wait(_JOIN_TIMEOUT_S)

    guard = WatchdogGuard(
        _scripted_watchdog("reject", reason="PII detected"),
        agent_name="t",
        cancel_registry=reg,
    )
    # The violation fires on a DIFFERENT tool's before_tool hook…
    with pytest.raises(PermissionError, match="PII detected"):
        guard.for_tool()("unrelated_tool", {"x": 1})

    # …and the in-flight extraction must die with the violation reason.
    caller.join(_JOIN_TIMEOUT_S)
    release.set()
    err = holder.get("error")
    assert isinstance(err, ToolCancelled), f"in-flight tool survived: {holder}"
    assert "Watchdog violation" in err.reason
    assert "PII detected" in err.reason


def test_watchdog_allow_leaves_inflight_running():
    from apx_agent import WatchdogGuard

    reg = CancellationRegistry()
    started = threading.Event()
    release = threading.Event()

    @cancellable(registry=reg, poll_interval_s=_POLL_S)
    def long_tool() -> str:
        started.set()
        release.wait(_JOIN_TIMEOUT_S)
        return "finished"

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = long_tool()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    assert started.wait(_JOIN_TIMEOUT_S)

    guard = WatchdogGuard(
        _scripted_watchdog("allow"), agent_name="t", cancel_registry=reg,
    )
    # Allow decision: no exception, and crucially NO cancellation.
    guard.for_tool()("some_tool", {})
    assert reg.active_count == 1, "allow decision must not cancel in-flight work"

    # Release and confirm the tool completes normally.
    release.set()
    caller.join(_JOIN_TIMEOUT_S)
    assert holder.get("result") == "finished"


def test_watchdog_reject_without_registry_still_raises():
    """No registry wired → reject path unchanged (no crash on the new code)."""
    from apx_agent import WatchdogGuard

    guard = WatchdogGuard(_scripted_watchdog("reject", reason="nope"), agent_name="t")
    with pytest.raises(PermissionError, match="nope"):
        guard.for_tool()("tool", {})

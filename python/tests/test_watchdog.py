"""Tests for the databricks-watchdog integration sketch.

The wire protocol to watchdog isn't finalized — these tests pin the
shape of the apx-agent side via an injected fake transport. When the
real watchdog API arrives, only the transport plumbing changes.

Covers:
  - WatchdogDecision defaults / parsing
  - WatchdogClient.evaluate happy path + transport-failure fail-open
  - WatchdogClient.report_violation shape + failure tolerance
  - WatchdogGuard adapters (for_input / for_output / for_tool / for_model)
    produce callables with the right hook signatures and behavior
  - reject decisions short-circuit; redact rewrites; allow passes through
  - violation reports fire on reject + redact, not on allow
  - emit_agent_metadata produces the expected JSON shape from a real agent
"""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent import (
    Agent,
    WatchdogClient,
    WatchdogDecision,
    WatchdogGuard,
    emit_agent_metadata,
    genie_tool,
    tool,
    uc_function_tool,
    vector_search_tool,
)


# ---------------------------------------------------------------------------
# Test transports
# ---------------------------------------------------------------------------


def _make_transport(responses: dict[str, dict[str, Any]] | None = None) -> Any:
    """Build a transport that returns mapped responses by operation key.

    Records every request in ``transport.calls`` for assertions.

    ``responses`` keys are either operation strings (``"tool_call"``)
    or the literal ``"violation_report"`` for report_violation calls.
    """
    responses = responses or {}

    def _transport(req: dict[str, Any]) -> dict[str, Any]:
        _transport.calls.append(req)  # type: ignore[attr-defined]
        if req.get("type") == "violation_report":
            return responses.get("violation_report", {})
        op = req.get("operation", "")
        return responses.get(op, {"action": "allow"})

    _transport.calls = []  # type: ignore[attr-defined]
    return _transport


# ---------------------------------------------------------------------------
# WatchdogDecision
# ---------------------------------------------------------------------------


def test_decision_defaults_to_allow() -> None:
    d = WatchdogDecision()
    assert d.action == "allow"
    assert d.reason is None
    assert d.metadata == {}


def test_decision_is_frozen() -> None:
    d = WatchdogDecision(action="reject", reason="nope")
    with pytest.raises(Exception):
        d.action = "allow"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WatchdogClient.evaluate
# ---------------------------------------------------------------------------


def test_default_client_allows_everything() -> None:
    client = WatchdogClient()  # no transport — noop allow
    d = client.evaluate(operation="tool_call", context={"tool_name": "x"})
    assert d.action == "allow"


def test_evaluate_parses_transport_response() -> None:
    transport = _make_transport({
        "tool_call": {
            "action": "reject",
            "reason": "PII tool access denied",
            "policy_id": "pii-strict-001",
            "domain": "security",
            "metadata": {"owner": "data-team@x.com"},
        },
    })
    client = WatchdogClient(transport=transport)
    d = client.evaluate(operation="tool_call", context={"tool_name": "leak_pii"})
    assert d.action == "reject"
    assert d.reason == "PII tool access denied"
    assert d.policy_id == "pii-strict-001"
    assert d.domain == "security"
    assert d.metadata == {"owner": "data-team@x.com"}


def test_evaluate_falls_back_to_allow_on_transport_exception() -> None:
    def _broken(_req: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("network down")

    client = WatchdogClient(transport=_broken)
    d = client.evaluate(operation="tool_call")
    assert d.action == "allow"
    assert d.reason is not None
    assert "transport error" in d.reason


def test_evaluate_falls_back_when_transport_returns_non_dict() -> None:
    client = WatchdogClient(transport=lambda _req: "garbage")  # type: ignore[arg-type,return-value]
    d = client.evaluate(operation="tool_call")
    assert d.action == "allow"


def test_evaluate_forwards_operation_and_context() -> None:
    transport = _make_transport()
    client = WatchdogClient(transport=transport)
    client.evaluate(operation="input_message", context={"agent_name": "triage"})
    assert transport.calls[0] == {
        "operation": "input_message",
        "context": {"agent_name": "triage"},
    }


# ---------------------------------------------------------------------------
# WatchdogClient.report_violation
# ---------------------------------------------------------------------------


def test_report_violation_sends_expected_shape() -> None:
    transport = _make_transport()
    client = WatchdogClient(transport=transport)
    decision = WatchdogDecision(
        action="reject", reason="bad", policy_id="p-1", domain="security",
        metadata={"k": "v"},
    )
    client.report_violation(decision, {"tool_name": "leak_pii"})
    assert transport.calls[0]["type"] == "violation_report"
    assert transport.calls[0]["decision"]["policy_id"] == "p-1"
    assert transport.calls[0]["context"] == {"tool_name": "leak_pii"}


def test_report_violation_swallows_transport_errors() -> None:
    def _broken(_req: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("network down")

    client = WatchdogClient(transport=_broken)
    # Should not raise — violation report failures are logged-and-continued
    client.report_violation(WatchdogDecision(action="reject"))


# ---------------------------------------------------------------------------
# WatchdogGuard.for_input
# ---------------------------------------------------------------------------


def test_for_input_allows_when_decision_is_allow() -> None:
    transport = _make_transport()
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")
    callback = guard.for_input()
    assert callback([{"role": "user", "content": "hi"}]) is None


def test_for_input_returns_reason_on_reject_and_reports_violation() -> None:
    transport = _make_transport({
        "input_message": {"action": "reject", "reason": "PII in input"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")
    callback = guard.for_input()
    result = callback([{"role": "user", "content": "my ssn is 123-45-6789"}])
    assert result == "PII in input"
    # First call was evaluate, second call was report_violation
    assert len(transport.calls) == 2
    assert transport.calls[1]["type"] == "violation_report"


def test_for_input_passes_agent_name_in_context() -> None:
    transport = _make_transport()
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")
    guard.for_input()([{"role": "user", "content": "hi"}])
    assert transport.calls[0]["context"]["agent_name"] == "triage"


# ---------------------------------------------------------------------------
# WatchdogGuard.for_output
# ---------------------------------------------------------------------------


def test_for_output_redaction_replaces_text() -> None:
    transport = _make_transport({
        "output_message": {
            "action": "redact",
            "reason": "PII detected",
            "redacted_content": "Redacted content.",
        },
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")
    out = guard.for_output()("Your SSN is 123-45-6789.")
    assert out == "Redacted content."


def test_for_output_redaction_without_content_passes_through() -> None:
    transport = _make_transport({
        "output_message": {"action": "redact", "reason": "x"},  # no redacted_content
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    assert guard.for_output()("hello") is None


def test_for_output_reject_returns_reason() -> None:
    transport = _make_transport({
        "output_message": {"action": "reject", "reason": "policy violation"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    out = guard.for_output()("anything")
    assert out == "policy violation"


# ---------------------------------------------------------------------------
# WatchdogGuard.for_tool
# ---------------------------------------------------------------------------


def test_for_tool_allows_silently() -> None:
    transport = _make_transport()
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    # Should not raise
    guard.for_tool()("classify_intent", {"query": "x"})


def test_for_tool_reject_raises_permission_error() -> None:
    transport = _make_transport({
        "tool_call": {"action": "reject", "reason": "tool denied"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    with pytest.raises(PermissionError, match="tool denied"):
        guard.for_tool()("classify_intent", {"query": "x"})


def test_for_tool_passes_tool_name_in_context() -> None:
    transport = _make_transport()
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    guard.for_tool()("my_tool", {"arg": 1})
    assert transport.calls[0]["context"]["tool_name"] == "my_tool"
    assert transport.calls[0]["context"]["arguments"] == {"arg": 1}


# ---------------------------------------------------------------------------
# WatchdogGuard.for_model
# ---------------------------------------------------------------------------


def test_for_model_allows_silently() -> None:
    transport = _make_transport()
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    guard.for_model()([["msg"]])  # multi-prompt chat shape


def test_for_model_reject_raises_permission_error() -> None:
    transport = _make_transport({
        "model_call": {"action": "reject", "reason": "model budget exceeded"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport))
    with pytest.raises(PermissionError, match="model budget exceeded"):
        guard.for_model()(["prompt"])


# ---------------------------------------------------------------------------
# emit_agent_metadata
# ---------------------------------------------------------------------------


def test_emit_agent_metadata_full_shape() -> None:
    @tool(uc="main.tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify a customer query."""
        return query

    agent = Agent(
        instructions="You triage customer questions.",
        tools=[
            classify_intent,
            uc_function_tool("main.tools.score"),
            genie_tool("space-abc"),
            vector_search_tool("main.search.docs"),
        ],
        sub_agents=["endpoints/billing"],
    )

    meta = emit_agent_metadata(agent, name="triage", model="databricks-claude-sonnet-4-6")

    assert meta["name"] == "triage"
    assert meta["model"] == "databricks-claude-sonnet-4-6"
    assert meta["instructions"] == "You triage customer questions."

    tool_names = {t["name"] for t in meta["tools"]}
    assert "classify_intent" in tool_names
    classify_meta = next(t for t in meta["tools"] if t["name"] == "classify_intent")
    assert classify_meta["uc_name"] == "main.tools.classify_intent"
    assert classify_meta["grants"] == ["agent_consumers"]

    assert "endpoints/billing" in meta["sub_agents"]

    kinds = {r["kind"] for r in meta["resources"]}
    assert {"uc_function", "genie_space", "vector_search_index", "serving_endpoint"}.issubset(kinds)


def test_emit_agent_metadata_minimal_no_model() -> None:
    agent = Agent(tools=[])
    meta = emit_agent_metadata(agent)
    assert meta["name"] is None
    assert meta["model"] is None
    assert meta["tools"] == []
    assert meta["resources"] == []

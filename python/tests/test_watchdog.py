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

from unittest.mock import MagicMock, patch

from apx_agent import (
    Agent,
    WatchdogClient,
    WatchdogDecision,
    WatchdogGuard,
    emit_agent_metadata,
    genie_tool,
    make_uc_violation_writer,
    make_watchdog_transport,
    set_uc_tags_for_agent,
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


# ---------------------------------------------------------------------------
# set_uc_tags_for_agent — UC tags emitter
# ---------------------------------------------------------------------------


def _make_full_agent() -> Agent:
    @tool(uc="main.tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify."""
        return query

    return Agent(
        instructions="...",
        tools=[
            classify_intent,
            uc_function_tool("main.tools.score"),
            genie_tool("space-abc"),
            vector_search_tool("main.search.docs"),
        ],
        sub_agents=["endpoints/billing"],
    )


def test_set_uc_tags_writes_expected_keys() -> None:
    agent = _make_full_agent()
    mlflow_client = MagicMock()

    tags = set_uc_tags_for_agent(
        agent,
        registered_model_name="main.agents.triage",
        model="databricks-claude-sonnet-4-6",
        name="triage",
        mlflow_client=mlflow_client,
    )

    expected_keys = {
        "apx.agent.name", "apx.agent.model", "apx.agent.tool_count",
        "apx.agent.tools", "apx.agent.uc_functions",
        "apx.agent.sub_agents", "apx.agent.resources", "apx.agent.metadata",
    }
    assert expected_keys.issubset(set(tags.keys()))

    # One MlflowClient.set_registered_model_tag call per tag
    assert mlflow_client.set_registered_model_tag.call_count == len(tags)
    written_keys = {c.kwargs["key"] for c in mlflow_client.set_registered_model_tag.call_args_list}
    assert written_keys == set(tags.keys())


def test_set_uc_tags_model_value_propagates() -> None:
    agent = _make_full_agent()
    mlflow_client = MagicMock()
    tags = set_uc_tags_for_agent(
        agent,
        registered_model_name="main.agents.triage",
        model="databricks-claude-sonnet-4-6",
        mlflow_client=mlflow_client,
    )
    assert tags["apx.agent.model"] == "databricks-claude-sonnet-4-6"


def test_set_uc_tags_continues_past_individual_failure() -> None:
    """One tag write failing shouldn't abort the rest."""
    agent = _make_full_agent()
    mlflow_client = MagicMock()
    # Fail every other call
    mlflow_client.set_registered_model_tag.side_effect = [
        None, RuntimeError("fail"), None, None, None, None, None, None,
    ]

    tags = set_uc_tags_for_agent(
        agent,
        registered_model_name="main.agents.triage",
        model="m",
        mlflow_client=mlflow_client,
    )
    # All tag writes attempted even with a failure in the middle
    assert mlflow_client.set_registered_model_tag.call_count == len(tags)


def test_set_uc_tags_truncates_long_metadata() -> None:
    @tool
    def long_doc(query: str) -> str:
        """Very long description.""" + ("x" * 10_000)
        return query

    agent = Agent(tools=[long_doc] * 20, instructions="x" * 50_000)
    mlflow_client = MagicMock()

    tags = set_uc_tags_for_agent(
        agent,
        registered_model_name="main.agents.x",
        mlflow_client=mlflow_client,
    )
    # Every written tag value stays under the UC limit
    for value in tags.values():
        assert len(value) <= 4000


# ---------------------------------------------------------------------------
# make_uc_violation_writer
# ---------------------------------------------------------------------------


def test_violation_writer_rejects_bad_table_path() -> None:
    with pytest.raises(ValueError, match="three-part"):
        make_uc_violation_writer("not.three", ws=MagicMock())


def test_violation_writer_inserts_row_with_decision_fields() -> None:
    ws = MagicMock()
    writer = make_uc_violation_writer("main.x.violations", ws=ws, auto_create=False)
    with patch("apx_agent._watchdog.run_sql") as mock_sql:
        writer({
            "type": "violation_report",
            "decision": {
                "action": "reject",
                "reason": "policy bad",
                "policy_id": "p-1",
                "domain": "security",
                "metadata": {"owner": "data@x.com"},
            },
            "context": {"agent_name": "triage", "tool_name": "leak_pii"},
        })

    assert mock_sql.call_count == 1
    sql = mock_sql.call_args.args[1]
    assert "INSERT INTO main.x.violations" in sql
    # Decision values inlined
    assert "reject" in sql
    assert "policy bad" in sql
    assert "p-1" in sql
    assert "security" in sql
    # Context JSON inlined
    assert "triage" in sql


def test_violation_writer_passes_through_non_violation_requests() -> None:
    ws = MagicMock()
    writer = make_uc_violation_writer("main.x.violations", ws=ws, auto_create=False)
    with patch("apx_agent._watchdog.run_sql") as mock_sql:
        result = writer({"operation": "tool_call", "context": {}})
    assert result == {"action": "allow"}
    mock_sql.assert_not_called()


def test_violation_writer_auto_create_runs_once() -> None:
    ws = MagicMock()
    writer = make_uc_violation_writer("main.x.violations", ws=ws)
    with patch("apx_agent._watchdog.run_sql") as mock_sql:
        writer({"type": "violation_report", "decision": {"action": "reject"}, "context": {}})
        writer({"type": "violation_report", "decision": {"action": "reject"}, "context": {}})

    create_calls = [c for c in mock_sql.call_args_list if "CREATE TABLE" in c.args[1]]
    assert len(create_calls) == 1


def test_violation_writer_escapes_single_quotes_in_decision_strings() -> None:
    ws = MagicMock()
    writer = make_uc_violation_writer("main.x.violations", ws=ws, auto_create=False)
    with patch("apx_agent._watchdog.run_sql") as mock_sql:
        writer({
            "type": "violation_report",
            "decision": {"action": "reject", "reason": "user's policy violated"},
            "context": {},
        })

    sql = mock_sql.call_args.args[1]
    assert "user''s policy violated" in sql


def test_violation_writer_logs_and_continues_on_sql_failure() -> None:
    ws = MagicMock()
    writer = make_uc_violation_writer("main.x.violations", ws=ws, auto_create=False)
    with patch(
        "apx_agent._watchdog.run_sql", side_effect=RuntimeError("warehouse down"),
    ):
        # Should not raise
        result = writer({
            "type": "violation_report",
            "decision": {"action": "reject"},
            "context": {},
        })
    assert result == {}


# ---------------------------------------------------------------------------
# make_watchdog_transport — combined evaluate + violation
# ---------------------------------------------------------------------------


def test_watchdog_transport_routes_violation_to_uc_writer() -> None:
    ws = MagicMock()
    transport = make_watchdog_transport(
        mcp_url="https://watchdog.example.com/mcp",
        mcp_tool_name="evaluate_operation",
        violations_table="main.x.violations",
        ws=ws,
    )
    with patch("apx_agent._watchdog.run_sql") as mock_sql, \
         patch("apx_agent._watchdog.make_mcp_transport") as mock_mcp:
        # Re-create after patching the MCP factory so we don't hit real MCP
        transport = make_watchdog_transport(
            mcp_url="https://watchdog.example.com/mcp",
            mcp_tool_name="evaluate_operation",
            violations_table="main.x.violations",
            ws=ws,
        )
        transport({
            "type": "violation_report",
            "decision": {"action": "reject", "reason": "x"},
            "context": {"agent_name": "triage"},
        })

    insert_calls = [c for c in mock_sql.call_args_list if "INSERT INTO" in c.args[1]]
    assert len(insert_calls) == 1


def test_watchdog_transport_routes_evaluate_to_mcp() -> None:
    ws = MagicMock()

    fake_mcp = MagicMock(return_value={"action": "reject", "reason": "blocked"})
    with patch("apx_agent._watchdog.make_mcp_transport", return_value=fake_mcp):
        transport = make_watchdog_transport(
            mcp_url="https://watchdog.example.com/mcp",
            mcp_tool_name="evaluate_operation",
            violations_table="main.x.violations",
            ws=ws,
        )
        response = transport({"operation": "tool_call", "context": {}})

    assert response == {"action": "reject", "reason": "blocked"}
    fake_mcp.assert_called_once()


# ---------------------------------------------------------------------------
# WatchdogClient end-to-end with the real combined transport
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit attribute wiring — WatchdogGuard records decisions on active span
# ---------------------------------------------------------------------------


def test_for_tool_reject_records_audit_attrs_on_active_span() -> None:
    transport = _make_transport({
        "tool_call": {"action": "reject", "reason": "blocked",
                      "policy_id": "p-1", "domain": "security"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")

    fake_span = MagicMock()
    with patch("apx_agent._watchdog.current_active_span", return_value=fake_span), \
         pytest.raises(PermissionError):
        guard.for_tool()("classify_intent", {"query": "x"})

    keys_set = [c.args[0] for c in fake_span.set_attribute.call_args_list]
    assert "apx.watchdog.action" in keys_set
    assert "apx.watchdog.reason" in keys_set
    assert "apx.watchdog.policy_id" in keys_set
    assert "apx.watchdog.domain" in keys_set
    assert "apx.agent.name" in keys_set


def test_for_tool_allow_does_not_record_watchdog_audit_attrs() -> None:
    """Allow decisions are the default — no audit annotation needed."""
    transport = _make_transport()  # default allow
    guard = WatchdogGuard(WatchdogClient(transport=transport))

    fake_span = MagicMock()
    with patch("apx_agent._watchdog.current_active_span", return_value=fake_span):
        guard.for_tool()("classify_intent", {})

    keys_set = [c.args[0] for c in fake_span.set_attribute.call_args_list]
    assert "apx.watchdog.action" not in keys_set


def test_for_output_redact_records_audit_attrs() -> None:
    transport = _make_transport({
        "output_message": {"action": "redact", "reason": "PII",
                           "redacted_content": "<redacted>", "policy_id": "p-2"},
    })
    guard = WatchdogGuard(WatchdogClient(transport=transport), agent_name="triage")

    fake_span = MagicMock()
    with patch("apx_agent._watchdog.current_active_span", return_value=fake_span):
        out = guard.for_output()("text with PII")

    assert out == "<redacted>"
    keys_set = [c.args[0] for c in fake_span.set_attribute.call_args_list]
    assert "apx.watchdog.action" in keys_set
    assert "apx.watchdog.policy_id" in keys_set


def test_client_with_combined_transport_reject_decision_writes_violation_row() -> None:
    """Integration-style: client.evaluate via MCP + client.report_violation via UC."""
    ws = MagicMock()

    fake_mcp = MagicMock(return_value={"action": "reject", "reason": "blocked", "policy_id": "p-1"})
    with patch("apx_agent._watchdog.make_mcp_transport", return_value=fake_mcp), \
         patch("apx_agent._watchdog.run_sql") as mock_sql:
        transport = make_watchdog_transport(
            mcp_url="https://watchdog.example.com/mcp",
            mcp_tool_name="evaluate_operation",
            violations_table="main.x.violations",
            ws=ws,
        )
        client = WatchdogClient(transport=transport)

        decision = client.evaluate(operation="tool_call", context={"tool_name": "x"})
        assert decision.action == "reject"
        assert decision.policy_id == "p-1"

        client.report_violation(decision, {"agent_name": "triage", "tool_name": "x"})

    inserts = [c for c in mock_sql.call_args_list if "INSERT INTO main.x.violations" in c.args[1]]
    assert len(inserts) == 1
    sql = inserts[0].args[1]
    assert "p-1" in sql
    assert "triage" in sql

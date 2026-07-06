"""Tests for _audit.py — standard apx.* span-attribute schema.

Covers:
  - AuditAttrs constants are stable (keys don't change unintentionally)
  - set_audit_attrs maps short kwargs to canonical keys
  - set_audit_attrs skips None / empty values
  - set_audit_attrs raises on unknown kwargs (typos fail loud)
  - set_audit_attrs is a no-op with a None span
  - hash_for_audit is deterministic and truncates to the requested length
  - output_summary returns (type_name, size_estimate) for common shapes
  - input_keys_summary handles dict / string / other arg types
  - version_correlation_attrs / stamp_version_correlation read
    APX_MODEL_VERSION / APX_GIT_SHA from the env (issue #404) and are a
    strict no-op when the env vars are absent
  - cross-agent trace correlation (issue #443): build_traceparent emits
    W3C-valid values (derived from the active MLflow span when present),
    stamp_outbound_trace_id tags the caller side, stamp_caller_correlation
    tags the receiver side, and absent headers are a strict no-op
"""

from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

import pytest

from apx_agent import (
    AuditAttrs,
    build_traceparent,
    hash_for_audit,
    input_keys_summary,
    output_summary,
    set_audit_attrs,
    stamp_caller_correlation,
    stamp_outbound_trace_id,
    stamp_version_correlation,
    traceparent_trace_id,
    version_correlation_attrs,
)

W3C_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


# ---------------------------------------------------------------------------
# AuditAttrs constants — pinned via assertion to catch accidental renames
# ---------------------------------------------------------------------------


def test_audit_attrs_namespace() -> None:
    # Every public attribute key starts with apx.
    for name in dir(AuditAttrs):
        if name.startswith("_"):
            continue
        value = getattr(AuditAttrs, name)
        if isinstance(value, str):
            assert value.startswith("apx."), f"AuditAttrs.{name} = {value!r}"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("AGENT_NAME", "apx.agent.name"),
        ("SESSION_ID", "apx.session.id"),
        ("OPERATION", "apx.operation"),
        ("TOOL_NAME", "apx.tool.name"),
        ("TOOL_UC_FUNCTION", "apx.tool.uc_function"),
        ("MODEL_ENDPOINT", "apx.model.endpoint"),
        ("MODEL_INPUT_TOKENS", "apx.model.input_tokens"),
        ("WATCHDOG_ACTION", "apx.watchdog.action"),
        ("WATCHDOG_POLICY_ID", "apx.watchdog.policy_id"),
        ("USER_TOKEN_PROVIDED", "apx.user.token_provided"),
        ("APPROVAL_DECISION", "apx.approval.decision"),
        ("MODEL_VERSION", "apx.model_version"),
        ("GIT_SHA", "apx.git_sha"),
        ("TRACEPARENT", "apx.traceparent"),
        ("CALLER", "apx.caller"),
        ("OUTBOUND_TRACE_ID", "apx.outbound.trace_id"),
    ],
)
def test_audit_attrs_specific_keys(name: str, expected: str) -> None:
    assert getattr(AuditAttrs, name) == expected


# ---------------------------------------------------------------------------
# set_audit_attrs
# ---------------------------------------------------------------------------


def test_set_audit_attrs_maps_kwargs_to_canonical_keys() -> None:
    span = MagicMock()
    set_audit_attrs(
        span,
        agent_name="triage",
        tool_name="classify_intent",
        watchdog_action="reject",
    )
    keys_set = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "apx.agent.name" in keys_set
    assert "apx.tool.name" in keys_set
    assert "apx.watchdog.action" in keys_set


def test_set_audit_attrs_maps_approval_decision() -> None:
    span = MagicMock()
    set_audit_attrs(span, approval_decision="approve")
    keys_set = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "apx.approval.decision" in keys_set


def test_set_audit_attrs_skips_none_and_empty() -> None:
    span = MagicMock()
    set_audit_attrs(
        span,
        agent_name="triage",
        tool_name=None,
        watchdog_reason="",
        watchdog_policy_id="p-1",
    )
    keys_set = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "apx.agent.name" in keys_set
    assert "apx.watchdog.policy_id" in keys_set
    assert "apx.tool.name" not in keys_set
    assert "apx.watchdog.reason" not in keys_set


def test_set_audit_attrs_no_op_for_none_span() -> None:
    # Should not raise even with a None span
    set_audit_attrs(None, agent_name="triage", tool_name="x")


def test_user_hash_none_guard_and_reuse() -> None:
    """user_hash returns None for absent ids (so set_audit_attrs skips it) and
    otherwise reuses hash_for_audit — never hashing the literal 'None'."""
    from apx_agent._audit import user_hash

    assert user_hash(None) is None
    assert user_hash("") is None
    assert user_hash("u-123") == hash_for_audit("u-123")
    assert user_hash("u-123") != hash_for_audit("None")


def test_set_audit_attrs_records_user_hash() -> None:
    span = MagicMock()
    set_audit_attrs(span, user_hash=hash_for_audit("u-1"))
    keys_set = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "apx.user.hash" in keys_set


def test_set_audit_attrs_raises_on_unknown_kwarg() -> None:
    span = MagicMock()
    with pytest.raises(ValueError, match="unknown kwarg"):
        set_audit_attrs(span, made_up_field="value")


def test_set_audit_attrs_swallows_span_exceptions() -> None:
    """If span.set_attribute raises, the helper should log and continue
    (matches set_span_attribute's defensive contract)."""
    span = MagicMock()
    span.set_attribute.side_effect = RuntimeError("span broken")
    # Should not raise
    set_audit_attrs(span, agent_name="triage")


# ---------------------------------------------------------------------------
# Version correlation (issue #404) — APX_MODEL_VERSION / APX_GIT_SHA → attrs
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_correlation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APX_MODEL_VERSION", raising=False)
    monkeypatch.delenv("APX_GIT_SHA", raising=False)


def test_version_correlation_attrs_empty_without_env(
    _clean_correlation_env: None,
) -> None:
    assert version_correlation_attrs() == {}


def test_version_correlation_attrs_reads_both_env_vars(
    _clean_correlation_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APX_MODEL_VERSION", "7")
    monkeypatch.setenv("APX_GIT_SHA", "a" * 40)
    assert version_correlation_attrs() == {
        "apx.model_version": "7",
        "apx.git_sha": "a" * 40,
    }


def test_version_correlation_attrs_skips_empty_values(
    _clean_correlation_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bundle template defaults the vars to "" — an empty value must not
    # produce an empty-string attribute.
    monkeypatch.setenv("APX_MODEL_VERSION", "")
    monkeypatch.setenv("APX_GIT_SHA", "b" * 40)
    assert version_correlation_attrs() == {"apx.git_sha": "b" * 40}


def test_stamp_version_correlation_sets_span_attrs_and_trace_tags(
    _clean_correlation_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APX_GIT_SHA", "c" * 40)
    span = MagicMock()
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_version_correlation(span)
    span.set_attribute.assert_called_once_with("apx.git_sha", "c" * 40)
    tags_mock.assert_called_once_with({"apx.git_sha": "c" * 40})


def test_stamp_version_correlation_no_op_without_env(
    _clean_correlation_env: None,
) -> None:
    # Absent env → no span writes, no trace-tag call: zero behavior change
    # for locally-run agents and pre-#404 deploys.
    span = MagicMock()
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_version_correlation(span)
    span.set_attribute.assert_not_called()
    tags_mock.assert_not_called()


def test_stamp_version_correlation_survives_none_span(
    _clean_correlation_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None span (tracing off) still stamps the trace tags and never raises.
    monkeypatch.setenv("APX_MODEL_VERSION", "3")
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_version_correlation(None)
    tags_mock.assert_called_once_with({"apx.model_version": "3"})


# ---------------------------------------------------------------------------
# Cross-agent trace correlation (issue #443)
# ---------------------------------------------------------------------------


def test_build_traceparent_is_w3c_valid_without_active_span() -> None:
    tp = build_traceparent()
    assert W3C_TRACEPARENT.match(tp), tp


def test_build_traceparent_generates_fresh_ids_per_call() -> None:
    # Without an active span, two hops must not share a trace id.
    assert build_traceparent() != build_traceparent()


def test_build_traceparent_derives_from_active_mlflow_span() -> None:
    """With an active MLflow span, the header carries the CALLER's live
    trace-id/span-id — that is what makes the join real, not cosmetic."""
    span = MagicMock()
    span.trace_id = "tr-" + "ab" * 16
    span.span_id = "cd" * 8
    with patch("apx_agent._audit.current_active_span", return_value=span):
        tp = build_traceparent()
    assert tp == f"00-{'ab' * 16}-{'cd' * 8}-01"


def test_build_traceparent_falls_back_on_non_hex_span_ids() -> None:
    span = MagicMock()
    span.trace_id = "tr-not-hex"
    span.span_id = "also-not-hex"
    with patch("apx_agent._audit.current_active_span", return_value=span):
        tp = build_traceparent()
    assert W3C_TRACEPARENT.match(tp), tp


def test_traceparent_trace_id_extracts_trace_id() -> None:
    assert traceparent_trace_id(f"00-{'ab' * 16}-{'cd' * 8}-01") == "ab" * 16


def test_traceparent_trace_id_rejects_malformed() -> None:
    assert traceparent_trace_id("garbage") is None
    assert traceparent_trace_id("") is None
    assert traceparent_trace_id("00-shorthex-cd-01") is None


def test_stamp_outbound_trace_id_tags_caller_trace() -> None:
    span = MagicMock()
    tp = f"00-{'12' * 16}-{'34' * 8}-01"
    with patch("apx_agent._audit.current_active_span", return_value=span), patch(
        "apx_agent._audit.set_trace_tags"
    ) as tags_mock:
        stamp_outbound_trace_id(tp)
    span.set_attribute.assert_called_once_with("apx.outbound.trace_id", "12" * 16)
    tags_mock.assert_called_once_with({"apx.outbound.trace_id": "12" * 16})


def test_stamp_outbound_trace_id_no_op_on_malformed_traceparent() -> None:
    span = MagicMock()
    with patch("apx_agent._audit.current_active_span", return_value=span), patch(
        "apx_agent._audit.set_trace_tags"
    ) as tags_mock:
        stamp_outbound_trace_id("garbage")
    span.set_attribute.assert_not_called()
    tags_mock.assert_not_called()


def test_stamp_caller_correlation_stamps_all_three_keys() -> None:
    span = MagicMock()
    tp = f"00-{'ef' * 16}-{'01' * 8}-01"
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_caller_correlation(
            span, {"traceparent": tp, "x-apx-caller": "orchestrator"}
        )
    attrs = dict(c.args for c in span.set_attribute.call_args_list)
    assert attrs == {
        "apx.traceparent": tp,
        "apx.outbound.trace_id": "ef" * 16,
        "apx.caller": "orchestrator",
    }
    # The trace tag carries the SAME key/value the caller side stamps — the
    # one-tag-equality join.
    tags_mock.assert_called_once_with(attrs)


def test_stamp_caller_correlation_caller_only() -> None:
    span = MagicMock()
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_caller_correlation(span, {"x-apx-caller": "orchestrator"})
    span.set_attribute.assert_called_once_with("apx.caller", "orchestrator")
    tags_mock.assert_called_once_with({"apx.caller": "orchestrator"})


def test_stamp_caller_correlation_keeps_raw_traceparent_when_malformed() -> None:
    # A malformed traceparent is still recorded verbatim for debugging, but
    # no bogus join key is derived from it.
    span = MagicMock()
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_caller_correlation(span, {"traceparent": "garbage"})
    span.set_attribute.assert_called_once_with("apx.traceparent", "garbage")
    tags_mock.assert_called_once_with({"apx.traceparent": "garbage"})


def test_stamp_caller_correlation_no_op_without_headers() -> None:
    # Absent headers → no span writes, no trace-tag call: zero behavior
    # change for non-agent callers.
    span = MagicMock()
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_caller_correlation(span, {})
    span.set_attribute.assert_not_called()
    tags_mock.assert_not_called()


def test_stamp_caller_correlation_survives_none_span() -> None:
    # A None span (tracing off) still stamps the trace tags and never raises.
    with patch("apx_agent._audit.set_trace_tags") as tags_mock:
        stamp_caller_correlation(None, {"x-apx-caller": "a"})
    tags_mock.assert_called_once_with({"apx.caller": "a"})


# ---------------------------------------------------------------------------
# hash_for_audit
# ---------------------------------------------------------------------------


def test_hash_for_audit_is_deterministic() -> None:
    assert hash_for_audit("hello") == hash_for_audit("hello")
    assert hash_for_audit({"a": 1}) == hash_for_audit({"a": 1})


def test_hash_for_audit_differs_across_inputs() -> None:
    assert hash_for_audit("a") != hash_for_audit("b")


def test_hash_for_audit_respects_length() -> None:
    assert len(hash_for_audit("x", length=8)) == 8
    assert len(hash_for_audit("x", length=32)) == 32


def test_hash_for_audit_hex_only() -> None:
    h = hash_for_audit({"complex": ["nested", 1]})
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# output_summary
# ---------------------------------------------------------------------------


def test_output_summary_for_str() -> None:
    result = output_summary("hello world")
    assert result.type_name == "str"
    assert result.size == 11


def test_output_summary_for_list() -> None:
    result = output_summary([1, 2, 3])
    assert result.type_name == "list"
    assert result.size == 3


def test_output_summary_for_dict() -> None:
    result = output_summary({"a": 1, "b": 2})
    assert result.type_name == "dict"
    assert result.size == 2


def test_output_summary_for_int_falls_back_to_str_length() -> None:
    result = output_summary(42)
    assert result.type_name == "int"
    assert result.size == 2  # str(42) = "42"


# ---------------------------------------------------------------------------
# input_keys_summary
# ---------------------------------------------------------------------------


def test_input_keys_summary_dict_returns_sorted_keys() -> None:
    assert input_keys_summary({"b": 1, "a": 2}) == "a,b"


def test_input_keys_summary_string() -> None:
    assert input_keys_summary("hello") == "<string>"


def test_input_keys_summary_other() -> None:
    assert input_keys_summary([1, 2, 3]) == "list"


def test_input_keys_summary_empty_dict() -> None:
    assert input_keys_summary({}) == ""

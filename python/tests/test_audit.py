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
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apx_agent import (
    AuditAttrs,
    hash_for_audit,
    input_keys_summary,
    output_summary,
    set_audit_attrs,
    stamp_version_correlation,
    version_correlation_attrs,
)


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
        ("MODEL_VERSION", "apx.model_version"),
        ("GIT_SHA", "apx.git_sha"),
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

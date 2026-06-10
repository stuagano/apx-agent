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
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent import (
    AuditAttrs,
    hash_for_audit,
    input_keys_summary,
    output_summary,
    set_audit_attrs,
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

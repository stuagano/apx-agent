"""Tests for apx_agent._fleet (fleet selector + helpers)."""
from __future__ import annotations

import pytest

from apx_agent import _fleet


@pytest.mark.unit
def test_to_label_key_adds_prefix_once():
    assert _fleet.to_label_key("team") == "apx.label.team"
    assert _fleet.to_label_key("apx.label.team") == "apx.label.team"


@pytest.mark.unit
def test_is_reserved_flags_system_namespaces():
    assert _fleet.is_reserved("apx.agent.name") is True
    assert _fleet.is_reserved("apx.apps.role") is True
    assert _fleet.is_reserved("team") is False
    assert _fleet.is_reserved("apx.label.team") is False


@pytest.mark.unit
def test_parse_where_splits_key_value():
    assert _fleet.parse_where(["team=revops", "env=prod"]) == {
        "team": "revops",
        "env": "prod",
    }


@pytest.mark.unit
def test_parse_where_rejects_missing_equals():
    with pytest.raises(ValueError, match="key=value"):
        _fleet.parse_where(["teamrevops"])

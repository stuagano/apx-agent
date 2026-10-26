"""Tests for _cost.py — USD estimates from token usage (budget guard, dev UI)."""

from __future__ import annotations


def test_usd_for_token_usage_uses_published_sonnet_rates() -> None:
    from apx_agent._cost import usd_for_token_usage

    assert usd_for_token_usage(1_000_000, 0, model="databricks-claude-sonnet-4-6") == 3.0
    assert usd_for_token_usage(0, 1_000_000, model="databricks:/databricks-claude-sonnet-4-6") == 15.0
    assert usd_for_token_usage(30, 30, model="databricks-claude-sonnet-4-6") == 0.00054
    assert usd_for_token_usage(10, 10, model="unknown-endpoint") is None
    assert usd_for_token_usage(10, 10, model=None) is None

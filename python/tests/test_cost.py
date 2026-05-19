"""Tests for _cost.py — DBU + USD cost helpers over system tables.

Covers:
  - cost_for_endpoint requires non-empty endpoint + positive lookback
  - SQL query construction includes endpoint name + lookback hours
  - Returned breakdown sums DBUs and USD across SKU rows
  - Zero/None unit prices surface as None USD (avoids "free" misread)
  - SQL failures degrade to an empty breakdown with a logged warning
  - cost_for_agent dispatches to cost_for_endpoint with the agent name
    when no endpoint is supplied
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apx_agent import CostBreakdown, cost_for_agent, cost_for_endpoint


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_cost_for_endpoint_requires_endpoint() -> None:
    with pytest.raises(ValueError):
        cost_for_endpoint("", ws=MagicMock())


def test_cost_for_endpoint_rejects_zero_lookback() -> None:
    with pytest.raises(ValueError):
        cost_for_endpoint("triage", ws=MagicMock(), lookback_hours=0)


def test_cost_for_agent_requires_one_of_agent_or_endpoint() -> None:
    with pytest.raises(ValueError):
        cost_for_agent(ws=MagicMock())


# ---------------------------------------------------------------------------
# SQL query construction
# ---------------------------------------------------------------------------


def test_query_includes_endpoint_name_and_lookback_hours() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]) as mock_sql:
        cost_for_endpoint("customer_triage", ws=ws, lookback_hours=12)

    sql = mock_sql.call_args.args[1]
    assert "system.billing.usage" in sql
    assert "system.billing.list_prices" in sql
    assert "customer_triage" in sql
    assert "INTERVAL 12 HOUR" in sql


def test_query_escapes_single_quotes_in_endpoint_name() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]) as mock_sql:
        cost_for_endpoint("user's-endpoint", ws=ws)

    sql = mock_sql.call_args.args[1]
    assert "user''s-endpoint" in sql


def test_warehouse_id_forwarded_to_run_sql() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]) as mock_sql:
        cost_for_endpoint("triage", ws=ws, warehouse_id="wh-prod")

    assert mock_sql.call_args.kwargs["warehouse_id"] == "wh-prod"


# ---------------------------------------------------------------------------
# Row aggregation
# ---------------------------------------------------------------------------


def test_breakdown_aggregates_rows() -> None:
    ws = MagicMock()
    rows = [
        {"sku_name": "MODEL_SERVING", "usage_unit": "DBU", "dbus": 100.0, "usd": 7.5, "unit_price": 0.075},
        {"sku_name": "WAREHOUSE",     "usage_unit": "DBU", "dbus": 50.0,  "usd": 3.0, "unit_price": 0.06},
    ]
    with patch("apx_agent._cost.run_sql", return_value=rows):
        breakdown = cost_for_endpoint("triage", ws=ws)

    assert isinstance(breakdown, CostBreakdown)
    assert breakdown.endpoint == "triage"
    assert breakdown.total_dbus == 150.0
    assert breakdown.total_usd == 10.5
    assert len(breakdown.rows) == 2


def test_zero_usd_normalised_to_none() -> None:
    """Pricing-missing rows return usd=0 from the SQL — surface as None."""
    ws = MagicMock()
    rows = [
        {"sku_name": "X", "usage_unit": "DBU", "dbus": 10.0, "usd": 0, "unit_price": None},
        {"sku_name": "Y", "usage_unit": "DBU", "dbus": 5.0, "usd": 1.5, "unit_price": 0.3},
    ]
    with patch("apx_agent._cost.run_sql", return_value=rows):
        breakdown = cost_for_endpoint("triage", ws=ws)

    # The zero-usd row gets None; the breakdown total falls back to None too
    # (mixed pricing availability — caller should look at individual rows).
    assert breakdown.rows[0]["usd"] is None
    assert breakdown.rows[1]["usd"] == 1.5
    assert breakdown.total_usd is None  # not all rows had prices


def test_all_rows_with_prices_sums_to_total_usd() -> None:
    ws = MagicMock()
    rows = [
        {"sku_name": "A", "usage_unit": "DBU", "dbus": 10.0, "usd": 1.0, "unit_price": 0.1},
        {"sku_name": "B", "usage_unit": "DBU", "dbus": 20.0, "usd": 2.0, "unit_price": 0.1},
    ]
    with patch("apx_agent._cost.run_sql", return_value=rows):
        breakdown = cost_for_endpoint("triage", ws=ws)

    assert breakdown.total_usd == 3.0


def test_empty_rows_returns_empty_breakdown() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]):
        breakdown = cost_for_endpoint("triage", ws=ws)

    assert breakdown.total_dbus == 0.0
    assert breakdown.total_usd is None
    assert breakdown.rows == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_sql_failure_returns_empty_breakdown_with_warning() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", side_effect=RuntimeError("system.billing not accessible")):
        breakdown = cost_for_endpoint("triage", ws=ws)

    # Doesn't raise — degrades to an empty breakdown
    assert breakdown.endpoint == "triage"
    assert breakdown.total_dbus == 0.0
    assert breakdown.rows == []


# ---------------------------------------------------------------------------
# cost_for_agent dispatching
# ---------------------------------------------------------------------------


def test_cost_for_agent_uses_agent_name_as_endpoint_by_default() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]) as mock_sql:
        cost_for_agent(agent_name="customer_triage", ws=ws)

    sql = mock_sql.call_args.args[1]
    assert "customer_triage" in sql


def test_cost_for_agent_explicit_endpoint_wins() -> None:
    ws = MagicMock()
    with patch("apx_agent._cost.run_sql", return_value=[]) as mock_sql:
        cost_for_agent(
            agent_name="customer_triage",
            endpoint="explicit-endpoint",
            ws=ws,
        )

    sql = mock_sql.call_args.args[1]
    assert "explicit-endpoint" in sql
    # agent_name should NOT appear when an explicit endpoint is given
    assert "customer_triage" not in sql

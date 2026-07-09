"""Tests for scripts/monthly_summary.py — pure logic + mocked run_sql/run_once.

No live warehouse or LLM call: run_sql and run_once are monkeypatched.
"""

from __future__ import annotations

import datetime as dt


def test_previous_month_mid_year() -> None:
    from scripts import monthly_summary
    assert monthly_summary.previous_month(dt.date(2026, 7, 15)) == "2026-06-01"


def test_previous_month_january_rolls_back_a_year() -> None:
    from scripts import monthly_summary
    assert monthly_summary.previous_month(dt.date(2026, 1, 15)) == "2025-12-01"


def test_parse_month() -> None:
    from scripts import monthly_summary
    assert monthly_summary.parse_month("2026-03") == "2026-03-01"


def test_summarize_month_splits_count_from_theme_summary(monkeypatch) -> None:
    from scripts import monthly_summary

    calls = {}

    def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
        calls["sql"] = sql
        calls["parameters"] = parameters
        return [{"n": 7}]

    def fake_run_once(agent, prompt):
        calls["prompt"] = prompt
        return "3 themes found"

    monkeypatch.setattr(monthly_summary, "run_sql", fake_run_sql)
    monkeypatch.setattr(monthly_summary, "run_once", fake_run_once)

    count, summary = monthly_summary.summarize_month(object(), "2026-06-01")

    assert count == 7
    assert summary == "3 themes found"
    assert "2026-06" in calls["prompt"]


def test_write_summary_creates_table_then_inserts(monkeypatch) -> None:
    from scripts import monthly_summary

    statements = []

    def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
        statements.append(sql)
        return []

    monkeypatch.setattr(monthly_summary, "run_sql", fake_run_sql)

    monthly_summary.write_summary(object(), "2026-06-01", 7, "themes...")

    assert "CREATE TABLE IF NOT EXISTS" in statements[0]
    assert "complaint_summaries" in statements[0]
    assert "INSERT INTO" in statements[1]

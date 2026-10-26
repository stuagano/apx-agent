"""A Genie row missing a requested column is skipped, not turned into an empty-id signal."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402
from databricks.sdk.service.dashboards import MessageStatus  # noqa: E402


def test_scan_skips_rows_missing_required_columns(monkeypatch):
    ws = MagicMock()
    ws.genie.start_conversation_and_wait.return_value = SimpleNamespace(status=MessageStatus.COMPLETED)
    monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(demand_genie_space_id="s"))
    full = {"component_id": "C1", "component_name": "cap", "earliest_request": "t0",
            "latest_request": "t1", "customer_count": 4, "total_units_requested": 9}
    monkeypatch.setattr(tools, "_genie_rows", lambda *a: [full, {"component_name": "no id"}])
    [signal] = tools._scan_via_genie(24, 2, ws)
    assert signal.component_id == "C1" and signal.confidence == "HIGH"


def test_scan_question_names_the_columns_it_requires(monkeypatch):
    # Rows are parsed by exact key, so the question must ask Genie for those exact column names.
    ws = MagicMock()
    ws.genie.start_conversation_and_wait.return_value = SimpleNamespace(status=MessageStatus.FAILED, error="x")
    monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(demand_genie_space_id="s"))
    tools._scan_via_genie(24, 2, ws)
    question = ws.genie.start_conversation_and_wait.call_args.kwargs["content"]
    for column in ("component_id", "component_name", "customer_count", "earliest_request",
                   "latest_request", "total_units_requested"):
        assert column in question, column

"""Smoke tests for the classify_shortage_severity UC helper.

Pure logic — no Databricks dependencies required to run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _add_src_to_path() -> None:
    src = Path(__file__).parent.parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


@pytest.mark.parametrize(
    "avg,maxp,customers,events,expected",
    [
        # CRITICAL — peer events spiked >40% AND >=4 customers
        (35.0, 55.0, 5, 3, "CRITICAL"),
        # HIGH — avg >25% spike (customer count irrelevant once avg is high)
        (28.0, 35.0, 2, 4, "HIGH"),
        # HIGH — customers >=4 (price irrelevant once customer count is high)
        (8.0, 12.0, 5, 3, "HIGH"),
        # MEDIUM — avg >10% spike
        (15.0, 18.0, 2, 2, "MEDIUM"),
        # MEDIUM — customer count >=2
        (5.0, 7.0, 3, 2, "MEDIUM"),
        # LOW — peer events exist but neither threshold trips
        (3.0, 5.0, 1, 5, "LOW"),
        # NOVEL — no historical events
        (0.0, 0.0, 3, 0, "NOVEL"),
    ],
)
def test_severity_thresholds(
    avg: float, maxp: float, customers: int, events: int, expected: str,
) -> None:
    from shortage_intelligence_agent.backend.uc_helpers import classify_shortage_severity

    assert classify_shortage_severity(
        avg_price_delta_pct=avg,
        max_price_delta_pct=maxp,
        customer_count=customers,
        similar_events_found=events,
    ) == expected


def test_uc_metadata_attached() -> None:
    """The @tool(uc=...) decorator should attach metadata so publish_tools_to_uc finds it."""
    from apx_agent import get_tool_metadata

    from shortage_intelligence_agent.backend.uc_helpers import classify_shortage_severity

    meta = get_tool_metadata(classify_shortage_severity)
    assert meta is not None
    assert meta.uc_name == "main.shortage_tools.classify_shortage_severity"
    assert "data_analysts" in meta.grants
    assert "agent_consumers" in meta.grants

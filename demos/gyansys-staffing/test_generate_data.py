# demos/gyansys-staffing/test_generate_data.py
from __future__ import annotations

from datetime import date, timedelta

from generate_data import REFERENCE_DATE, generate


def _is_stalled(opp: dict) -> bool:
    open_stages = {"Prospecting", "Qualification", "Proposal", "Negotiation"}
    if opp["stage"] not in open_stages:
        return False
    age = (REFERENCE_DATE - opp["last_activity_date"]).days
    return age > 30


def test_counts_are_deterministic():
    a = generate()
    b = generate()
    assert len(a.people) == 200
    assert len(a.opportunities) == 75
    # deterministic: same seed → identical first rows
    assert a.people[0] == b.people[0]
    assert a.opportunities[0] == b.opportunities[0]


def test_planted_stalled_opportunities_exist():
    data = generate()
    stalled = [o for o in data.opportunities if _is_stalled(o)]
    assert len(stalled) >= 3
    # planted stalled opps are high-value and carry a reason
    assert any(o["amount"] >= 100_000 for o in stalled)
    assert all(o["stall_reason"] for o in stalled)


def test_india_is_tight_on_databricks_skill():
    data = generate()
    india_dbx = [
        p for p in data.people
        if p["region"] == "India" and "Databricks" in p["skills"]
    ]
    assert len(india_dbx) >= 1, "need at least one India Databricks person to match"
    # planted scarcity: their average availability is low (the bandwidth story)
    avg_avail = sum(p["availability_pct"] for p in india_dbx) / len(india_dbx)
    assert avg_avail < 30.0


def test_every_opportunity_has_required_skills():
    data = generate()
    assert all(o["required_skills"].strip() for o in data.opportunities)
    assert all(isinstance(o["required_skills"], str) for o in data.opportunities)

"""Synthetic data for all sources (CRM, field-service system, document store, ERP).

Offline, dependency-free (stdlib ``random`` only — no Faker needed for the
contract the gate checks). Produces rows per view, every row keyed by a company
drawn from ``contract.COMPANIES``, and every company present in every view so
all 7 brief sections join cleanly (AC-2).

The output is a dict ``{view_name: [row-dict, ...]}`` whose row keys are exactly
that view's contract columns — the same shape the agent's stubbed SQL and a
future Databricks load job both consume. Deterministic under a fixed seed.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from contract import COMPANIES, VIEWS  # loaded via importlib in the same dir

_PRODUCTS = [
    "Model A sensor", "Model B analyzer", "Model C detector",
    "Model D meter", "Model E camera", "Model F probe",
]
_STAGES = ["Discovery", "Qualification", "Proposal", "Negotiation", "Closed Won"]
_STATUSES = ["Open", "In Progress", "Blocked", "Closed"]
_SEVERITIES = ["Low", "Medium", "High", "Critical"]
_AUTHORS = ["A. Rivera", "J. Chen", "M. Osei", "P. Novak", "S. Delgado"]
_OUTCOMES = ["Won", "Lost"]


def _date_str(rng: random.Random) -> str:
    return (date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))).isoformat()


def _value_for(col: str, company: str, i: int, rng: random.Random) -> Any:
    """Plausible synthetic value for a contract column, by column name."""
    if col == "company":
        return company
    if col in ("close_date", "due_date", "date", "expected_ship"):
        return _date_str(rng)
    if col == "value":
        return rng.randrange(10_000, 500_000, 5_000)
    if col == "qty":
        return rng.randint(1, 50)
    if col == "stage":
        return rng.choice(_STAGES)
    if col == "status":
        return rng.choice(_STATUSES)
    if col == "severity":
        return rng.choice(_SEVERITIES)
    if col == "outcome":
        return rng.choice(_OUTCOMES)
    if col == "product":
        return rng.choice(_PRODUCTS)
    if col == "author":
        return rng.choice(_AUTHORS)
    if col == "opportunity":
        return f"{rng.choice(_PRODUCTS)} fleet upgrade"
    if col == "action":
        return f"Follow up on {rng.choice(_PRODUCTS)} quote"
    if col == "note":
        return f"Site visit: interest in {rng.choice(_PRODUCTS)}."
    if col == "description":
        return f"{rng.choice(_PRODUCTS)} unit #{i + 1}"
    if col.endswith("_id"):
        return f"{col[:-3].upper()}-{rng.randint(1000, 9999)}"
    return f"{col}-{i}"


def generate(seed: int = 0, rows_per_company: int = 2) -> dict[str, list[dict[str, Any]]]:
    """Return ``{view: [rows]}`` conforming to the frozen contract.

    Every company in ``COMPANIES`` gets ``rows_per_company`` rows in every view,
    so all views are non-empty and join cleanly on ``company``.
    """
    rng = random.Random(seed)
    out: dict[str, list[dict[str, Any]]] = {}
    for view, cols in VIEWS.items():
        rows: list[dict[str, Any]] = []
        for company in COMPANIES:
            for i in range(rows_per_company):
                rows.append({col: _value_for(col, company, i, rng) for col in cols})
        out[view] = rows
    return out

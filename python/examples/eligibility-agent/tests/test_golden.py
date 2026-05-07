"""End-to-end golden cases: full pipeline from parsed docs → decision → trail.

Each case validates the decision, tier, and key flags without touching
a Databricks workspace.
"""
from datetime import date

import pytest

from eligibility_agent.tools.assess_eligibility import assess_eligibility
from eligibility_agent.tools.check_residency import check_residency
from eligibility_agent.tools.compute_income import compute_income
from eligibility_agent.tools.reasoning_trail import build_reasoning_trail

_TODAY = date.today().isoformat()
_HOUSEHOLD = {"residence_address": "100 Main St", "residence_city": "Springfield", "residence_state": "CA"}


@pytest.fixture(autouse=True)
def set_state(monkeypatch):
    monkeypatch.setenv("STATE_CODE", "CA")
    from eligibility_agent import config
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _run(parsed: dict, household: dict, household_size: int) -> dict:
    income = compute_income(parsed)
    residency = check_residency(parsed, household)
    elig = assess_eligibility(
        application_id=parsed.get("application_id", "A-TEST"),
        annual_household_income=income["annual_household_income"],
        household_size=household_size,
        residency_result=residency,
    )
    trail = build_reasoning_trail(
        application_id=parsed.get("application_id", "A-TEST"),
        parsed=parsed, income=income, residency=residency, eligibility=elig,
    )
    return {"income": income, "residency": residency, "eligibility": elig, "trail": trail}


def _residency_doc(address="100 Main St", csz="Springfield CA 62701"):
    return {"document_type": "residency", "extracted": {
        "address_line": address, "city_state_zip": csz, "document_date": _TODAY,
    }, "confidence_concern": False}


def test_golden_1_two_w2_priority_tier():
    """Two W-2 filers, ~130% FPL for household of 4 → priority eligible."""
    parsed = {"application_id": "G-1", "documents": [
        {"document_type": "w2", "extracted": {"employee_name": "Alice S", "annual_wages": 26000.00}, "confidence_concern": False},
        {"document_type": "w2", "extracted": {"employee_name": "Bob S", "annual_wages": 16000.00}, "confidence_concern": False},
        _residency_doc(),
    ]}
    out = _run(parsed, _HOUSEHOLD, household_size=4)
    assert out["eligibility"]["decision"] == "eligible"
    assert out["eligibility"]["tier"] == "priority"


def test_golden_2_standard_tier():
    """Household income at ~248% FPL for household of 4 → standard eligible."""
    parsed = {"application_id": "G-2", "documents": [
        {"document_type": "w2", "extracted": {"employee_name": "Alice S", "annual_wages": 48210.00}, "confidence_concern": False},
        {"document_type": "w2", "extracted": {"employee_name": "Bob S", "annual_wages": 31474.00}, "confidence_concern": False},
        _residency_doc(),
    ]}
    out = _run(parsed, _HOUSEHOLD, household_size=4)
    assert out["eligibility"]["decision"] == "eligible"
    assert out["eligibility"]["tier"] == "standard"


def test_golden_3_over_400_pct_ineligible():
    """Single filer $130k, household of 1 → > 400% FPL → ineligible."""
    parsed = {"application_id": "G-3", "documents": [
        {"document_type": "w2", "extracted": {"employee_name": "Solo Earner", "annual_wages": 130000.00}, "confidence_concern": False},
        _residency_doc(),
    ]}
    out = _run(parsed, _HOUSEHOLD, household_size=1)
    assert out["eligibility"]["decision"] == "ineligible"


def test_golden_4_paystub_only_flagged():
    """Paystub-only filer — annualized and flagged, still eligible."""
    parsed = {"application_id": "G-4", "documents": [
        {"document_type": "paystub", "extracted": {"employee_name": "Bob S", "gross_pay": 1210.55}, "confidence_concern": False},
        _residency_doc(),
    ]}
    out = _run(parsed, _HOUSEHOLD, household_size=4)
    assert any("paystub-only" in d.lower() or "no w-2" in d.lower() for d in out["income"]["discrepancies"])
    assert out["eligibility"]["decision"] == "eligible"


def test_golden_5_income_discrepancy_flagged():
    """Paystub-annualized vs W-2 diverge >5% — discrepancy flagged, W-2 wins."""
    parsed = {"application_id": "G-5", "documents": [
        {"document_type": "paystub", "extracted": {"employee_name": "Pat C", "gross_pay": 1850.00}, "confidence_concern": False},
        {"document_type": "w2", "extracted": {"employee_name": "Pat C", "annual_wages": 42000.00}, "confidence_concern": False},
        _residency_doc(),
    ]}
    out = _run(parsed, _HOUSEHOLD, household_size=4)
    assert any("discrepancy" in d.lower() for d in out["income"]["discrepancies"])
    assert out["income"]["annual_household_income"] == 42000.00

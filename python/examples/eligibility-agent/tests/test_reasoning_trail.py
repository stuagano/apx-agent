import pytest
from eligibility_agent.tools.reasoning_trail import build_reasoning_trail


@pytest.fixture
def fixture():
    return dict(
        application_id="A-001",
        parsed={"documents": [
            {"document_type": "paystub", "extracted": {"employee_name": "Alice Smith", "gross_pay": 1842.30, "ytd_gross": 19341.0}, "confidence_concern": False},
            {"document_type": "w2", "extracted": {"employee_name": "Alice Smith", "annual_wages": 48210.00}, "confidence_concern": False},
        ]},
        income={
            "annual_household_income": 48210.00,
            "source_breakdown": {"Alice Smith": {"preferred_source": "w2", "preferred_amount": 48210.00}},
            "discrepancies": [],
            "confidence": "high",
        },
        residency={"verified": True, "issues": []},
        eligibility={
            "decision": "eligible",
            "tier": "priority",
            "annual_household_income": 48210.00,
            "household_size": 4,
            "fpl_thresholds": {"100_pct": 32150, "185_pct": 59478, "400_pct": 128600},
            "income_to_fpl_ratio": 1.5,
            "blockers": [],
        },
    )


def test_contains_decision_and_application_id(fixture):
    trail = build_reasoning_trail(**fixture)
    assert "A-001" in trail
    assert "Eligible" in trail
    assert "priority" in trail.lower()


def test_lists_each_document(fixture):
    trail = build_reasoning_trail(**fixture)
    assert "paystub" in trail.lower()
    assert "W-2" in trail or "w2" in trail.lower()
    assert "Alice Smith" in trail


def test_shows_fpl_thresholds(fixture):
    trail = build_reasoning_trail(**fixture)
    assert "32,150" in trail or "32150" in trail
    assert "FPL" in trail


def test_includes_hhs_disclaimer(fixture):
    trail = build_reasoning_trail(**fixture)
    assert "Federal Poverty Level" in trail or "HHS" in trail

from eligibility_agent.tools.assess_eligibility import assess_eligibility

# household_of_4: 100% FPL = $32,150 → 185% = $59,478 → 400% = $128,600

def test_priority_tier_below_185_percent():
    res = assess_eligibility("A-1", 42000.0, 4, {"verified": True})
    assert res["decision"] == "eligible"
    assert res["tier"] == "priority"


def test_standard_tier_between_185_and_400():
    res = assess_eligibility("A-2", 79684.0, 4, {"verified": True})
    assert res["decision"] == "eligible"
    assert res["tier"] == "standard"


def test_ineligible_above_400_percent():
    res = assess_eligibility("A-3", 160000.0, 4, {"verified": True})
    assert res["decision"] == "ineligible"
    assert res["tier"] is None


def test_residency_failure_blocks_decision():
    res = assess_eligibility("A-4", 42000.0, 4, {"verified": False, "issues": ["address mismatch"]})
    assert res["decision"] == "blocked"
    assert any("residency" in b.lower() for b in res["blockers"])


def test_household_size_drives_threshold():
    # household_of_1: 100% FPL = $15,650 → 400% = $62,600
    res = assess_eligibility("A-5", 52000.0, 1, {"verified": True})
    assert res["decision"] == "eligible"
    assert res["tier"] == "standard"

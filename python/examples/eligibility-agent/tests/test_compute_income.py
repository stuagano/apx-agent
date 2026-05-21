from tools.compute_income import compute_income


def test_prefers_w2_over_paystub_annualization():
    parsed = {"documents": [
        {"document_type": "paystub", "extracted": {"employee_name": "Alice Smith", "gross_pay": 1842.30, "ytd_gross": 19341.0}},
        {"document_type": "w2", "extracted": {"employee_name": "Alice Smith", "annual_wages": 48210.00}},
    ]}
    result = compute_income(parsed)
    assert result["source_breakdown"]["Alice Smith"]["preferred_source"] == "w2"
    assert result["annual_household_income"] == 48210.00


def test_paystub_only_annualizes_to_26_periods():
    parsed = {"documents": [
        {"document_type": "paystub", "extracted": {"employee_name": "Bob Jones", "gross_pay": 1210.55}},
    ]}
    result = compute_income(parsed)
    assert result["annual_household_income"] == round(1210.55 * 26, 2)
    assert result["source_breakdown"]["Bob Jones"]["preferred_source"] == "paystub_annualized"


def test_flags_discrepancy_between_w2_and_paystub():
    parsed = {"documents": [
        {"document_type": "paystub", "extracted": {"employee_name": "Pat Chen", "gross_pay": 1850.00}},
        {"document_type": "w2", "extracted": {"employee_name": "Pat Chen", "annual_wages": 42000.00}},
    ]}
    result = compute_income(parsed)
    assert any("discrepancy" in d.lower() or "Pat Chen" in d for d in result["discrepancies"])


def test_two_filer_household_sums_correctly():
    parsed = {"documents": [
        {"document_type": "w2", "extracted": {"employee_name": "Alice Smith", "annual_wages": 48210.00}},
        {"document_type": "w2", "extracted": {"employee_name": "Bob Smith", "annual_wages": 31474.00}},
    ]}
    result = compute_income(parsed)
    assert result["annual_household_income"] == 79684.00

from unittest.mock import MagicMock, patch

from tools.summarize_contract import summarize_contract


def test_summarize_contract_returns_record_when_found():
    ws = MagicMock()
    fake_row = {
        "contract_id": "CT-0001",
        "counterparty": "Acme Energy Co",
        "contract_type": "ppa",
        "term_years": 10.0,
        "auto_renewal": True,
        "expiry_date": "2030-04-30",
        "pricing_summary": "Flat $0.072/kWh.",
    }
    with patch(
        "tools.summarize_contract.run_sql",
        return_value=[fake_row],
    ):
        out = summarize_contract("CT-0001", ws=ws)
    assert out["contract"] == fake_row
    assert "found" in out and out["found"] is True


def test_summarize_contract_empty_when_not_found():
    ws = MagicMock()
    with patch(
        "tools.summarize_contract.run_sql",
        return_value=[],
    ):
        out = summarize_contract("CT-9999", ws=ws)
    assert out == {"contract": None, "found": False, "contract_id": "CT-9999"}

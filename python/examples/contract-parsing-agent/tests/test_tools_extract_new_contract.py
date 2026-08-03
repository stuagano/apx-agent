from unittest.mock import MagicMock, patch

from tools.extract_new_contract import (
    extract_new_contract,
)


def test_extract_new_contract_appends_and_returns_fields():
    ws = MagicMock()
    fake_extract = {
        "counterparty": "Duke Energy",
        "contract_type": "ppa",
        "effective_date": "2026-01-01",
        "expiry_date": "2031-01-01",
        "term_years": 5,
        "pricing_model": "fixed",
        "auto_renewal": False,
    }
    with patch(
        "tools.extract_new_contract.extract",
        return_value=fake_extract,
    ), patch(
        "tools.extract_new_contract.run_sql",
        return_value=[],
    ) as mock_sql:
        out = extract_new_contract(
            volume_path="/Volumes/x/y/uploads/abc.pdf",
            ws=ws,
        )
    assert out["extracted"]["counterparty"] == "Duke Energy"
    assert out["contract_id"].startswith("CT-LIVE-")
    insert_sql = mock_sql.call_args[0][1]
    assert "INSERT INTO" in insert_sql.upper()
    assert "Duke Energy" in insert_sql


def test_extract_new_contract_surfaces_extraction_error():
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract.extract",
        side_effect=RuntimeError("extraction_unavailable: timeout"),
    ):
        out = extract_new_contract(
            volume_path="/Volumes/x/y/uploads/oops.pdf", ws=ws,
        )
    assert out.get("error") == "extraction_unavailable"
    assert "timeout" in out.get("message", "")


def test_extract_new_contract_rejects_non_volume_path():
    # A path outside /Volumes/ must be refused before extraction/SQL touches it.
    ws = MagicMock()
    with patch("tools.extract_new_contract.extract") as mock_extract:
        out = extract_new_contract(volume_path="/etc/passwd", ws=ws)
    assert out.get("error") == "invalid_path"
    mock_extract.assert_not_called()


def test_extract_new_contract_surfaces_insert_error():
    ws = MagicMock()
    fake_extract = {
        "counterparty": "Duke Energy",
        "contract_type": "ppa",
        "effective_date": "2026-01-01",
        "expiry_date": "2031-01-01",
        "term_years": 5,
        "pricing_model": "fixed",
        "auto_renewal": False,
    }
    with patch(
        "tools.extract_new_contract.extract",
        return_value=fake_extract,
    ), patch(
        "tools.extract_new_contract.run_sql",
        side_effect=RuntimeError("warehouse timeout"),
    ):
        out = extract_new_contract(
            volume_path="/Volumes/x/y/uploads/abc.pdf", ws=ws,
        )
    assert out.get("error") == "insert_failed"
    assert "warehouse timeout" in out.get("message", "")

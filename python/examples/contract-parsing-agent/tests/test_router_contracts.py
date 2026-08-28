from pathlib import Path
from unittest.mock import MagicMock, patch

from api import dev_ui, list_contracts
from app import _find_client_dist


def test_client_dist_lookup_supports_shallow_apps_path(tmp_path: Path):
    dist = tmp_path / "client" / "dist"
    dist.mkdir(parents=True)

    assert _find_client_dist(tmp_path / "app.py", tmp_path) == dist
    assert _find_client_dist(Path("/app.py"), tmp_path / "missing") is None


def test_dev_ui_is_enabled_unless_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("APX_DEV_UI", raising=False)
    assert dev_ui() == {"enabled": True}

    monkeypatch.setenv("APX_DEV_UI", "0")
    assert dev_ui() == {"enabled": False}


def test_list_contracts_returns_rows():
    ws = MagicMock()
    expected = [
        {
            "contract_id": "CT-001",
            "counterparty": "Acme Corp",
            "contract_type": "fixed_rate",
            "effective_date": "2024-01-01",
            "expiry_date": "2026-01-01",
            "term_years": 2,
            "auto_renewal": True,
        }
    ]
    with patch(
        "api.run_sql", return_value=expected
    ) as mock_sql, patch(
        "api.get_settings"
    ) as mock_cfg:
        mock_cfg.return_value.qualified_table.return_value = "cat.sch.tbl"
        result = list_contracts(ws=ws)

    assert result == expected
    sql_arg = mock_sql.call_args[0][1]
    assert "cat.sch.tbl" in sql_arg
    assert "ORDER BY expiry_date" in sql_arg
    mock_cfg.return_value.qualified_table.assert_called_with("primary")


def test_list_contracts_coerces_types():
    ws = MagicMock()
    raw_rows = [
        {
            "contract_id": "CT-002",
            "counterparty": "Duke Energy",
            "contract_type": "ppa",
            "effective_date": "2024-06-01",
            "expiry_date": "2029-06-01",
            "term_years": "5.0",
            "auto_renewal": "false",
        }
    ]
    with patch(
        "api.run_sql", return_value=raw_rows
    ), patch(
        "api.get_settings"
    ) as mock_cfg:
        mock_cfg.return_value.qualified_table.return_value = "cat.sch.tbl"
        result = list_contracts(ws=ws)

    assert result[0]["auto_renewal"] is False
    assert result[0]["term_years"] == 5

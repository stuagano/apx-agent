from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apx_agent import ResourceSpec, ToolError
from apx_agent._resources import get_resources

from tools.extract_new_contract import extract_new_contract


FAKE_EXTRACT = {
    "counterparty": "Duke Energy",
    "contract_type": "ppa",
    "effective_date": "2026-01-01",
    "expiry_date": "2031-01-01",
    "term_years": 5,
    "pricing_model": "fixed",
    "auto_renewal": False,
}


@pytest.mark.asyncio
async def test_extract_new_contract_appends_and_returns_fields():
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract._extract_fields",
        new=AsyncMock(return_value=FAKE_EXTRACT),
    ), patch(
        "tools.extract_new_contract.run_sql",
        return_value=[],
    ) as mock_sql:
        out = await extract_new_contract(
            volume_path="/Volumes/x/y/uploads/abc.pdf",
            ws=ws,
        )
    assert out["extracted"]["counterparty"] == "Duke Energy"
    assert out["contract_id"].startswith("CT-LIVE-")
    insert_sql = mock_sql.call_args[0][1]
    assert "INSERT INTO" in insert_sql.upper()
    assert "Duke Energy" in insert_sql


@pytest.mark.asyncio
async def test_extract_new_contract_surfaces_extraction_error():
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract._extract_fields",
        new=AsyncMock(side_effect=ToolError("ai_parse_document / ai_extract failed: timeout")),
    ):
        out = await extract_new_contract(
            volume_path="/Volumes/x/y/uploads/oops.pdf", ws=ws,
        )
    assert out.get("error") == "extraction_unavailable"
    assert "timeout" in out["message"]


@pytest.mark.asyncio
async def test_extract_new_contract_rejects_non_volume_path():
    # A path outside /Volumes/ must be refused before extraction/SQL touches it.
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract._extract_fields",
        new=AsyncMock(),
    ) as mock_extract:
        out = await extract_new_contract(volume_path="/etc/passwd", ws=ws)
    assert out.get("error") == "invalid_path"
    mock_extract.assert_not_called()


@pytest.mark.asyncio
async def test_extract_new_contract_rejects_other_volume_path():
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract._extract_fields",
        new=AsyncMock(
            side_effect=ToolError(
                "path '/Volumes/other/vol/x.pdf' is outside the configured contract volumes "
                "['x.y.uploads']."
            )
        ),
    ), patch(
        "tools.extract_new_contract.run_sql",
    ) as mock_sql:
        out = await extract_new_contract(
            volume_path="/Volumes/other/vol/x.pdf", ws=ws,
        )
    assert out.get("error") == "invalid_path"
    mock_sql.assert_not_called()


@pytest.mark.asyncio
async def test_extract_new_contract_surfaces_insert_error():
    ws = MagicMock()
    with patch(
        "tools.extract_new_contract._extract_fields",
        new=AsyncMock(return_value=FAKE_EXTRACT),
    ), patch(
        "tools.extract_new_contract.run_sql",
        side_effect=RuntimeError("warehouse timeout"),
    ):
        out = await extract_new_contract(
            volume_path="/Volumes/x/y/uploads/abc.pdf",
            ws=ws,
        )
    assert out.get("error") == "insert_failed"
    assert "warehouse timeout" in out["message"]


def test_extract_new_contract_declares_warehouse_when_configured(monkeypatch):
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh-contracts")
    import importlib

    import config
    import tools.extract_new_contract as mod

    config.get_settings.cache_clear()
    importlib.reload(mod)
    try:
        kinds = {s.kind: s.identifier for s in get_resources(mod.extract_new_contract)}
        assert kinds["sql_warehouse"] == "wh-contracts"
        assert "serving_endpoint" not in kinds
        assert ResourceSpec("sql_warehouse", "wh-contracts") in get_resources(
            mod.extract_new_contract
        )
    finally:
        monkeypatch.delenv("SQL_WAREHOUSE_ID", raising=False)
        config.get_settings.cache_clear()
        importlib.reload(mod)
        config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_extract_rejects_path_outside_configured_volumes(monkeypatch):
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh-1")
    monkeypatch.setenv("VOLUMES_UPLOADS", "/Volumes/x/y/uploads")
    monkeypatch.setenv("VOLUMES_RAW", "/Volumes/x/y/raw_contracts")
    import config
    from tools import extract_new_contract as mod

    config.get_settings.cache_clear()
    mod._extractor.cache_clear()
    try:
        with patch.object(mod, "_extractor") as mock_extractor:
            out = await extract_new_contract(
                volume_path="/Volumes/other/vol/x.pdf",
                ws=MagicMock(),
            )
        assert out.get("error") == "invalid_path"
        mock_extractor.assert_not_called()
    finally:
        config.get_settings.cache_clear()
        mod._extractor.cache_clear()



@pytest.mark.asyncio
async def test_extract_fields_builds_factory_for_uploads_volume(monkeypatch):
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh-1")
    monkeypatch.setenv("VOLUMES_UPLOADS", "/Volumes/x/y/uploads")
    monkeypatch.setenv("VOLUMES_RAW", "/Volumes/x/y/raw_contracts")
    import config
    from tools.extract_new_contract import _extract_fields, _extractor

    config.get_settings.cache_clear()
    _extractor.cache_clear()
    fake = AsyncMock(return_value={"extracted": FAKE_EXTRACT, "path": "/Volumes/x/y/uploads/abc.pdf"})
    try:
        with patch("tools.extract_new_contract.document_extract_tool", return_value=fake) as factory:
            out = await _extract_fields("/Volumes/x/y/uploads/abc.pdf", MagicMock())
        assert out["counterparty"] == "Duke Energy"
        kwargs = factory.call_args.kwargs
        assert kwargs["warehouse_id"] == "wh-1"
        assert kwargs["volume"] == "x.y.uploads"
        fake.assert_awaited()
        assert fake.await_args.kwargs["path"] == "/Volumes/x/y/uploads/abc.pdf"
    finally:
        config.get_settings.cache_clear()
        _extractor.cache_clear()

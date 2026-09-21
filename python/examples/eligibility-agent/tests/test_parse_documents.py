from unittest.mock import AsyncMock, patch

import pytest
from apx_agent import ResourceSpec, ToolError
from apx_agent._resources import get_resources
from tools.parse_documents import parse_documents


def test_parse_documents_declares_documents_table_not_vision():
    kinds = {s.kind: s.identifier for s in get_resources(parse_documents)}
    assert kinds["uc_table"].endswith(".documents")
    assert "serving_endpoint" not in kinds


def test_parse_documents_declares_warehouse_when_configured(monkeypatch):
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh-elig")
    import importlib

    import config
    import tools.parse_documents as mod

    config.get_settings.cache_clear()
    importlib.reload(mod)
    try:
        kinds = {s.kind: s.identifier for s in get_resources(mod.parse_documents)}
        assert kinds["sql_warehouse"] == "wh-elig"
        assert "serving_endpoint" not in kinds
        assert ResourceSpec("sql_warehouse", "wh-elig") in get_resources(mod.parse_documents)
    finally:
        monkeypatch.delenv("SQL_WAREHOUSE_ID", raising=False)
        config.get_settings.cache_clear()
        importlib.reload(mod)
        config.get_settings.cache_clear()


@pytest.mark.asyncio
@patch("tools.parse_documents._extract_document", new_callable=AsyncMock)
@patch("tools.parse_documents._list_documents")
async def test_routes_each_doc_through_document_extract(mock_list, mock_extract):
    mock_list.return_value = [
        {"document_id": "d1", "document_type": "paystub", "volume_path": "/Volumes/main/eligibility_demo/documents/p1.pdf", "ocr_quality_hint": "good"},
        {"document_id": "d2", "document_type": "w2", "volume_path": "/Volumes/main/eligibility_demo/documents/w2.pdf", "ocr_quality_hint": "good"},
    ]
    mock_extract.side_effect = [
        {"employee_name": "Alice Smith", "gross_pay": 1842.30},
        {"employee_name": "Alice Smith", "annual_wages": 48210.00},
    ]
    result = await parse_documents("A-001", ws=None)
    assert len(result["documents"]) == 2
    assert result["documents"][0]["extracted"]["gross_pay"] == 1842.30
    assert mock_extract.await_count == 2
    assert mock_extract.await_args_list[0].args[1] == "paystub"
    assert mock_extract.await_args_list[1].args[1] == "w2"


@pytest.mark.asyncio
@patch("tools.parse_documents._extract_document", new_callable=AsyncMock)
@patch("tools.parse_documents._list_documents")
async def test_low_ocr_quality_flagged(mock_list, mock_extract):
    mock_list.return_value = [
        {"document_id": "d1", "document_type": "paystub", "volume_path": "/Volumes/main/eligibility_demo/documents/p1.pdf", "ocr_quality_hint": "poor"},
    ]
    mock_extract.return_value = {"gross_pay": 1842.30}
    result = await parse_documents("A-001", ws=None)
    assert result["documents"][0]["confidence_concern"] is True


@pytest.mark.asyncio
@patch("tools.parse_documents._list_documents")
async def test_unknown_type_is_toolerror_no_vision_fallback(mock_list):
    mock_list.return_value = [
        {"document_id": "d1", "document_type": "passport", "volume_path": "/Volumes/main/eligibility_demo/documents/x.pdf", "ocr_quality_hint": "good"},
    ]
    with pytest.raises(ToolError, match="unsupported document_type"):
        await parse_documents("A-001", ws=None)



@pytest.mark.asyncio
async def test_extract_document_dispatches_to_type_schema(monkeypatch):
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh-elig")
    import config
    from tools.parse_documents import _DOC_TYPES, _extract_document, _extractors

    config.get_settings.cache_clear()
    _extractors.cache_clear()
    fake = AsyncMock(return_value={"extracted": {"employee_name": "Alice", "gross_pay": 10.0}})
    try:
        with patch("tools.parse_documents.document_extract_tool", return_value=fake) as factory:
            out = await _extract_document(
                "/Volumes/main/eligibility_demo/documents/p1.pdf", "paystub", ws=None
            )
        assert out["gross_pay"] == 10.0
        names = [c.kwargs["name"] for c in factory.call_args_list]
        assert names == [f"extract_{t}" for t in _DOC_TYPES]
        volumes = {c.kwargs["volume"] for c in factory.call_args_list}
        assert volumes == {"main.eligibility_demo.documents"}
        fake.assert_awaited()
        assert fake.await_args.kwargs["path"].endswith("/documents/p1.pdf")
    finally:
        config.get_settings.cache_clear()
        _extractors.cache_clear()

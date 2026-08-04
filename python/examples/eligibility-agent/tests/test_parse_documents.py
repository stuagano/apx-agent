from unittest.mock import patch

from apx_agent import ResourceSpec
from apx_agent._resources import get_resources
from tools.parse_documents import _VISION_ENDPOINT, parse_documents


def test_parse_documents_declares_vision_endpoint_and_documents_table():
    kinds = {s.kind: s.identifier for s in get_resources(parse_documents)}
    assert kinds["serving_endpoint"] == _VISION_ENDPOINT
    assert kinds["uc_table"].endswith(".documents")
    assert ResourceSpec("serving_endpoint", _VISION_ENDPOINT) in get_resources(parse_documents)


@patch("tools.parse_documents._extract_via_vision")
@patch("tools.parse_documents._list_documents")
def test_routes_each_doc_through_vision(mock_list, mock_extract):
    mock_list.return_value = [
        {"document_id": "d1", "document_type": "paystub", "volume_path": "/Volumes/x/p1.pdf", "ocr_quality_hint": "good"},
        {"document_id": "d2", "document_type": "w2", "volume_path": "/Volumes/x/w2.pdf", "ocr_quality_hint": "good"},
    ]
    mock_extract.side_effect = [
        {"employee_name": "Alice Smith", "gross_pay": 1842.30},
        {"employee_name": "Alice Smith", "annual_wages": 48210.00},
    ]
    result = parse_documents("A-001", ws=None)
    assert len(result["documents"]) == 2
    assert result["documents"][0]["extracted"]["gross_pay"] == 1842.30


@patch("tools.parse_documents._extract_via_vision")
@patch("tools.parse_documents._list_documents")
def test_low_ocr_quality_flagged(mock_list, mock_extract):
    mock_list.return_value = [
        {"document_id": "d1", "document_type": "paystub", "volume_path": "/Volumes/x/p1.pdf", "ocr_quality_hint": "poor"},
    ]
    mock_extract.return_value = {"gross_pay": 1842.30}
    result = parse_documents("A-001", ws=None)
    assert result["documents"][0]["confidence_concern"] is True

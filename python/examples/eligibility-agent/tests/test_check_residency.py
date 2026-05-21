import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def set_state(monkeypatch):
    monkeypatch.setenv("STATE_CODE", "CA")
    # Reset lru_cache so env var takes effect
    import config
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


from tools.check_residency import check_residency

_HOUSEHOLD = {"residence_address": "100 Main St", "residence_city": "Sacramento", "residence_state": "CA"}


def _parsed(address="100 Main St", csz="Sacramento CA 95814", days_ago=10):
    return {"documents": [{"document_type": "residency", "extracted": {
        "address_line": address,
        "city_state_zip": csz,
        "document_date": (date.today() - timedelta(days=days_ago)).isoformat(),
    }}]}


def test_matching_address_and_state_passes():
    result = check_residency(_parsed(), _HOUSEHOLD)
    assert result["verified"] is True


def test_address_mismatch_fails():
    result = check_residency(_parsed(address="999 Other Ave"), _HOUSEHOLD)
    assert result["verified"] is False
    assert any("address" in i.lower() for i in result["issues"])


def test_old_document_fails_recency():
    result = check_residency(_parsed(days_ago=90), _HOUSEHOLD)
    assert result["verified"] is False
    assert any("day" in i.lower() for i in result["issues"])


def test_wrong_state_fails():
    result = check_residency(_parsed(csz="Austin TX 78701"), _HOUSEHOLD)
    assert result["verified"] is False
    assert any("CA" in i for i in result["issues"])


def test_missing_residency_doc_fails():
    result = check_residency({"documents": []}, _HOUSEHOLD)
    assert result["verified"] is False

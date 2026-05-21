from unittest.mock import patch

import pytest

from tools.get_household import get_household


@patch("tools.get_household._fetch_row")
def test_returns_household_dict(mock_fetch):
    mock_fetch.return_value = {
        "household_id": "h-1",
        "primary_filer_name": "Alice Smith",
        "secondary_filer_name": None,
        "household_size": 3,
        "residence_address": "100 Main St",
        "residence_city": "Sacramento",
        "residence_state": "CA",
        "residence_zip": "95814",
    }
    result = get_household("A-001", ws=None)
    assert result["household_id"] == "h-1"
    assert result["household_size"] == 3


@patch("tools.get_household._fetch_row")
def test_raises_when_not_found(mock_fetch):
    mock_fetch.return_value = None
    with pytest.raises(ValueError, match="no household found"):
        get_household("A-MISSING", ws=None)

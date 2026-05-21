from unittest.mock import MagicMock
from agent import who_am_i


def test_who_am_i_formats_display_name_and_email():
    mock_ws = MagicMock()
    mock_ws.current_user.me.return_value = MagicMock(
        display_name="Alice Smith",
        user_name="alice@example.com",
    )
    result = who_am_i(mock_ws)
    assert result == "Alice Smith (alice@example.com)"


def test_who_am_i_calls_current_user_me():
    mock_ws = MagicMock()
    mock_ws.current_user.me.return_value = MagicMock(
        display_name="Bob Jones",
        user_name="bob@example.com",
    )
    who_am_i(mock_ws)
    mock_ws.current_user.me.assert_called_once()

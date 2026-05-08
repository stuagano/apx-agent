import pytest
from unittest.mock import MagicMock, patch
from tools.poll_deployment import poll_deployment


def _make_app(api_state="RUNNING", deploy_state="SUCCEEDED", url="https://mcp-test.databricksapps.com"):
    app = MagicMock()
    app.app_status.state.value = api_state
    app.active_deployment.status.state.value = deploy_state
    app.url = url
    return app


def test_returns_url_after_both_stages_pass():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    with patch("httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        result = poll_deployment("mcp-test", ws)

    assert result == "https://mcp-test.databricksapps.com"


def test_stage1_retries_until_running():
    ws = MagicMock()
    ws.apps.get.side_effect = [
        _make_app(api_state="DEPLOYING", deploy_state="IN_PROGRESS"),
        _make_app(api_state="RUNNING", deploy_state="SUCCEEDED"),
    ]

    with patch("httpx.get") as mock_get, patch("time.sleep"):
        mock_get.return_value = MagicMock(status_code=200)
        result = poll_deployment("mcp-test", ws)

    assert ws.apps.get.call_count == 2
    assert result == "https://mcp-test.databricksapps.com"


def test_stage2_retries_until_health_200():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    with patch("httpx.get") as mock_get, patch("time.sleep"):
        mock_get.side_effect = [
            Exception("connection refused"),
            MagicMock(status_code=200),
        ]
        result = poll_deployment("mcp-test", ws)

    assert mock_get.call_count == 2
    assert result == "https://mcp-test.databricksapps.com"


def test_stage2_timeout_returns_warning_url():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    # time.time returns: start(0), stage1 check(1), stage2 start(2), then always past deadline
    time_values = [0, 1, 2] + [200] * 20

    with patch("httpx.get", side_effect=Exception("down")), \
         patch("time.sleep"), \
         patch("time.time", side_effect=time_values):
        result = poll_deployment("mcp-test", ws)

    assert "https://mcp-test.databricksapps.com" in result
    assert "30 seconds" in result


def test_stage1_timeout_raises():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app(api_state="DEPLOYING", deploy_state="IN_PROGRESS")

    # time.time: start(0), then immediately past 120s deadline
    time_values = [0] + [200] * 5

    with patch("time.sleep"), patch("time.time", side_effect=time_values):
        with pytest.raises(TimeoutError, match="RUNNING"):
            poll_deployment("mcp-test", ws)

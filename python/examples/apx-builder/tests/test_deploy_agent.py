from unittest.mock import MagicMock
from databricks.sdk.errors import NotFound
from tools.deploy_agent import deploy_agent


def test_creates_app_when_it_does_not_exist():
    ws = MagicMock()
    ws.apps.get.side_effect = NotFound("not found")

    deploy_agent("mcp-my-agent", "/Users/test/apx-builder/mcp-my-agent", ws)

    ws.apps.create.assert_called_once_with(
        name="mcp-my-agent",
        description="Agent built by apx-builder",
    )


def test_skips_create_when_app_already_exists():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()

    deploy_agent("mcp-my-agent", "/Users/test/apx-builder/mcp-my-agent", ws)

    ws.apps.create.assert_not_called()


def test_always_calls_deploy_with_correct_path():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()
    workspace_path = "/Users/test@example.com/apx-builder/mcp-my-agent"

    deploy_agent("mcp-my-agent", workspace_path, ws)

    ws.apps.deploy.assert_called_once()
    kwargs = ws.apps.deploy.call_args[1]
    assert kwargs["app_name"] == "mcp-my-agent"
    assert kwargs["source_code_path"] == workspace_path


def test_returns_app_name():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()

    result = deploy_agent("mcp-my-agent", "/some/path", ws)

    assert result == "mcp-my-agent"

"""Tests for mcp_loader.py — verifies server loading and custom tool structure."""
from unittest.mock import MagicMock, patch


def test_get_mcp_servers_returns_apx_server():
    """get_mcp_servers() returns a dict with apx key."""
    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)
        servers, tool_names = mcp_loader.get_mcp_servers()

    import mcp_loader as _cleanup
    _cleanup._apx_server = None

    assert "apx" in servers
    assert "databricks" not in servers


def test_apx_tool_names_are_present():
    """The apx MCP server tool names are mcp__apx__ prefixed."""
    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)
        _, tool_names = mcp_loader.get_mcp_servers()

    import mcp_loader as _cleanup
    _cleanup._apx_server = None

    assert "mcp__apx__manage_workspace_files" in tool_names
    assert "mcp__apx__create_and_deploy_app" in tool_names
    assert "mcp__apx__get_app_status" in tool_names


def test_manage_workspace_files_upload_succeeds(tmp_path):
    """_upload_directory uploads all files in the directory."""
    (tmp_path / "app.py").write_text("from fastapi import FastAPI")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")

    fake_ws = MagicMock()
    fake_ws.workspace.mkdirs.return_value = None
    fake_ws.workspace.import_.return_value = None

    with patch("mcp_loader.get_workspace_client", return_value=fake_ws):
        from mcp_loader import _upload_directory
        result = _upload_directory({
            "action": "upload",
            "local_path": str(tmp_path),
            "workspace_path": "/Workspace/Users/test@example.com/mcp-test",
        })

    assert result["status"] == "success"
    assert result["files_uploaded"] == 2


def test_manage_workspace_files_unsupported_action():
    """Non-upload actions return an error."""
    with patch("mcp_loader.get_workspace_client", return_value=MagicMock()):
        from mcp_loader import _upload_directory
        result = _upload_directory({
            "action": "delete",
            "local_path": "/tmp/x",
            "workspace_path": "/Workspace/test",
        })

    assert "error" in result
    assert "upload" in result["error"]


def test_make_wrapper_not_needed():
    """mcp_loader no longer uses _make_wrapper (databricks server removed)."""
    import mcp_loader
    assert not hasattr(mcp_loader, "_make_wrapper")

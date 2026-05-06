"""Tests for mcp_loader.py — verifies server loading and custom tool structure."""
from unittest.mock import AsyncMock, MagicMock, patch


def test_get_mcp_servers_returns_both_servers():
    """get_mcp_servers() returns a dict with databricks and apx keys."""
    fake_mcp = MagicMock()
    fake_mcp.list_tools = AsyncMock(return_value=[])

    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server) as mock_create, \
         patch.dict("sys.modules", {
             "databricks_mcp_server": MagicMock(),
             "databricks_mcp_server.server": MagicMock(mcp=fake_mcp),
             "databricks_mcp_server.tools": MagicMock(),
             "databricks_mcp_server.tools.sql": MagicMock(),
             "databricks_mcp_server.tools.file": MagicMock(),
             "databricks_mcp_server.tools.genie": MagicMock(),
             "databricks_mcp_server.tools.compute": MagicMock(),
         }):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)
        # reload() reinitializes singletons to None — no manual reset needed
        servers, tool_names = mcp_loader.get_mcp_servers()

    # Reset singletons after the patch block so state does not leak to other tests
    import mcp_loader as _mcp_loader_cleanup
    _mcp_loader_cleanup._databricks_server = None
    _mcp_loader_cleanup._databricks_tool_names = None
    _mcp_loader_cleanup._apx_server = None

    assert "databricks" in servers
    assert "apx" in servers


def test_apx_tool_names_are_present():
    """The apx MCP server tool names are mcp__apx__ prefixed."""
    fake_mcp = MagicMock()
    fake_mcp.list_tools = AsyncMock(return_value=[])
    fake_server = MagicMock()

    with patch("mcp_loader.create_sdk_mcp_server", return_value=fake_server), \
         patch.dict("sys.modules", {
             "databricks_mcp_server": MagicMock(),
             "databricks_mcp_server.server": MagicMock(mcp=fake_mcp),
             "databricks_mcp_server.tools": MagicMock(),
             "databricks_mcp_server.tools.sql": MagicMock(),
             "databricks_mcp_server.tools.file": MagicMock(),
             "databricks_mcp_server.tools.genie": MagicMock(),
             "databricks_mcp_server.tools.compute": MagicMock(),
         }):
        import importlib
        import mcp_loader
        importlib.reload(mcp_loader)
        # reload() reinitializes singletons to None — no manual reset needed
        _, tool_names = mcp_loader.get_mcp_servers()

    # Reset singletons after the patch block so state does not leak to other tests
    import mcp_loader as _mcp_loader_cleanup
    _mcp_loader_cleanup._databricks_server = None
    _mcp_loader_cleanup._databricks_tool_names = None
    _mcp_loader_cleanup._apx_server = None

    assert "mcp__apx__create_and_deploy_app" in tool_names
    assert "mcp__apx__get_app_status" in tool_names


def test_convert_schema_maps_string_to_str():
    from mcp_loader import _convert_schema

    schema = {"properties": {"table_name": {"type": "string"}, "limit": {"type": "integer"}}}
    result = _convert_schema(schema)

    assert result["table_name"] is str
    assert result["limit"] is int


def test_convert_schema_handles_anyof_optional():
    from mcp_loader import _convert_schema

    schema = {"properties": {"name": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    result = _convert_schema(schema)

    assert result["name"] is str


def test_make_wrapper_handles_async_fn():
    """_make_wrapper must await async tool functions instead of returning the coroutine."""
    from unittest.mock import MagicMock, patch

    async def async_tool(query: str):
        return {"rows": [{"result": "ok"}]}

    with patch("mcp_loader.tool", side_effect=lambda name, desc, schema: lambda fn: fn):
        from mcp_loader import _make_wrapper
        wrapper = _make_wrapper("test_tool", "desc", {"query": str}, async_tool)

    result = wrapper({"query": "SELECT 1"})
    text = result["content"][0]["text"]
    assert "ok" in text
    assert "coroutine" not in text

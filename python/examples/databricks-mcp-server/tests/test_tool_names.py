"""Verify TOOL_NAMES matches tools actually registered in the MCP server."""
import asyncio


def test_tool_names_matches_registered_tools():
    from databricks_mcp_server import TOOL_NAMES
    from databricks_mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    declared = set(TOOL_NAMES)
    missing = registered - declared
    extra = declared - registered
    assert not missing, f"Tools registered but missing from TOOL_NAMES: {missing}"
    assert not extra, f"Names in TOOL_NAMES but not registered: {extra}"

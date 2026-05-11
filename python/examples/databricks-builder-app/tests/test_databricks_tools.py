"""Tests for SSE MCP server config function."""
import importlib.util
import os
import sys
import pytest


def _load_module():
    """Load databricks_tools module directly, bypassing server/services/__init__.py."""
    spec = importlib.util.spec_from_file_location(
        "server.services.databricks_tools",
        os.path.join(os.path.dirname(__file__), "..", "server", "services", "databricks_tools.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Remove cached version if present so monkeypatched env vars take effect
    sys.modules.pop("server.services.databricks_tools", None)
    spec.loader.exec_module(mod)
    return mod


def test_returns_sse_config_type(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    mod = _load_module()
    config, _ = mod.get_databricks_server_config()
    assert config["type"] == "sse"


def test_returns_correct_url(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    mod = _load_module()
    config, _ = mod.get_databricks_server_config()
    assert config["url"] == "http://localhost:8080/sse"


def test_tool_names_prefixed(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    mod = _load_module()
    _, tool_names = mod.get_databricks_server_config()
    assert all(n.startswith("mcp__databricks__") for n in tool_names)
    assert "mcp__databricks__execute_sql" in tool_names
    assert "mcp__databricks__list_warehouses" in tool_names


def test_tool_names_nonempty(monkeypatch):
    monkeypatch.setenv("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse")
    mod = _load_module()
    _, tool_names = mod.get_databricks_server_config()
    assert len(tool_names) > 0


def test_raises_without_url(monkeypatch):
    monkeypatch.delenv("DATABRICKS_MCP_SERVER_URL", raising=False)
    mod = _load_module()
    with pytest.raises(ValueError, match="DATABRICKS_MCP_SERVER_URL"):
        mod.get_databricks_server_config()

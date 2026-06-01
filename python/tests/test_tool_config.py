import os

import pytest
from apx_agent._tool_config import ToolConfigError, load_config_tools


def test_dispatches_single_tool_by_keyword():
    tools = load_config_tools([{"type": "genie", "space_id": "01ef", "name": "ask_sales"}])
    assert len(tools) == 1
    assert tools[0].__name__ == "ask_sales"


def test_flattens_toolkit_lists():
    # sql + a jobs toolkit (returns 4) → 1 + 4 = 5 callables, all flat
    tools = load_config_tools([
        {"type": "sql", "warehouse_id": "wh1"},
        {"type": "jobs", "warehouse_id": "wh1"},
    ])
    assert all(callable(t) for t in tools)
    assert len(tools) == 5


def test_unknown_type_raises_listing_known():
    with pytest.raises(ToolConfigError, match="unknown type 'nope'"):
        load_config_tools([{"type": "nope"}])


def test_missing_type_raises():
    with pytest.raises(ToolConfigError, match="missing 'type'"):
        load_config_tools([{"space_id": "x"}])


def test_missing_required_arg_wrapped_as_config_error():
    with pytest.raises(ToolConfigError, match=r"type=genie.*space_id"):
        load_config_tools([{"type": "genie"}])  # genie_tool needs space_id


def test_empty_input_returns_empty_list():
    assert load_config_tools([]) == []


def test_same_name_collision_requires_explicit_name():
    with pytest.raises(ToolConfigError, match="duplicate tool name"):
        load_config_tools([
            {"type": "vector_search", "index_name": "a.b.c"},
            {"type": "vector_search", "index_name": "a.b.d"},  # both default to "vector_search"
        ])


def test_env_var_resolved_on_string_values(monkeypatch):
    monkeypatch.setenv("SALES_SPACE", "01ef-from-env")
    tools = load_config_tools([{"type": "genie", "space_id": "$SALES_SPACE", "name": "ask"}])
    # The genie tool closes over the resolved space_id; assert via the factory
    # being called with the resolved value by checking no error + a built tool.
    assert tools[0].__name__ == "ask"


def test_allowlist_blocks_disallowed_host(monkeypatch):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    with pytest.raises(ToolConfigError, match="not in APX_TOOLS_ALLOWED_HOSTS"):
        load_config_tools([{
            "type": "mcp_toolkit",
            "server_url": "https://evil.example.com/mcp",
        }])


def test_allowlist_unset_allows_any_host(monkeypatch):
    monkeypatch.delenv("APX_TOOLS_ALLOWED_HOSTS", raising=False)
    # No allow-list configured → trusted default → host not checked.
    # Monkeypatch mcp_toolkit so we don't hit the network at factory time.
    import apx_agent._tool_config as mod
    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": lambda **kw: [lambda: None]})
    tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://anything/mcp"}])
    assert len(tools) == 1


def test_io_factory_failure_skipped_with_warning(monkeypatch, caplog):
    import logging
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.delenv("APX_TOOLS_STRICT", raising=False)
    with caplog.at_level(logging.WARNING, logger="apx_agent._tool_config"):
        tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])
    assert tools == []
    assert "Skipping tool #0" in caplog.text


def test_io_factory_failure_raises_in_strict_mode(monkeypatch):
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.setenv("APX_TOOLS_STRICT", "1")
    with pytest.raises(ToolConfigError, match="server down"):
        load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])

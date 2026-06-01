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

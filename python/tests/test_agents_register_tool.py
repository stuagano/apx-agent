from apx_agent import Agent


def _a_tool(query: str) -> str:
    """Echo tool."""
    return query


def _b_tool(number: int) -> int:
    """Double tool."""
    return number * 2


def _a_tool_v2(query: str) -> str:
    """Echo tool replacement."""
    return query.upper()


_a_tool_v2.__name__ = "_a_tool"


def test_register_tool_updates_both_lists_and_collect_tools():
    agent = Agent(tools=[])
    before = len(agent.collect_tools())
    agent._register_tool(_a_tool)
    assert _a_tool in agent._tool_fns
    # _analyzed must grow too, or collect_tools()/build_router() won't see it
    assert len(agent._analyzed) == len(agent._tool_fns)
    names = [t.name for t in agent.collect_tools()]
    assert "_a_tool" in names
    assert len(agent.collect_tools()) == before + 1


def test_public_register_tool_replaces_same_name_and_bumps_version():
    agent = Agent(tools=[_a_tool])
    assert agent.tools_version == 0

    agent.register_tool(_a_tool_v2)

    assert agent._tool_fns == [_a_tool_v2]
    assert len(agent._analyzed) == 1
    assert [t.name for t in agent.collect_tools()] == ["_a_tool"]
    assert agent.tools_version == 1


def test_unregister_tool_removes_from_both_lists_and_returns_bool():
    agent = Agent(tools=[_a_tool, _b_tool])
    v0 = agent.tools_version

    assert agent.unregister_tool("_a_tool") is True

    assert _a_tool not in agent._tool_fns
    assert len(agent._analyzed) == len(agent._tool_fns) == 1
    assert [t.name for t in agent.collect_tools()] == ["_b_tool"]
    assert agent.tools_version == v0 + 1


def test_unregister_absent_tool_is_noop():
    agent = Agent(tools=[_a_tool])
    v0 = agent.tools_version

    assert agent.unregister_tool("does_not_exist") is False

    assert agent._tool_fns == [_a_tool]
    assert agent.tools_version == v0

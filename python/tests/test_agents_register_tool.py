from apx_agent import Agent


def _a_tool(query: str) -> str:
    """Echo tool."""
    return query


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

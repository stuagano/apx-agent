def test_native_agent_declares_the_discovery_tools_and_shipped_skill():
    from agent import agent

    assert agent._name == "discovery"
    assert {tool.__name__ for tool in agent._tool_fns} == {
        "fetch_web_page",
        "nonprofit_discovery",
    }

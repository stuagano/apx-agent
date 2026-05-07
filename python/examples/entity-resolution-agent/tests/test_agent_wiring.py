def test_agent_is_handoff_agent():
    from apx_agent import HandoffAgent
    from entity_resolution_agent.backend.agent_router import agent
    assert isinstance(agent, HandoffAgent)


def test_agent_has_supervisor_and_evaluator():
    from entity_resolution_agent.backend.agent_router import agent
    assert "supervisor" in agent._agents
    assert "evaluator" in agent._agents


def test_agent_starts_with_supervisor():
    from entity_resolution_agent.backend.agent_router import agent
    assert agent._start == "supervisor"


def test_supervisor_has_two_tools():
    """Supervisor should expose normalize_record and search_accounts (not vector/sql directly)."""
    from entity_resolution_agent.backend.agent_router import agent
    sup = agent._agents["supervisor"]
    tool_names = [t.__name__ for t in sup._tool_fns]
    assert "normalize_record" in tool_names
    assert "search_accounts" in tool_names
    assert "vector_search" not in tool_names
    assert "sql_search" not in tool_names

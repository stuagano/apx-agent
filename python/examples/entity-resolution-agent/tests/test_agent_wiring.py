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


def test_all_tools_registered():
    from entity_resolution_agent.backend.agent_router import agent
    tool_names = {t.name for t in agent.collect_tools()}
    assert "normalize_record" in tool_names
    assert "vector_search" in tool_names
    assert "sql_search" in tool_names
    assert "evaluate_candidates" in tool_names
    assert "log_decision" in tool_names

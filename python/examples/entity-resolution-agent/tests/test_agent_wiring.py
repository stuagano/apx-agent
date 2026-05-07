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

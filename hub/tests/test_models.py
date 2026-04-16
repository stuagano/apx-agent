from hub.models import HubAgent, HubSkill, RegisterRequest, RegisterResponse, ChatRequest, ChatResponse


def test_hub_skill_construction():
    skill = HubSkill(name="query", description="Run a SQL query")
    assert skill.name == "query"
    assert skill.description == "Run a SQL query"


def test_hub_agent_construction():
    agent = HubAgent(
        name="billing-agent",
        description="Handles billing questions",
        source="apx",
        url="https://billing-agent.workspace.databricksapps.com",
    )
    assert agent.name == "billing-agent"
    assert agent.source == "apx"
    assert agent.status == "unknown"
    assert agent.skills == []
    assert agent.metadata == {}
    assert agent.id != ""


def test_hub_agent_id_deterministic():
    a1 = HubAgent(name="test", description="d", source="apx")
    a2 = HubAgent(name="test", description="d", source="apx")
    assert a1.id == a2.id


def test_hub_agent_id_differs_by_source():
    a1 = HubAgent(name="test", description="d", source="apx")
    a2 = HubAgent(name="test", description="d", source="serving_endpoint")
    assert a1.id != a2.id


def test_register_request():
    req = RegisterRequest(url="https://my-agent.workspace.databricksapps.com")
    assert req.url == "https://my-agent.workspace.databricksapps.com"


def test_register_response():
    resp = RegisterResponse(id="apx:billing-agent")
    assert resp.id == "apx:billing-agent"


def test_chat_request():
    req = ChatRequest(agent_id="apx:billing", message="Hello")
    assert req.conversation_id is None


def test_chat_response():
    resp = ChatResponse(agent_id="apx:billing", message="Hi!", conversation_id="conv-1")
    assert resp.conversation_id == "conv-1"

import pytest
from hub.models import HubAgent
from hub.registry import AgentRegistry


@pytest.fixture
def registry() -> AgentRegistry:
    return AgentRegistry()


@pytest.fixture
def sample_agent() -> HubAgent:
    return HubAgent(
        name="billing-agent",
        description="Handles billing",
        source="apx",
        url="https://billing.example.com",
        status="online",
    )


def test_empty_registry(registry: AgentRegistry):
    assert registry.list() == []


def test_add_and_get(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    assert registry.get(sample_agent.id) == sample_agent


def test_add_duplicate_updates(registry: AgentRegistry):
    agent_v1 = HubAgent(name="a", description="v1", source="apx", status="online")
    agent_v2 = HubAgent(name="a", description="v2", source="apx", status="offline")
    registry.add(agent_v1)
    registry.add(agent_v2)
    assert len(registry.list()) == 1
    assert registry.get(agent_v1.id).description == "v2"


def test_get_missing_returns_none(registry: AgentRegistry):
    assert registry.get("nonexistent") is None


def test_list_all(registry: AgentRegistry):
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="serving_endpoint"))
    registry.add(HubAgent(name="a3", description="d", source="genie_space"))
    assert len(registry.list()) == 3


def test_list_filter_by_source(registry: AgentRegistry):
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="serving_endpoint"))
    registry.add(HubAgent(name="a3", description="d", source="genie_space"))
    apx_agents = registry.list(source="apx")
    assert len(apx_agents) == 1
    assert apx_agents[0].name == "a1"


def test_list_filter_by_query(registry: AgentRegistry):
    registry.add(HubAgent(name="billing-agent", description="Handles billing", source="apx"))
    registry.add(HubAgent(name="data-triage", description="Investigates data issues", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1
    assert results[0].name == "billing-agent"


def test_list_query_searches_description(registry: AgentRegistry):
    registry.add(HubAgent(name="agent-x", description="Handles billing questions", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1


def test_list_query_case_insensitive(registry: AgentRegistry):
    registry.add(HubAgent(name="Billing-Agent", description="d", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1


def test_remove(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    registry.remove(sample_agent.id)
    assert registry.get(sample_agent.id) is None


def test_remove_missing_is_noop(registry: AgentRegistry):
    registry.remove("nonexistent")  # should not raise


def test_update_status(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    registry.update_status(sample_agent.id, "offline")
    assert registry.get(sample_agent.id).status == "offline"

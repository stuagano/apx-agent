import pytest
from unittest.mock import MagicMock
from hub.discovery import discover_serving_endpoints, discover_genie_spaces
from hub.models import HubAgent


def _make_endpoint(name: str, task: str = "llm/v1/chat", state: str = "READY", tags: dict | None = None):
    ep = MagicMock()
    ep.name = name
    ep.task = task
    ep.state = MagicMock()
    ep.state.ready = state
    ep.tags = tags or {}
    return ep


def test_discover_serving_endpoints_chat_task():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("my-agent", task="llm/v1/chat"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1
    assert agents[0].name == "my-agent"
    assert agents[0].source == "serving_endpoint"
    assert agents[0].status == "online"


def test_discover_serving_endpoints_skips_non_agent():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("embeddings-model", task="llm/v1/embeddings"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 0


def test_discover_serving_endpoints_includes_tagged():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("custom-agent", task="custom", tags={"agent": "true"}),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1


def test_discover_serving_endpoints_not_ready():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("my-agent", task="llm/v1/chat", state="NOT_READY"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1
    assert agents[0].status == "offline"


def test_discover_genie_spaces():
    ws = MagicMock()
    ws.api_client.do.return_value = {"spaces": [
        {"space_id": "abc123", "title": "Sales Analytics", "description": "Sales metrics"},
    ]}
    agents = discover_genie_spaces(ws)
    assert len(agents) == 1
    assert agents[0].name == "Sales Analytics"
    assert agents[0].source == "genie_space"
    assert agents[0].metadata["space_id"] == "abc123"
    assert agents[0].status == "online"


def test_discover_genie_spaces_empty():
    ws = MagicMock()
    ws.api_client.do.return_value = {"spaces": []}
    agents = discover_genie_spaces(ws)
    assert len(agents) == 0


def test_discover_genie_spaces_api_error():
    ws = MagicMock()
    ws.api_client.do.side_effect = Exception("API error")
    agents = discover_genie_spaces(ws)
    assert len(agents) == 0

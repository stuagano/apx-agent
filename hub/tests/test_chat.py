import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from hub.chat import proxy_chat
from hub.models import HubAgent, ChatRequest, ChatResponse


@pytest.fixture
def apx_agent() -> HubAgent:
    return HubAgent(
        name="billing",
        description="Billing agent",
        source="apx",
        url="https://billing.example.com",
        status="online",
    )


@pytest.fixture
def serving_agent() -> HubAgent:
    return HubAgent(
        name="my-model",
        description="A model",
        source="serving_endpoint",
        url="my-model",
        status="online",
    )


@pytest.fixture
def genie_agent() -> HubAgent:
    return HubAgent(
        name="Sales Genie",
        description="Sales analytics",
        source="genie_space",
        status="online",
        metadata={"space_id": "abc123"},
    )


@pytest.mark.asyncio
async def test_proxy_chat_apx_agent(apx_agent: HubAgent):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output_text": "Your bill is $42.",
    }
    mock_response.raise_for_status = MagicMock()

    with patch("hub.chat.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        request = ChatRequest(agent_id=apx_agent.id, message="What's my bill?")
        result = await proxy_chat(request, apx_agent, headers={})

        assert isinstance(result, ChatResponse)
        assert result.message == "Your bill is $42."
        assert result.conversation_id is not None

        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert call_url == "https://billing.example.com/responses"


@pytest.mark.asyncio
async def test_proxy_chat_serving_endpoint(serving_agent: HubAgent):
    ws = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Model response"
    ws.serving_endpoints.query.return_value = mock_response

    request = ChatRequest(agent_id=serving_agent.id, message="Hello")
    result = await proxy_chat(request, serving_agent, headers={}, ws=ws)

    assert result.message == "Model response"
    ws.serving_endpoints.query.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_chat_genie_space(genie_agent: HubAgent):
    ws = MagicMock()
    # First call: start conversation; second: message still pending; third: completed
    ws.api_client.do.side_effect = [
        {"conversation_id": "conv-new", "message_id": "msg-1"},
        {"status": "EXECUTING_QUERY"},
        {"status": "COMPLETED", "attachments": [{"text": {"content": "Sales are up 10%"}}]},
    ]

    with patch("hub.chat.asyncio.sleep", new_callable=AsyncMock):
        request = ChatRequest(agent_id=genie_agent.id, message="How are sales?")
        result = await proxy_chat(request, genie_agent, headers={}, ws=ws)

    assert result.message == "Sales are up 10%"
    assert result.conversation_id is not None


@pytest.mark.asyncio
async def test_proxy_chat_genie_space_failed(genie_agent: HubAgent):
    ws = MagicMock()
    ws.api_client.do.side_effect = [
        {"conversation_id": "conv-new", "message_id": "msg-1"},
        {"status": "FAILED"},
    ]

    with patch("hub.chat.asyncio.sleep", new_callable=AsyncMock):
        request = ChatRequest(agent_id=genie_agent.id, message="Bad query")
        result = await proxy_chat(request, genie_agent, headers={}, ws=ws)

    assert "failed" in result.message.lower()


@pytest.mark.asyncio
async def test_proxy_chat_agent_no_url():
    agent = HubAgent(name="broken", description="d", source="apx", url=None)
    request = ChatRequest(agent_id=agent.id, message="Hello")

    with pytest.raises(ValueError, match="no URL configured"):
        await proxy_chat(request, agent, headers={})

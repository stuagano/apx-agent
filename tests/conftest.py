"""Shared fixtures for apx-agent tests."""

from __future__ import annotations

from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, Request, params
from pydantic import BaseModel

from apx_agent import Agent, AgentConfig, AgentContext, AgentTool, Dependencies, Message
from apx_agent._llm_loop import _build_tool_schemas


# ---------------------------------------------------------------------------
# Fake dependency for simulating Dependencies.Client / UserClient
# ---------------------------------------------------------------------------


class FakeWorkspace:
    """Stub for databricks.sdk.WorkspaceClient."""
    pass


FakeWorkspaceDep = Annotated[FakeWorkspace, Depends(lambda: FakeWorkspace())]


# ---------------------------------------------------------------------------
# Tool fixtures
# ---------------------------------------------------------------------------


def get_weather(city: str, country_code: str = "US") -> str:
    """Get current weather for a city."""
    return f"72°F in {city}, {country_code}"


def query_genie(question: str, space_id: str, ws: FakeWorkspaceDep) -> str:  # type: ignore[valid-type]
    """Answer a question using a Genie Space."""
    return "some answer"


def no_args(ws: FakeWorkspaceDep) -> list[str]:  # type: ignore[valid-type]
    """List things."""
    return []


class StructuredOutput(BaseModel):
    answer: str
    confidence: float = 1.0


def structured_tool(x: int) -> StructuredOutput:
    """Returns structured output."""
    return StructuredOutput(answer=str(x))


async def async_tool(query: str) -> str:
    """An async tool function."""
    return f"result for {query}"


@pytest.fixture
def sample_tools():
    """Return a list of sample tool functions."""
    return [get_weather, query_genie, no_args, structured_tool, async_tool]


@pytest.fixture
def basic_agent():
    """Return an Agent with get_weather and query_genie tools."""
    return Agent(tools=[get_weather, query_genie])


@pytest.fixture
def agent_config():
    """Return a basic AgentConfig."""
    return AgentConfig(
        name="test-agent",
        description="A test agent",
        model="databricks-meta-llama-3-3-70b-instruct",
        instructions="You are a helpful test agent.",
        max_iterations=5,
    )


@pytest.fixture
def agent_context(basic_agent, agent_config):
    """Return an AgentContext wired up with basic_agent and config."""
    from apx_agent._models import A2ASkill, AgentCard

    tools = basic_agent.collect_tools()
    card = AgentCard(
        name=agent_config.name,
        description=agent_config.description,
        skills=[
            A2ASkill(
                id=t.name, name=t.name, description=t.description,
                inputSchema=t.input_schema, outputSchema=t.output_schema,
            )
            for t in tools
        ],
    )
    return AgentContext(config=agent_config, tools=tools, card=card, agent=basic_agent)


@pytest.fixture
def mock_workspace_client():
    """Return a mocked WorkspaceClient."""
    ws = MagicMock()
    ws.config.host = "https://test-workspace.databricks.com"
    ws.config.authenticate.return_value = {"Authorization": "Bearer fake-token"}
    return ws


def make_llm_response(content: str = "Hello!", tool_calls: list | None = None) -> dict:
    """Build a fake LLM response payload."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "choices": [
            {
                "message": message,
                "finish_reason": finish_reason,
            }
        ]
    }


def make_tool_call(name: str, arguments: dict | None = None, call_id: str = "call_1") -> dict:
    """Build a fake tool_call entry."""
    import json
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
    }

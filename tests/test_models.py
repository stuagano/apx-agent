"""Tests for _models.py — protocol models, agent context, type aliases."""

from __future__ import annotations

import json

import pytest

from apx_agent._models import (
    A2ASkill,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    InvocationRequest,
    InvocationResponse,
    Message,
    OutputItem,
    OutputTextContent,
    set_custom_output,
)


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig(name="test")
        assert cfg.model == "databricks-meta-llama-3-3-70b-instruct"
        assert cfg.max_iterations == 10
        assert cfg.api_prefix == "/api"
        assert cfg.temperature is None
        assert cfg.max_tokens is None
        assert cfg.sub_agents == []

    def test_full_config(self):
        cfg = AgentConfig(
            name="my-agent",
            description="A test agent",
            model="gpt-4",
            instructions="Be helpful",
            temperature=0.7,
            max_tokens=1024,
            max_iterations=5,
            sub_agents=["http://agent1.com", "$AGENT2_URL"],
            api_prefix="/v1",
        )
        assert cfg.name == "my-agent"
        assert cfg.temperature == 0.7
        assert len(cfg.sub_agents) == 2


# ---------------------------------------------------------------------------
# Message + InvocationRequest
# ---------------------------------------------------------------------------


class TestMessage:
    def test_basic_message(self):
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.name is None
        assert msg.tool_call_id is None

    def test_tool_message(self):
        msg = Message(role="tool", content="result", tool_call_id="call_1")
        assert msg.tool_call_id == "call_1"


class TestInvocationRequest:
    def test_string_input(self):
        req = InvocationRequest(input="Hello")
        messages = req.messages()
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == "Hello"

    def test_list_input(self):
        req = InvocationRequest(input=[
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello!"),
        ])
        assert len(req.messages()) == 2

    def test_defaults(self):
        req = InvocationRequest(input="test")
        assert req.stream is False
        assert req.custom_inputs == {}

    def test_instructions_override(self):
        req = InvocationRequest(
            input="test",
            custom_inputs={"instructions": "Be concise"},
        )
        assert req.instructions_override() == "Be concise"

    def test_instructions_override_empty(self):
        req = InvocationRequest(input="test")
        assert req.instructions_override() == ""


# ---------------------------------------------------------------------------
# InvocationResponse
# ---------------------------------------------------------------------------


class TestInvocationResponse:
    def test_basic_response(self):
        resp = InvocationResponse(
            output=[OutputItem(content=[OutputTextContent(text="Hello!")])],
        )
        assert resp.output[0].content[0].text == "Hello!"
        assert resp.output[0].role == "assistant"
        assert resp.output[0].status == "completed"
        assert resp.custom_outputs == {}


# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_defaults(self):
        card = AgentCard(name="test", description="desc")
        assert card.schemaVersion == "1.0"
        assert card.protocolVersion == "0.3.0"
        assert card.capabilities.streaming is True
        assert card.capabilities.multiTurn is True
        assert len(card.authSchemes) == 1
        assert card.authSchemes[0].type == "bearer"
        assert card.mcpEndpoint is None

    def test_with_skills(self):
        card = AgentCard(
            name="test",
            description="desc",
            skills=[
                A2ASkill(id="tool1", name="tool1", description="A tool"),
            ],
        )
        assert len(card.skills) == 1

    def test_json_serialization(self):
        card = AgentCard(name="test", description="desc")
        data = json.loads(card.model_dump_json())
        assert "schemaVersion" in data
        assert "capabilities" in data
        assert "provider" in data

    def test_model_copy_update(self):
        card = AgentCard(name="test", description="desc")
        updated = card.model_copy(update={"url": "https://example.com", "mcpEndpoint": "/mcp"})
        assert updated.url == "https://example.com"
        assert updated.mcpEndpoint == "/mcp"
        assert card.url == ""  # original unchanged


# ---------------------------------------------------------------------------
# AgentContext
# ---------------------------------------------------------------------------


class TestAgentContext:
    def test_get_tool(self, agent_context):
        tool = agent_context.get_tool("get_weather")
        assert tool is not None
        assert tool.name == "get_weather"

    def test_get_tool_missing(self, agent_context):
        assert agent_context.get_tool("nonexistent") is None

    def test_tool_map(self, agent_context):
        assert len(agent_context._tool_map) == 2


# ---------------------------------------------------------------------------
# set_custom_output
# ---------------------------------------------------------------------------


class TestSetCustomOutput:
    def test_sets_value_on_request_state(self):
        from unittest.mock import MagicMock
        request = MagicMock(spec=["state"])
        request.state = MagicMock()
        request.state.custom_outputs = {}
        set_custom_output(request, "key", "value")
        assert request.state.custom_outputs["key"] == "value"

    def test_creates_custom_outputs_if_missing(self):
        from unittest.mock import MagicMock
        request = MagicMock(spec=["state"])
        request.state = MagicMock(spec=[])
        set_custom_output(request, "key", "value")
        assert request.state.custom_outputs["key"] == "value"

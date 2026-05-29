"""Regression tests for audit findings H2 and L3 in the agent-runtime files.

Scope-limited to pure message-conversion helpers so the suite runs without a
live LangGraph / Databricks endpoint.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402


def test_compile_run_tool_message_uses_tool_call_id() -> None:
    """L3: _to_langchain must carry the Message.tool_call_id, not an empty id."""
    from apx_agent._compile_run import _to_langchain
    from apx_agent._models import Message

    msgs = [
        Message(role="user", content="hi"),
        Message(role="tool", content="result", tool_call_id="call-123"),
    ]
    out = _to_langchain(msgs)
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert tool_msgs and tool_msgs[0].tool_call_id == "call-123"


def test_history_to_langchain_reconstructs_tool_calls() -> None:
    """H2: assistant history with tool_calls must rebuild AIMessage.tool_calls."""
    from apx_agent._responses_agent import _history_to_langchain

    history = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-7",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                }
            ],
        },
        {"role": "tool", "content": "answer", "tool_call_id": "call-7"},
    ]
    out = _history_to_langchain(history)
    ai = [m for m in out if isinstance(m, AIMessage)]
    assert ai and ai[0].tool_calls
    assert ai[0].tool_calls[0]["name"] == "lookup"
    assert ai[0].tool_calls[0]["id"] == "call-7"
    # The following ToolMessage's id references a real AIMessage tool call.
    tool = [m for m in out if isinstance(m, ToolMessage)]
    assert tool and tool[0].tool_call_id == "call-7"


def test_responses_input_function_call_maps_to_tool_calls() -> None:
    """H2: function_call input items become an AIMessage with tool_calls."""
    from apx_agent._responses_agent import _responses_input_to_langchain

    items = [
        {"role": "user", "content": "go"},
        {
            "type": "function_call",
            "call_id": "call-42",
            "name": "fetch",
            "arguments": '{"id": 1}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-42",
            "output": "done",
        },
    ]
    out = _responses_input_to_langchain(items)
    ai = [m for m in out if isinstance(m, AIMessage)]
    assert ai and ai[0].tool_calls
    assert ai[0].tool_calls[0]["id"] == "call-42"
    assert ai[0].tool_calls[0]["name"] == "fetch"
    tool = [m for m in out if isinstance(m, ToolMessage)]
    assert tool and tool[0].tool_call_id == "call-42"
    assert tool[0].content == "done"
    # The first user item still maps to a HumanMessage.
    assert any(isinstance(m, HumanMessage) for m in out)


def test_chat_agent_converter_preserves_tool_calls_round_trip() -> None:
    """H2/H13: tool_calls must survive the ChatAgent history round-trip.

    A persisted assistant turn carries tool_calls as pydantic ToolCall objects
    with a JSON-string ``arguments``. The converter must reconstruct
    AIMessage.tool_calls (name/args/id), not silently empty them — otherwise
    the next turn orphans the following ToolMessage.
    """
    pytest.importorskip("mlflow")
    from apx_agent._chat_agent import _to_langchain_messages
    from mlflow.types.agent import ChatAgentMessage

    m = ChatAgentMessage(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": 1}'},
            }
        ],
    )
    out = _to_langchain_messages([m])
    ai = [x for x in out if isinstance(x, AIMessage)]
    assert ai and ai[0].tool_calls
    assert ai[0].tool_calls[0]["name"] == "lookup"
    assert ai[0].tool_calls[0]["id"] == "c1"
    assert ai[0].tool_calls[0]["args"] == {"q": 1}

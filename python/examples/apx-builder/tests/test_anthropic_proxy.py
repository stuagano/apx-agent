"""Tests for anthropic_proxy.py — translation functions."""
import json

from anthropic_proxy import _to_openai, _to_anthropic, _MAX_TOOL_RESULT_CHARS, _trim_tool_result


def test_to_openai_simple_user_message():
    body = {
        "model": "databricks-claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    result = _to_openai(body)

    assert result["model"] == "databricks-claude-sonnet-4-6"
    assert result["max_tokens"] == 1024
    assert result["messages"] == [{"role": "user", "content": "Hello"}]


def test_to_openai_system_string_prepended():
    body = {
        "model": "m",
        "max_tokens": 100,
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = _to_openai(body)

    assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert result["messages"][1] == {"role": "user", "content": "hi"}


def test_to_openai_system_block_list():
    body = {
        "model": "m",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "Block one."}, {"type": "text", "text": "Block two."}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = _to_openai(body)

    assert result["messages"][0]["role"] == "system"
    assert "Block one." in result["messages"][0]["content"]
    assert "Block two." in result["messages"][0]["content"]


def test_to_openai_tools_converted():
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "my_tool",
                "description": "Does something",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        ],
    }
    result = _to_openai(body)

    assert len(result["tools"]) == 1
    t = result["tools"][0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "my_tool"
    assert t["function"]["description"] == "Does something"
    assert t["function"]["parameters"]["properties"]["x"]["type"] == "string"


def test_to_openai_assistant_tool_use():
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "id": "tc_123", "name": "search", "input": {"q": "test"}},
                ],
            }
        ],
    }
    result = _to_openai(body)

    msg = result["messages"][0]
    assert msg["role"] == "assistant"
    assert "Let me check." in (msg["content"] or "")
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tc_123"
    assert tc["function"]["name"] == "search"
    assert json.loads(tc["function"]["arguments"]) == {"q": "test"}


def test_to_openai_user_tool_result():
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tc_123", "content": "result text"},
                ],
            }
        ],
    }
    result = _to_openai(body)

    msg = result["messages"][0]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "tc_123"
    assert msg["content"] == "result text"


def test_to_openai_user_tool_result_block_list():
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tc_456",
                        "content": [{"type": "text", "text": "block output"}],
                    }
                ],
            }
        ],
    }
    result = _to_openai(body)

    msg = result["messages"][0]
    assert msg["role"] == "tool"
    assert msg["content"] == "block output"


def test_to_anthropic_simple_text():
    original = {"model": "databricks-claude-sonnet-4-6"}
    oai = {
        "id": "chatcmpl-abc",
        "choices": [{"message": {"role": "assistant", "content": "Hello!", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = _to_anthropic(original, oai)

    assert result["type"] == "message"
    assert result["role"] == "assistant"
    assert result["stop_reason"] == "end_turn"
    assert result["content"][0] == {"type": "text", "text": "Hello!"}
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5


def test_to_anthropic_tool_use():
    original = {"model": "m"}
    oai = {
        "id": "chatcmpl-xyz",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q": "test"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    result = _to_anthropic(original, oai)

    assert result["stop_reason"] == "tool_use"
    tu = result["content"][0]
    assert tu["type"] == "tool_use"
    assert tu["id"] == "call_1"
    assert tu["name"] == "search"
    assert tu["input"] == {"q": "test"}


def test_trim_tool_result_short_passthrough():
    short = "File written: /tmp/agent/prompt.py"
    assert _trim_tool_result(short) == short


def test_trim_tool_result_long_truncated():
    long_text = "x" * (_MAX_TOOL_RESULT_CHARS + 500)
    result = _trim_tool_result(long_text)
    assert len(result) < len(long_text)
    assert result.startswith("x" * _MAX_TOOL_RESULT_CHARS)
    assert "[trimmed" in result


def test_to_openai_tool_result_trimmed_when_long():
    long_content = "a" * (_MAX_TOOL_RESULT_CHARS + 1000)
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tc_789", "content": long_content},
                ],
            }
        ],
    }
    result = _to_openai(body)
    msg = result["messages"][0]
    assert len(msg["content"]) < len(long_content)
    assert "[trimmed" in msg["content"]


def test_to_anthropic_length_finish_reason():
    original = {"model": "m"}
    oai = {
        "id": "id",
        "choices": [{"message": {"content": "truncated", "tool_calls": []}, "finish_reason": "length"}],
        "usage": {},
    }
    result = _to_anthropic(original, oai)
    assert result["stop_reason"] == "max_tokens"

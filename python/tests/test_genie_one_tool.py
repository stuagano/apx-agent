"""Tests for ``genie_one_tool``: one tool call that asks Genie One and polls to the answer."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from apx_agent import genie_one_tool
from apx_agent._errors import ToolError
from apx_agent._inspection import _inspect_tool_fn
from apx_agent._resources import get_user_api_scopes

IDS = {"conversation_id": "c1", "response_id": "r1"}
WS = SimpleNamespace(config=SimpleNamespace(host="https://w.example/", authenticate=lambda: {"Authorization": "x"}))


def _fake_session(monkeypatch, replies, *, error=None):
    """Patch the MCP session so each call_tool returns the next structured state."""
    calls = []
    states = iter(replies)

    class Session:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if error is not None:
                raise error
            return SimpleNamespace(structuredContent=next(states))

    @asynccontextmanager
    async def open_session(url, headers, transport):
        calls.append(("open", url, headers, transport))
        yield Session()

    monkeypatch.setattr("apx_agent.mcp_consume._open_session", open_session)
    return calls


def test_llm_sees_only_the_question():
    plain, deps = _inspect_tool_fn(genie_one_tool())
    assert list(plain) == ["question"] and deps == ["ws"]


def test_declares_the_genie_user_scope_and_default_name():
    tool = genie_one_tool()
    assert tool.__name__ == "ask_genie_one"
    assert get_user_api_scopes(tool) == ["genie"]


@pytest.mark.parametrize("kwargs", [{"wait_s": 0}, {"poll_s": -1}])
def test_rejects_non_positive_timing(kwargs):
    with pytest.raises(ValueError, match="positive"):
        genie_one_tool(**kwargs)


async def test_polls_until_completed_and_returns_answer_with_link(monkeypatch):
    calls = _fake_session(monkeypatch, [
        {"status": "in_progress", **IDS},
        {"status": "in_progress", **IDS},
        {"status": "completed", "final_answer": " Revenue was **$1M**. ", "deep_link": "https://w.example/one/x"},
    ])
    result = await genie_one_tool(poll_s=0.001)(question="What was revenue?", ws=WS)
    assert result == "Revenue was **$1M**.\n\n[Explore in Databricks](https://w.example/one/x)"
    assert calls == [
        ("open", "https://w.example/api/2.0/mcp/genie", {"Authorization": "x"}, "http"),
        ("genie_ask", {"question": "What was revenue?"}),
        ("genie_poll_response", IDS),
        ("genie_poll_response", IDS),
    ]


async def test_answer_without_a_link_is_returned_bare(monkeypatch):
    _fake_session(monkeypatch, [{"status": "completed", "final_answer": "42"}])
    assert await genie_one_tool()(question="q", ws=WS) == "42"


async def test_stops_at_wait_s_and_returns_the_follow_link(monkeypatch):
    calls = _fake_session(monkeypatch, [{"status": "in_progress", "deep_link": "https://w.example/one/x", **IDS}] * 50)
    result = await genie_one_tool(wait_s=0.05, poll_s=0.02)(question="q", ws=WS)
    assert result.startswith("Genie One did not finish within 0s (status: in_progress).")
    assert "[Explore in Databricks](https://w.example/one/x)" in result
    assert 1 <= len([c for c in calls if c[0] == "genie_poll_response"]) < 50


async def test_mcp_failure_is_a_tool_error(monkeypatch):
    _fake_session(monkeypatch, [], error=RuntimeError("connection reset"))
    with pytest.raises(ToolError, match="Genie One question failed: connection reset"):
        await genie_one_tool()(question="q", ws=WS)

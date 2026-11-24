"""Genie follow-ups (#1254) and conversation cleanup (#1255).

genie_tool / genie_query_tool continue one Genie conversation per apx session per
space, recover when it disappears, delete what they started when the session
ends or is evicted, and ``sweep_genie_conversations`` clears leftovers.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from databricks.sdk.errors import NotFound
from databricks.sdk.service.dashboards import MessageStatus

import apx_agent.genie as g
from apx_agent import end_genie_session, genie_query_tool, genie_tool, sweep_genie_conversations
from apx_agent._mlflow_tracing import _ACTIVE_SESSION, current_session_id, safe_span, set_trace_session


def _msg(conversation_id: str, text: str = "ok") -> MagicMock:
    msg = MagicMock()
    msg.status = MessageStatus.COMPLETED
    msg.conversation_id = conversation_id
    msg.message_id = "m"
    att = MagicMock()
    att.text = MagicMock(content=text)
    att.query = None
    att.attachment_id = None
    msg.attachments = [att]
    return msg


def _ws(*conversation_ids: str) -> MagicMock:
    """A client whose start_conversation hands out ``conversation_ids`` in order."""
    ws = MagicMock()
    ws.genie.start_conversation_and_wait.side_effect = [_msg(c, f"start {c}") for c in conversation_ids]
    ws.genie.create_message_and_wait.side_effect = lambda space_id, conversation_id, content: _msg(
        conversation_id, f"follow-up in {conversation_id}"
    )
    return ws


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    registry = g.GenieConversations()
    monkeypatch.setattr(g, "_CONVERSATIONS", registry)
    token = _ACTIVE_SESSION.set("")
    yield registry
    _ACTIVE_SESSION.reset(token)


async def _ask(tool: Any, session: str, question: str, ws: Any) -> Any:
    _ACTIVE_SESSION.set(session)
    return await tool(question=question, ws=ws)


@pytest.mark.asyncio
async def test_follow_ups_reuse_one_conversation_within_a_session():
    ws = _ws("c1")
    tool = genie_tool("space-1")

    first = await _ask(tool, "s1", "top region?", ws)
    second = await _ask(tool, "s1", "and last year?", ws)

    assert first == "start c1"
    assert second == "follow-up in c1"
    ws.genie.start_conversation_and_wait.assert_called_once_with(space_id="space-1", content="top region?")
    ws.genie.create_message_and_wait.assert_called_once_with(
        space_id="space-1", conversation_id="c1", content="and last year?"
    )


@pytest.mark.asyncio
async def test_query_tool_follows_up_too():
    ws = _ws("c1")
    tool = genie_query_tool("space-1")
    await _ask(tool, "s1", "q1", ws)
    result = await _ask(tool, "s1", "q2", ws)
    assert result["genie_response"] == "follow-up in c1"
    ws.genie.create_message_and_wait.assert_called_once()


@pytest.mark.asyncio
async def test_sessions_never_share_a_conversation():
    ws = _ws("c1", "c2")
    tool = genie_tool("space-1")

    await _ask(tool, "s1", "q", ws)
    await _ask(tool, "s2", "q", ws)
    s1_again = await _ask(tool, "s1", "q", ws)
    s2_again = await _ask(tool, "s2", "q", ws)

    assert ws.genie.start_conversation_and_wait.call_count == 2
    assert s1_again == "follow-up in c1"
    assert s2_again == "follow-up in c2"


@pytest.mark.asyncio
async def test_spaces_in_one_session_get_their_own_conversation():
    ws = _ws("c1", "c2")
    await _ask(genie_tool("space-1"), "s1", "q", ws)
    await _ask(genie_tool("space-2"), "s1", "q", ws)
    assert ws.genie.start_conversation_and_wait.call_count == 2
    ws.genie.create_message_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_no_session_starts_a_new_conversation_each_time():
    ws = _ws("c1", "c2")
    tool = genie_tool("space-1")
    assert current_session_id() is None
    await tool(question="q", ws=ws)
    await tool(question="q", ws=ws)
    assert ws.genie.start_conversation_and_wait.call_count == 2
    ws.genie.create_message_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_follow_ups_off_keeps_one_conversation_per_question(_fresh_registry):
    ws = _ws("c1", "c2")
    tool = genie_tool("space-1", follow_ups=False)
    await _ask(tool, "s1", "q", ws)
    await _ask(tool, "s1", "q", ws)
    assert ws.genie.start_conversation_and_wait.call_count == 2
    ws.genie.create_message_and_wait.assert_not_called()
    assert end_genie_session("s1") == 0


@pytest.mark.asyncio
async def test_missing_conversation_is_replaced_transparently():
    ws = _ws("c1", "c2")
    ws.genie.create_message_and_wait.side_effect = NotFound("conversation c1 does not exist")
    tool = genie_tool("space-1")

    await _ask(tool, "s1", "q1", ws)
    answer = await _ask(tool, "s1", "q2", ws)

    assert answer == "start c2"
    assert ws.genie.start_conversation_and_wait.call_count == 2
    ws.genie.create_message_and_wait.side_effect = lambda space_id, conversation_id, content: _msg(
        conversation_id, f"follow-up in {conversation_id}"
    )
    assert await _ask(tool, "s1", "q3", ws) == "follow-up in c2"


@pytest.mark.asyncio
async def test_other_follow_up_errors_still_surface():
    from apx_agent._errors import ToolError
    from databricks.sdk.errors.base import DatabricksError

    ws = _ws("c1")
    ws.genie.create_message_and_wait.side_effect = DatabricksError("rate limited")
    tool = genie_tool("space-1")
    await _ask(tool, "s1", "q1", ws)
    with pytest.raises(ToolError, match="rate limited"):
        await _ask(tool, "s1", "q2", ws)


@pytest.mark.asyncio
async def test_ending_a_session_deletes_its_conversations_only():
    ws = _ws("c1", "c2", "c3")
    await _ask(genie_tool("space-1"), "s1", "q", ws)
    await _ask(genie_tool("space-2"), "s1", "q", ws)
    await _ask(genie_tool("space-1"), "s2", "q", ws)

    assert end_genie_session("s1") == 2

    deleted = {c.kwargs["conversation_id"] for c in ws.genie.delete_conversation.call_args_list}
    assert deleted == {"c1", "c2"}
    assert end_genie_session("s1") == 0
    assert await _ask(genie_tool("space-1"), "s2", "q", ws) == "follow-up in c3"


@pytest.mark.asyncio
async def test_delete_failure_never_fails_the_caller():
    ws = _ws("c1")
    ws.genie.delete_conversation.side_effect = RuntimeError("token expired")
    await _ask(genie_tool("space-1"), "s1", "q", ws)
    assert end_genie_session("s1") == 1


@pytest.mark.asyncio
async def test_lru_eviction_deletes_the_oldest(monkeypatch):
    monkeypatch.setattr(g, "_CONVERSATIONS", g.GenieConversations(max_conversations=2))
    ws = _ws("c1", "c2", "c3")
    tool = genie_tool("space-1")
    for session in ("s1", "s2", "s3"):
        await _ask(tool, session, "q", ws)
    ws.genie.delete_conversation.assert_called_once_with(space_id="space-1", conversation_id="c1")


@pytest.mark.asyncio
async def test_idle_conversations_expire_and_are_deleted(monkeypatch):
    registry = g.GenieConversations(idle_ttl_s=60)
    monkeypatch.setattr(g, "_CONVERSATIONS", registry)
    ws = _ws("c1", "c2")
    tool = genie_tool("space-1")
    await _ask(tool, "s1", "q", ws)

    now = time.monotonic()
    monkeypatch.setattr(g.time, "monotonic", lambda: now + 61)
    answer = await _ask(tool, "s1", "q", ws)

    ws.genie.delete_conversation.assert_called_once_with(space_id="space-1", conversation_id="c1")
    assert answer == "start c2"


# ---------------------------------------------------------------------------
# Session binding: where the tool learns its session
# ---------------------------------------------------------------------------

def test_agent_root_span_binds_and_rebinds_the_session():
    with safe_span("turn", span_type="AGENT", attributes={"apx.session.id": "s1"}):
        assert current_session_id() == "s1"
    with safe_span("turn", span_type="AGENT", attributes={"apx.session.id": None}):
        assert current_session_id() is None
    set_trace_session(None, "thread-9")
    assert current_session_id() == "thread-9"
    with safe_span("inner", span_type="TOOL"):
        assert current_session_id() == "thread-9"


def test_session_reaches_the_tool_through_a_compiled_graph(monkeypatch):
    """Two turns of a compiled agent in one session: one start, one follow-up."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    from apx_agent import Agent, _compile
    from apx_agent._compile import compile_to_langgraph

    class _ToolFake(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    def _turn(n: int) -> list[AIMessage]:
        call = {"name": "ask_genie", "args": {"question": f"q{n}"}, "id": f"t{n}"}
        return [AIMessage(content="", tool_calls=[call]), AIMessage(content=f"done {n}")]

    model = _ToolFake(messages=iter(_turn(1) + _turn(2)))
    monkeypatch.setattr(
        _compile, "_build_chat_databricks", lambda endpoint, *, temperature=None, max_tokens=None: model,
    )
    ws = _ws("c1")
    ws.config.host = "https://fake.cloud.databricks.com"
    graph = compile_to_langgraph(Agent(tools=[genie_tool("space-1")]), ws=ws, model="m")

    for n in (1, 2):
        with safe_span("turn", span_type="AGENT", attributes={"apx.session.id": "s1"}):
            graph.invoke({"messages": [HumanMessage(content=f"q{n}")]})

    ws.genie.start_conversation_and_wait.assert_called_once_with(space_id="space-1", content="q1")
    ws.genie.create_message_and_wait.assert_called_once_with(
        space_id="space-1", conversation_id="c1", content="q2"
    )


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def _sweep_ws() -> MagicMock:
    now_ms = time.time() * 1000
    old, fresh = now_ms - 48 * 3600 * 1000, now_ms - 60 * 1000

    def _conv(cid: str, ts: float) -> MagicMock:
        return MagicMock(conversation_id=cid, created_timestamp=int(ts))

    pages = {
        None: MagicMock(conversations=[_conv("old-1", old), _conv("new-1", fresh)], next_page_token="p2"),
        "p2": MagicMock(conversations=[_conv("old-2", old)], next_page_token=None),
    }
    ws = MagicMock()
    ws.genie.list_conversations.side_effect = lambda space_id, include_all, page_token: pages[page_token]
    return ws


def test_sweep_dry_run_lists_without_deleting():
    ws = _sweep_ws()
    stale = sweep_genie_conversations(ws, "space-1", older_than_s=24 * 3600)
    assert stale == ["old-1", "old-2"]
    ws.genie.delete_conversation.assert_not_called()
    assert ws.genie.list_conversations.call_args.kwargs["include_all"] is False


def test_sweep_deletes_stale_and_skips_failures():
    ws = _sweep_ws()

    def _delete(space_id: str, conversation_id: str) -> None:
        if conversation_id == "old-2":
            raise RuntimeError("forbidden")

    ws.genie.delete_conversation.side_effect = _delete
    deleted = sweep_genie_conversations(
        ws, "space-1", older_than_s=24 * 3600, include_all=True, dry_run=False
    )
    assert deleted == ["old-1"]
    assert {c.kwargs["conversation_id"] for c in ws.genie.delete_conversation.call_args_list} == {
        "old-1", "old-2",
    }
    assert ws.genie.list_conversations.call_args.kwargs["include_all"] is True


def test_cli_genie_sweep_dry_run_and_delete(monkeypatch):
    from apx_agent import cli

    ws = _sweep_ws()
    monkeypatch.setattr(cli, "_require_sdk", lambda profile=None: ws)
    runner = CliRunner()

    dry = runner.invoke(cli.main, ["resources", "genie-sweep", "space-1", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would delete 2 conversation(s)" in dry.output and "old-1" in dry.output
    ws.genie.delete_conversation.assert_not_called()

    real = runner.invoke(cli.main, ["resources", "genie-sweep", "space-1", "--older-than-hours", "1"])
    assert real.exit_code == 0, real.output
    assert "deleted 2 conversation(s) older than 1h" in real.output
    assert ws.genie.delete_conversation.call_count == 2

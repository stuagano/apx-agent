"""Reality check (ctk): mid-turn interrupt for human approval (Slice A).

Proves that an ASK policy on a checkpointed run SUSPENDS the graph exactly at the
gated tool (via LangGraph ``interrupt()``), surfaces the approval payload, and
RESUMES on the human decision — approve → the tool runs, deny → the tool is
blocked. Without a checkpointer it falls back to the turn-boundary flow
(``ApprovalRequired``). Uses the existing InMemory checkpointer; durability is a
later slice.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._policy import (  # noqa: E402
    ApprovalRequired,
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
)


class _ToolFake(GenericFakeChatModel):
    """Canned chat model that also accepts bind_tools (create_agent needs it)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def send_email(to: str) -> str:
    """Send an email to the given address."""
    return f"sent to {to}"


def _ask_gate() -> PolicyGate:
    ask = FunctionPolicy(
        lambda ev: PolicyResult(action=PolicyAction.ASK, reason="needs human ok"),
        name="always-ask",
    )
    return PolicyGate([ask])


def _agent() -> LlmAgent:
    return LlmAgent(name="a", tools=[send_email], before_tool=_ask_gate())


def _ws() -> Any:
    from unittest.mock import MagicMock

    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _install_model(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    model = _ToolFake(messages=iter(messages))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )


def _tool_call(to: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "send_email", "args": {"to": to}, "id": "t1"}],
    )


def _tool_msgs(result: dict[str, Any]) -> list[str]:
    return [str(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]


def test_ask_interrupts_then_resume_approve_runs_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="all done")])
    graph = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "T"}}

    r1 = graph.invoke({"messages": [HumanMessage(content="email x@y.com")]}, config=cfg)
    # Suspended at the gated tool, with a structured approval payload.
    assert "__interrupt__" in r1
    payload = r1["__interrupt__"][0].value
    assert payload["type"] == "approval_required"
    assert payload["tool_name"] == "send_email"
    assert payload["arguments"] == {"to": "x@y.com"}
    # The tool has NOT run yet.
    assert not any("sent to" in t for t in _tool_msgs(r1))

    # Human approves → resume → the tool actually runs.
    r2 = graph.invoke(Command(resume="approve"), config=cfg)
    assert any("sent to x@y.com" in t for t in _tool_msgs(r2))


def test_resume_deny_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="ok, cancelled")])
    graph = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "T"}}

    graph.invoke({"messages": [HumanMessage(content="email x@y.com")]}, config=cfg)
    r2 = graph.invoke(Command(resume="deny"), config=cfg)
    # The tool never ran; the gated call was refused.
    assert not any("sent to" in t for t in _tool_msgs(r2))


def test_ask_without_checkpointer_falls_back_to_turn_boundary() -> None:
    """Outside a checkpointed run (no thread_id / no graph context), ASK raises
    ApprovalRequired — the existing turn-boundary flow, unchanged."""
    with pytest.raises(ApprovalRequired):
        _ask_gate()("send_email", {"to": "x@y.com"})

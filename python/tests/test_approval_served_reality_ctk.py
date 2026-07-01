"""Reality check (ctk): mid-turn approval reachable over the served ChatAgent
path (Slice B).

A gated tool suspends the run; ``predict`` returns an approval-required response
(payload under ``custom_outputs["approval_required"]``, tool NOT run); the client
resumes by resending with ``custom_inputs={"session_id": …, "resume": …}`` —
approve runs the tool, deny blocks it. In-process checkpointer; durability later.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402

from apx_agent import LlmAgent, chat_agent_for  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._policy import (  # noqa: E402
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
)


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def send_email(to: str) -> str:
    """Send an email to the given address."""
    return f"sent to {to}"


def _ask_gate() -> PolicyGate:
    return PolicyGate(
        [
            FunctionPolicy(
                lambda ev: PolicyResult(action=PolicyAction.ASK, reason="needs human ok"),
                name="ask",
            )
        ]
    )


def _agent() -> LlmAgent:
    return LlmAgent(name="a", tools=[send_email], before_tool=_ask_gate())


def _ws() -> Any:
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
        content="", tool_calls=[{"name": "send_email", "args": {"to": to}, "id": "t1"}]
    )


def _contents(resp: Any) -> str:
    return " ".join(m.content for m in resp.messages if m.content)


def test_predict_surfaces_approval_then_resume_approve_runs_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        r1 = chat.predict(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        )
        # Approval required, tool NOT run.
        assert r1.custom_outputs and "approval_required" in r1.custom_outputs
        assert r1.custom_outputs["approval_required"]["tool_name"] == "send_email"
        assert "sent to" not in _contents(r1)

        # Client approves by resending with the resume decision.
        r2 = chat.predict([], custom_inputs={"session_id": "T", "resume": "approve"})

    assert not (r2.custom_outputs or {}).get("approval_required")
    # The gated tool actually ran on resume.
    assert "sent to x@y.com" in _contents(r2)


def test_predict_resume_deny_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="cancelled")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        )
        r2 = chat.predict([], custom_inputs={"session_id": "T", "resume": "deny"})
    # The tool never ran.
    assert "sent to" not in _contents(r2)

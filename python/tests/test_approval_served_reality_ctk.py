"""Reality check (ctk): mid-turn approval reachable over ALL served paths (#329).

A gated tool suspends the run; the handler surfaces an approval-required result
(payload under ``custom_outputs["approval_required"]``, tool NOT run); the client
resumes by resending with ``{"resume": …}`` on the same thread — approve runs the
tool, deny blocks it. In-process checkpointer; durability later.

Covered here: ChatAgent ``predict`` + ``predict_stream`` (key ``session_id``) and
ResponsesAgent invoke + stream (key ``thread_id``). Every assertion is against the
returned response / streamed output, never ``get_state`` alone (cf. #338).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

import json  # noqa: E402

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402
from mlflow.types.responses import ResponsesAgentRequest  # noqa: E402

from apx_agent import LlmAgent, chat_agent_for  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._responses_agent import compile_to_responses_agent  # noqa: E402
from apx_agent._policy import (  # noqa: E402
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
)


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):  # type: ignore[override]
        # GenericFakeChatModel drops tool_calls when streamed, so it can't route a
        # gated tool under stream_mode="messages". Emit the next scripted message as
        # one chunk that preserves the tool call (via tool_call_chunks).
        msg = next(self.messages)  # type: ignore[arg-type]
        tcc = [
            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=msg.content, tool_call_chunks=tcc)
        )


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


# ── predict_stream (ChatAgent streaming) ──────────────────────────────────────


def _stream_text(chunks: list[Any]) -> str:
    return " ".join(c.delta.content for c in chunks if c.delta and c.delta.content)


def _stream_approval(chunks: list[Any]) -> Any:
    for c in chunks:
        if c.custom_outputs and "approval_required" in c.custom_outputs:
            return c.custom_outputs["approval_required"]
    return None


def test_predict_stream_surfaces_approval_then_resume_approve_runs_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        c1 = list(chat.predict_stream(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        ))
        appr = _stream_approval(c1)
        assert appr and appr["tool_name"] == "send_email"
        assert "sent to" not in _stream_text(c1)  # tool NOT run

        c2 = list(chat.predict_stream(
            [], custom_inputs={"session_id": "T", "resume": "approve"}
        ))
    assert _stream_approval(c2) is None
    assert "sent to x@y.com" in _stream_text(c2)  # gated tool ran on resume


def test_predict_stream_resume_deny_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="cancelled")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(chat.predict_stream(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        ))
        c2 = list(chat.predict_stream(
            [], custom_inputs={"session_id": "T", "resume": "deny"}
        ))
    assert "sent to" not in _stream_text(c2)


# ── ResponsesAgent (invoke + stream) — key is ``thread_id`` ───────────────────


def _req(text: str, **ci: Any) -> Any:
    inp = [{"role": "user", "content": text}] if text else []
    return ResponsesAgentRequest(input=inp, custom_inputs=ci or None)


def _out_blob(items: list[Any]) -> str:
    return " ".join(str(i.model_dump()) for i in items)


def _events_blob(events: list[Any]) -> str:
    return " ".join(str(e.model_dump()) for e in events)


def _events_approval(events: list[Any]) -> Any:
    for e in events:
        co = e.custom_outputs
        if co and "approval_required" in co:
            return co["approval_required"]
    return None


def test_responses_invoke_surfaces_approval_then_resume_approve_runs_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    non_stream, _ = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        r1 = non_stream(_req("email x@y.com", thread_id="T"))
        assert r1.custom_outputs
        assert r1.custom_outputs["approval_required"]["tool_name"] == "send_email"
        assert "sent to" not in _out_blob(r1.output)  # tool NOT run

        r2 = non_stream(_req("", thread_id="T", resume="approve"))
    assert not (r2.custom_outputs or {}).get("approval_required")
    assert "sent to x@y.com" in _out_blob(r2.output)  # gated tool ran on resume


def test_responses_invoke_resume_approve_with_resent_input_keeps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prose says "resume by resending" — a client that resends the ORIGINAL
    # input alongside resume must still get the tool result + answer, not a slice
    # that drops them (Command(resume) is fed, so the resent input never enters
    # graph state; input_count must be ignored on resume).
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    non_stream, _ = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("email x@y.com", thread_id="T"))
        r2 = non_stream(_req("email x@y.com", thread_id="T", resume="approve"))
    assert "sent to x@y.com" in _out_blob(r2.output)  # tool result NOT sliced away


def test_responses_invoke_resume_deny_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="cancelled")])
    non_stream, _ = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("email x@y.com", thread_id="T"))
        r2 = non_stream(_req("", thread_id="T", resume="deny"))
    assert "sent to" not in _out_blob(r2.output)


def test_responses_stream_surfaces_approval_then_resume_approve_runs_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    _, streaming = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        e1 = list(streaming(_req("email x@y.com", thread_id="S")))
        appr = _events_approval(e1)
        assert appr and appr["tool_name"] == "send_email"
        assert "sent to" not in _events_blob(e1)  # tool NOT run

        e2 = list(streaming(_req("", thread_id="S", resume="approve")))
    assert _events_approval(e2) is None
    assert "sent to x@y.com" in _events_blob(e2)  # gated tool ran on resume


def test_responses_stream_resume_deny_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="cancelled")])
    _, streaming = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(streaming(_req("email x@y.com", thread_id="S")))
        e2 = list(streaming(_req("", thread_id="S", resume="deny")))
    assert "sent to" not in _events_blob(e2)


# ── durable checkpointer: approval survives a restart (Slice C) ───────────────


def _restart(saver: InMemorySaver) -> InMemorySaver:
    """A FRESH saver instance backed by the SAME state — simulates a process
    restart onto durable storage (what the Lakebase PostgresSaver provides). If a
    pending approval resumes through this, the round-trip survived losing the
    original saver instance, which is the whole point of Slice C.
    """
    fresh = InMemorySaver()
    fresh.storage = saver.storage
    fresh.writes = saver.writes
    fresh.blobs = saver.blobs
    return fresh


def test_predict_approval_survives_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    saver = InMemorySaver()
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat1 = chat_agent_for(
            _agent(), model="m", conversation_store=None, checkpointer=saver
        )
        r1 = chat1.predict(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        )
        assert r1.custom_outputs["approval_required"]["tool_name"] == "send_email"

        # Restart: original agent + saver instance are gone; a fresh checkpointer
        # on the same durable backing resumes the pending approval.
        chat2 = chat_agent_for(
            _agent(), model="m", conversation_store=None, checkpointer=_restart(saver)
        )
        r2 = chat2.predict([], custom_inputs={"session_id": "T", "resume": "approve"})
    assert "sent to x@y.com" in _contents(r2)  # gated tool ran after the restart

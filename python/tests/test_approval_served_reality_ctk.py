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


def test_predict_stream_resume_with_resent_input_does_not_double_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #484 (#375 regression on the chat streaming path): the pause turn persists
    # the user's input; a resume that RESENDS it must NOT persist it again.
    from apx_agent import InMemoryConversationStore

    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])

    store = InMemoryConversationStore()
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=store, checkpointer=InMemorySaver()
    )

    persisted_inputs: list[list[Any]] = []
    orig = chat._persist_conv_turn

    def _spy(conv_id: Any, *, input_messages: Any, **kw: Any) -> Any:
        persisted_inputs.append(list(input_messages))
        return orig(conv_id, input_messages=input_messages, **kw)

    monkeypatch.setattr(chat, "_persist_conv_turn", _spy)

    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(chat.predict_stream(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        ))  # pause → persists input
        list(chat.predict_stream(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u2")],
            custom_inputs={"session_id": "T", "resume": "approve"},
        ))  # resume RESENDS input

    assert len(persisted_inputs) == 2, f"expected pause + resume persist: {persisted_inputs}"
    assert persisted_inputs[0], "pause turn must persist the user input"
    assert persisted_inputs[1] == [], (
        f"resume turn re-persisted the input (double-write): {persisted_inputs[1]}"
    )


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


def test_responses_resume_with_resent_input_does_not_double_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #375: the pause turn persists the user's input; a resume that RESENDS the
    # original input (as the approval prose instructs) must NOT persist it again.
    import apx_agent._responses_agent as _ra
    from apx_agent import InMemoryConversationStore

    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])

    persisted_inputs: list[list[Any]] = []
    orig = _ra._persist_conv_turn

    def _spy(store: Any, conv_id: Any, *, input_items: Any, output_items: Any, **kw: Any) -> Any:
        persisted_inputs.append(list(input_items))
        return orig(store, conv_id, input_items=input_items, output_items=output_items, **kw)

    monkeypatch.setattr(_ra, "_persist_conv_turn", _spy)

    store = InMemoryConversationStore()
    non_stream, _ = compile_to_responses_agent(
        _agent(), model="m", conversation_store=store, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("email x@y.com", thread_id="T"))  # pause → persists input
        non_stream(_req("email x@y.com", thread_id="T", resume="approve"))  # resume RESENDS input

    assert len(persisted_inputs) == 2, f"expected a pause + a resume persist: {persisted_inputs}"
    assert persisted_inputs[0], "pause turn must persist the user input"
    assert persisted_inputs[1] == [], (
        f"resume turn re-persisted the input (double-write): {persisted_inputs[1]}"
    )


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


def test_predict_approval_turn_persists_user_prompt_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The approval turn early-returns before the normal persist; without an
    # explicit persist the user's prompt (and title) would be lost. Assert the
    # STORE contents, not just the response (the #338 lesson).
    from apx_agent._conversation import InMemoryConversationStore

    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    store = InMemoryConversationStore()
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=store, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        r1 = chat.predict(
            [ChatAgentMessage(role="user", content="email x@y.com please", id="u1")],
            custom_inputs={"session_id": "T"},
        )
    assert r1.custom_outputs["approval_required"]["tool_name"] == "send_email"
    # The prompt landed in the store on the approval turn (not lost) ...
    items = store.list_items("T", order="asc", limit=100).data
    blob = " ".join(str(it.data) for it in items)
    assert "email x@y.com please" in blob
    # ... and the conversation got a synthesized title.
    conv = store.get_conversation("T")
    assert conv is not None and conv.title


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


# ── #469: resume calls stamp an explicit approval-decision audit marker ───────
#
# USER_HASH is already stamped on every predict/invoke span (including resume
# calls), but nothing flagged a given span as *being* an approve/deny decision
# — an auditor could not tell a resume span from an ordinary turn without
# externally correlating two spans. approval_decision closes that gap.


def _spy_audit_calls(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    orig = module.set_audit_attrs

    def _spy(span: Any, **fields: Any) -> None:
        calls.append(fields)
        orig(span, **fields)

    monkeypatch.setattr(module, "set_audit_attrs", _spy)
    return calls


def test_predict_resume_stamps_approval_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent._chat_agent as _ca

    calls = _spy_audit_calls(monkeypatch, _ca)
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
            custom_inputs={"session_id": "T"},
        )
        chat.predict([], custom_inputs={"session_id": "T", "resume": "approve"})
    decisions = [c["approval_decision"] for c in calls if "approval_decision" in c]
    assert decisions == ["approve"]


def test_predict_stream_resume_stamps_approval_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent._chat_agent as _ca

    calls = _spy_audit_calls(monkeypatch, _ca)
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    chat = chat_agent_for(
        _agent(), model="m", conversation_store=None, checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(
            chat.predict_stream(
                [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
                custom_inputs={"session_id": "S"},
            )
        )
        list(chat.predict_stream([], custom_inputs={"session_id": "S", "resume": "approve"}))
    decisions = [c["approval_decision"] for c in calls if "approval_decision" in c]
    assert decisions == ["approve"]


def test_responses_invoke_resume_stamps_approval_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent._responses_agent as _ra

    calls = _spy_audit_calls(monkeypatch, _ra)
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    non_stream, _ = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("email x@y.com", thread_id="T2"))
        non_stream(_req("", thread_id="T2", resume="approve"))
    decisions = [c["approval_decision"] for c in calls if "approval_decision" in c]
    assert decisions == ["approve"]


def test_responses_stream_resume_stamps_approval_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent._responses_agent as _ra

    calls = _spy_audit_calls(monkeypatch, _ra)
    _install_model(monkeypatch, [_tool_call("x@y.com"), AIMessage(content="done")])
    _, streaming = compile_to_responses_agent(
        _agent(), model="m", checkpointer=InMemorySaver()
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(streaming(_req("email x@y.com", thread_id="S2")))
        list(streaming(_req("", thread_id="S2", resume="approve")))
    decisions = [c["approval_decision"] for c in calls if "approval_decision" in c]
    assert decisions == ["approve"]

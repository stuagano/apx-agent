"""Reality check (ctk): short-term memory is reachable through the SERVED path.

This is the stronger claim than the compile-level proof: that an `ApxChatAgent`
(`/invocations`) actually recalls a prior turn via the checkpointer. The test is
built to isolate exactly that — **`conversation_store=None`**, so the only way
turn 2 can know turn 1 is the checkpointer (not history replay). Turn 2 sends
**only the new message**, and the graph compiles per-`predict` against one
process-scoped saver, mirroring single-replica production.
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
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402

from apx_agent import LlmAgent, chat_agent_for  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    model = GenericFakeChatModel(messages=iter([AIMessage(content=f"r{i}") for i in range(20)]))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )


def _agent() -> LlmAgent:
    return LlmAgent(name="a", tools=[], instructions="Be helpful.")


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def test_served_predict_recalls_prior_turn_via_checkpointer() -> None:
    saver = InMemorySaver()  # process-scoped (one instance, shared per-predict compile)
    agent = _agent()
    # No conversation_store: the ONLY path from turn 1 to turn 2 is the checkpointer.
    chat = chat_agent_for(agent, model="m", conversation_store=None, checkpointer=saver)
    ci = {"session_id": "T"}

    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="my name is Zara", id="u1")],
            custom_inputs=ci,
        )
        # Turn 2 sends ONLY the new message — no replay.
        chat.predict(
            [ChatAgentMessage(role="user", content="what is my name", id="u2")],
            custom_inputs=ci,
        )
        probe = compile_to_langgraph(agent, ws=_ws(), model="m", checkpointer=saver)

    humans = [
        m.content
        for m in probe.get_state({"configurable": {"thread_id": "T"}}).values["messages"]
        if isinstance(m, HumanMessage)
    ]
    # Turn 1 persisted through the served predict, and turn 2 layered on it.
    assert "my name is Zara" in humans
    assert "what is my name" in humans


def test_served_predict_turn2_returns_only_new_output() -> None:
    """Regression: with a checkpointer, turn 2's response is ONLY the new
    assistant output — not leaked prior-turn history / echoed input. (invoke
    returns the full thread state; the slice must offset past the prior state.)"""
    saver = InMemorySaver()
    chat = chat_agent_for(_agent(), model="m", conversation_store=None, checkpointer=saver)
    ci = {"session_id": "T"}
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="my name is Zara", id="u1")],
            custom_inputs=ci,
        )
        r2 = chat.predict(
            [ChatAgentMessage(role="user", content="what is my name", id="u2")],
            custom_inputs=ci,
        )
    assert len(r2.messages) == 1 and r2.messages[0].role == "assistant"
    assert "my name is Zara" not in " ".join(m.content for m in r2.messages if m.content)


def test_served_predict_without_session_id_does_not_crash() -> None:
    """No session_id → a checkpointer-compiled graph would demand a thread_id.
    The served path must fall back to a stateless turn, not 500."""
    chat = chat_agent_for(_agent(), model="m", conversation_store=None, checkpointer=InMemorySaver())
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        resp = chat.predict(
            [ChatAgentMessage(role="user", content="hi", id="u1")]
        )  # no custom_inputs → no session_id
    assert resp is not None
    assert len(resp.messages) >= 1


def test_served_predict_stream_recalls_prior_turn() -> None:
    """Same guarantee on the streaming invoke site."""
    saver = InMemorySaver()
    agent = _agent()
    chat = chat_agent_for(agent, model="m", conversation_store=None, checkpointer=saver)
    ci = {"session_id": "S"}
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(
            chat.predict_stream(
                [ChatAgentMessage(role="user", content="I live in Oslo", id="u1")],
                custom_inputs=ci,
            )
        )
        list(
            chat.predict_stream(
                [ChatAgentMessage(role="user", content="where do I live", id="u2")],
                custom_inputs=ci,
            )
        )
        probe = compile_to_langgraph(agent, ws=_ws(), model="m", checkpointer=saver)
    humans = [
        m.content
        for m in probe.get_state({"configurable": {"thread_id": "S"}}).values["messages"]
        if isinstance(m, HumanMessage)
    ]
    assert "I live in Oslo" in humans and "where do I live" in humans


# ---------------------------------------------------------------------------
# Default-on: short-term memory is part of a served LlmAgent without wiring
# ---------------------------------------------------------------------------


def test_short_term_on_by_default_for_llm_agent() -> None:
    """No checkpointer, no conversation_store, no config — yet a served LlmAgent
    remembers across turns within a session_id (auto InMemorySaver)."""
    agent = _agent()
    chat = chat_agent_for(agent, model="m")  # nothing wired
    ci = {"session_id": "D"}
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="my favorite color is teal", id="u1")],
            custom_inputs=ci,
        )
        chat.predict(
            [ChatAgentMessage(role="user", content="what is my favorite color", id="u2")],
            custom_inputs=ci,
        )
        probe = compile_to_langgraph(agent, ws=_ws(), model="m", checkpointer=chat._checkpointer)
    humans = [
        m.content
        for m in probe.get_state({"configurable": {"thread_id": "D"}}).values["messages"]
        if isinstance(m, HumanMessage)
    ]
    assert "my favorite color is teal" in humans and "what is my favorite color" in humans


def test_no_auto_checkpointer_when_conversation_store_present() -> None:
    """A durable conversation store keeps its replay path — don't swap in an
    in-process saver (would lose history on restart)."""
    from apx_agent import InMemoryConversationStore

    chat = chat_agent_for(_agent(), model="m", conversation_store=InMemoryConversationStore())
    assert chat._checkpointer is None


def test_no_auto_checkpointer_for_composite_agent() -> None:
    """Composite agents can't take a checkpointer (compile would raise) — no
    auto-enable, no crash."""
    from apx_agent import SequentialAgent

    chat = chat_agent_for(SequentialAgent(agents=[_agent()]), model="m")
    assert chat._checkpointer is None

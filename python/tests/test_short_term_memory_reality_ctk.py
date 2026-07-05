"""Reality check (ctk) for thread-scoped short-term memory (LangGraph checkpointer).

Claim: an agent compiled with a checkpointer recalls a prior turn's state on a
later turn WITHOUT the caller replaying history.

The trap this guards against (the same mock-vs-live gap caught in managed
memory): a single long-lived executor would persist across turns even with a
broken checkpointer. Production compiles **per request**, so the saver must be
**process-scoped** (shared across compiles) to survive. The reality test mirrors
that exactly — two SEPARATE compiles sharing ONE saver, and turn 2 sends only
the new message — so a green result means short-term memory actually works in
the single-replica serving lifecycle, not just within one in-process object.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from apx_agent import LlmAgent, SequentialAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._executor import ExecutorConfig, TurnComplete  # noqa: E402
from apx_agent._langgraph_executor import LangGraphExecutor  # noqa: E402
from apx_agent._models import Message  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real Databricks chat model with a canned fake so the
    compiled graph runs without a live endpoint. Each LLM call returns the
    next canned AIMessage; the content is irrelevant — we assert on the
    checkpointed *message history*, not the model's answer."""
    replies = iter([AIMessage(content=f"reply-{i}") for i in range(20)])
    model = GenericFakeChatModel(messages=replies)
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


# ---------------------------------------------------------------------------
# Capability proof — persistence across SEPARATE compiles, one shared saver
# ---------------------------------------------------------------------------


def test_short_term_memory_persists_across_compiles() -> None:
    """Turn 2 (delta-only) on the same thread sees turn 1 — via the shared,
    process-scoped checkpointer, not via replay."""
    saver = InMemorySaver()  # process-scoped: created ONCE, shared below
    cfg = {"configurable": {"thread_id": "T"}}

    # Turn 1 — its own compile (mirrors a per-request compile).
    g1 = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=saver)
    g1.invoke({"messages": [HumanMessage(content="my name is Zara")]}, config=cfg)

    # Turn 2 — a DIFFERENT compile, SAME saver, sends ONLY the new message.
    g2 = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=saver)
    g2.invoke({"messages": [HumanMessage(content="what is my name")]}, config=cfg)

    humans = [
        m.content
        for m in g2.get_state(cfg).values["messages"]
        if isinstance(m, HumanMessage)
    ]
    # Turn 1's message is present though turn 2 never sent it → the checkpointer
    # carried it across the two independent compiles. Read-after-write.
    assert "my name is Zara" in humans
    assert "what is my name" in humans


def test_cross_thread_isolation() -> None:
    """A different thread_id never sees another thread's state."""
    saver = InMemorySaver()
    g1 = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=saver)
    g1.invoke(
        {"messages": [HumanMessage(content="secret for T")]},
        config={"configurable": {"thread_id": "T"}},
    )
    g2 = compile_to_langgraph(_agent(), ws=_ws(), model="m", checkpointer=saver)
    other = g2.get_state({"configurable": {"thread_id": "OTHER"}}).values
    assert not other.get("messages")


# ---------------------------------------------------------------------------
# Mechanism / guards
# ---------------------------------------------------------------------------


class TestExecutorThreadId:
    @pytest.mark.asyncio
    async def test_checkpointer_without_thread_id_raises(self) -> None:
        """A compiled-in checkpointer requires a thread_id, or LangGraph blows
        up mid-turn — the executor must couple them with a clear error."""
        ex = LangGraphExecutor(_agent(), ws=_ws(), model="m", checkpointer=InMemorySaver())
        with pytest.raises(ValueError, match="thread_id"):
            async for _ in ex.run_turn(
                [Message(role="user", content="hi")], [], "", ExecutorConfig()
            ):
                pass

    @pytest.mark.asyncio
    async def test_no_checkpointer_runs_stateless_without_thread_id(self) -> None:
        """Default path is unchanged: no checkpointer → no thread_id needed."""
        ex = LangGraphExecutor(_agent(), ws=_ws(), model="m")
        events = [
            e
            async for e in ex.run_turn(
                [Message(role="user", content="hi")], [], "", ExecutorConfig()
            )
        ]
        assert any(isinstance(e, TurnComplete) for e in events)

    @pytest.mark.asyncio
    async def test_executor_persists_across_two_executors(self) -> None:
        """End-to-end through run_turn: two executors sharing one saver, the
        second turn (delta-only) recalls the first."""
        saver = InMemorySaver()
        agent, ws = _agent(), _ws()
        cfg = ExecutorConfig(thread_id="T")

        ex1 = LangGraphExecutor(agent, ws=ws, model="m", checkpointer=saver)
        async for _ in ex1.run_turn(
            [Message(role="user", content="my name is Zara")], [], "", cfg
        ):
            pass
        ex2 = LangGraphExecutor(agent, ws=ws, model="m", checkpointer=saver)
        async for _ in ex2.run_turn(
            [Message(role="user", content="what is my name")], [], "", cfg
        ):
            pass

        compiled = ex2._get_compiled("m")
        humans = [
            m.content
            for m in compiled.get_state({"configurable": {"thread_id": "T"}}).values[
                "messages"
            ]
            if isinstance(m, HumanMessage)
        ]
        assert "my name is Zara" in humans and "what is my name" in humans


def test_checkpointer_on_non_llm_agent_is_rejected() -> None:
    """Scope guard: short-term memory is wired only on the LlmAgent path for
    now — a checkpointer on a composite agent fails loud, not silently."""
    composite = SequentialAgent(agents=[_agent()])
    with pytest.raises(NotImplementedError, match="LlmAgent"):
        compile_to_langgraph(composite, ws=_ws(), model="m", checkpointer=InMemorySaver())


def _msg_texts(items: Any) -> list[str]:
    out: list[str] = []
    for it in items:
        if it.type == "message":
            for b in it.data.content:
                if isinstance(b, dict) and "text" in b:
                    out.append(b["text"])
    return out


def test_predict_isolates_conversations_by_principal() -> None:
    """#491: two principals using the SAME client session_id must not share a
    conversation — the key is namespaced by the OBO principal. (_fake_chat is
    autouse, so the model is already stubbed.)"""
    from unittest.mock import patch

    from mlflow.types.agent import ChatAgentMessage

    from apx_agent import InMemoryConversationStore, chat_agent_for
    from apx_agent._obo import scope_session_key

    store = InMemoryConversationStore()
    chat = chat_agent_for(_agent(), model="m", conversation_store=store)

    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        chat.predict(
            [ChatAgentMessage(role="user", content="alice secret", id="1")],
            custom_inputs={"session_id": "S", "user_id": "alice"},
        )
        chat.predict(
            [ChatAgentMessage(role="user", content="bob message", id="2")],
            custom_inputs={"session_id": "S", "user_id": "bob"},
        )

    alice_id = scope_session_key({"session_id": "S"}, "alice")["session_id"]
    bob_id = scope_session_key({"session_id": "S"}, "bob")["session_id"]
    assert alice_id != bob_id

    alice_texts = _msg_texts(store.list_items(alice_id, order="asc").data)
    bob_texts = _msg_texts(store.list_items(bob_id, order="asc").data)
    assert any("alice secret" in t for t in alice_texts)
    # The isolation property: bob's conversation never contains alice's turn.
    assert not any("alice secret" in t for t in bob_texts)

"""Reality check (ctk): declared ``memory=`` recalls into the prompt each turn (#1087).

Drives the real compiled graph with a fake chat model that records the exact
messages it is sent, so each assertion is about the prompt the model saw, not
about a helper's return value. Reverting the middleware wiring in
``_compile_llm_agent`` makes the recall tests fail.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._defaults import get_databricks_headers  # noqa: E402
from apx_agent._memory_wiring import attach_declared_memory  # noqa: E402
from apx_agent._models import AgentConfig, MemoryBackendConfig  # noqa: E402
from ctk import expect  # noqa: E402

FACT = "the deploy target is apps"


class _RecordingModel(GenericFakeChatModel):
    """Fake chat model that keeps every prompt it was sent."""

    prompts: list[list[BaseMessage]] = []

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        return super()._generate(messages, *args, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self  # the declared recall/remember/forget tools are bound; never called


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _RecordingModel:
    fake = _RecordingModel(
        messages=iter([AIMessage(content=f"r{i}") for i in range(20)]), prompts=[]
    )
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: fake,
    )
    return fake


def _agent(**memory: Any) -> LlmAgent:
    agent = LlmAgent(name="a", tools=[], instructions="Be helpful.")
    attach_declared_memory(
        agent, AgentConfig(name="a", memory=MemoryBackendConfig(type="inmemory", **memory)), ws=None
    )
    return agent


def _turn(agent: LlmAgent, user: str, text: str, saver: Any = None) -> dict[str, Any]:
    ws = MagicMock(name="ws")
    graph = compile_to_langgraph(
        agent, ws=ws, model="m", headers=get_databricks_headers(user_id=user), checkpointer=saver
    )
    config = {"configurable": {"thread_id": user}} if saver is not None else None
    return graph.invoke({"messages": [HumanMessage(content=text)]}, config)


def _system_text(prompt: list[BaseMessage]) -> str:
    return "\n".join(m.text for m in prompt if m.type == "system")


@pytest.mark.unit
def test_remembered_fact_appears_in_next_turns_prompt(model: _RecordingModel) -> None:
    agent = _agent()
    agent._apx_memory_store.add({"principal_id": "alice", "content": FACT})

    _turn(agent, "alice", "where do we deploy?")

    system = _system_text(model.prompts[-1])
    expect(system).contains("### Relevant memories").contains(FACT).verify()
    # The author's instructions are kept; memories are added below them.
    assert system.index("Be helpful.") < system.index(FACT)


@pytest.mark.unit
def test_recall_is_scoped_to_the_callers_principal(model: _RecordingModel) -> None:
    agent = _agent()
    agent._apx_memory_store.add({"principal_id": "alice", "content": FACT})
    agent._apx_memory_store.add({"principal_id": "bob", "content": "bob prefers tea"})

    _turn(agent, "bob", "what do I like?")
    bob_system = _system_text(model.prompts[-1])
    _turn(agent, "alice", "what do I like?")
    alice_system = _system_text(model.prompts[-1])

    expect(bob_system).contains("bob prefers tea").verify()
    assert FACT not in bob_system
    expect(alice_system).contains(FACT).verify()
    assert "bob prefers tea" not in alice_system


@pytest.mark.unit
def test_raising_store_does_not_fail_the_turn_and_is_logged(
    model: _RecordingModel, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _agent()
    agent._apx_memory_store.recall = MagicMock(side_effect=RuntimeError("store down"))

    with caplog.at_level(logging.WARNING, logger="apx_agent._compile"):
        result = _turn(agent, "alice", "hello")

    assert result["messages"][-1].content == "r0"
    assert "memory auto-recall failed" in caplog.text and "store down" in caplog.text
    assert "### Relevant memories" not in _system_text(model.prompts[-1])


@pytest.mark.unit
def test_injected_memories_are_absent_from_saved_history(model: _RecordingModel) -> None:
    agent = _agent()
    agent._apx_memory_store.add({"principal_id": "alice", "content": FACT})
    saver = InMemorySaver()

    result = _turn(agent, "alice", "where do we deploy?", saver=saver)

    # The model saw the memory...
    expect(_system_text(model.prompts[-1])).contains(FACT).verify()
    # ...but neither the turn result nor the checkpointed thread carries it.
    saved = saver.get({"configurable": {"thread_id": "alice"}})
    assert saved is not None
    history = [str(m.content) for m in saved["channel_values"]["messages"]]
    assert history and not any(FACT in h for h in history)
    assert not any(FACT in str(m.content) for m in result["messages"])


@pytest.mark.unit
def test_recall_k_and_threshold_bound_the_block(model: _RecordingModel) -> None:
    agent = _agent(recall_k=1)
    agent._apx_memory_store.add({"principal_id": "alice", "content": "older fact"})
    agent._apx_memory_store.add({"principal_id": "alice", "content": "newer fact"})
    _turn(agent, "alice", "facts?")
    system = _system_text(model.prompts[-1])
    assert system.count("\n- ") == 1

    # Recency scores are <= 1, so a threshold above 1 drops every memory.
    strict = _agent(recall_min_score=1.5)
    strict._apx_memory_store.add({"principal_id": "alice", "content": FACT})
    _turn(strict, "alice", "facts?")
    assert FACT not in _system_text(model.prompts[-1])


@pytest.mark.unit
def test_auto_recall_off_switch(model: _RecordingModel) -> None:
    agent = _agent(auto_recall=False)
    agent._apx_memory_store.add({"principal_id": "alice", "content": FACT})
    _turn(agent, "alice", "where do we deploy?")
    assert FACT not in _system_text(model.prompts[-1])

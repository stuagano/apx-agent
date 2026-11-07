"""Reality check (ctk): strategy extraction runs on a bounded trigger (#1089).

Declares ``memory.extract_every_n_turns`` and drives real turns: the compiled
graph with a checkpointer, and the served ``chat_agent_for`` default path (no
store, no checkpointer wired, so the auto-injected ``InMemorySaver`` holds the
session). One fake stands in for the agent's model, answering chat turns and
extraction prompts. Every assertion reads the memory store back. Removing the
extract middleware from ``_compile_llm_agent`` makes these tests fail.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("mlflow")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from apx_agent import LlmAgent, chat_agent_for  # noqa: E402
from apx_agent import _compile, _memory_strategies  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._defaults import get_databricks_headers  # noqa: E402
from apx_agent._memory import MemoryFilter  # noqa: E402
from apx_agent._memory_strategies import BUILTIN_PROMPTS  # noqa: E402
from apx_agent._memory_wiring import attach_declared_memory  # noqa: E402
from apx_agent._models import AgentConfig, MemoryBackendConfig  # noqa: E402
from ctk import expect  # noqa: E402

FACT = "The user runs the billing service."
PREFERENCE = "Report status as a table."
SUMMARY = "The user asked about billing."


class _Model(GenericFakeChatModel):
    """Answers extraction prompts by strategy, everything else as a chat reply."""

    extraction_calls: list[str] = []
    fail_extractions: int = 0
    gate: Any = None

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        system = "\n".join(str(m.content) for m in messages if m.type == "system")
        strategy = next((s for s in BUILTIN_PROMPTS if BUILTIN_PROMPTS[s] in system), None)
        if strategy is None:
            self.messages = iter([AIMessage(content="ok")])
            return super()._generate(messages, *args, **kwargs)
        if self.gate is not None:
            self.gate.wait(10)
        self.extraction_calls.append(str(messages[-1].content))
        if self.fail_extractions:
            self.fail_extractions -= 1
            raise RuntimeError("model endpoint down")
        answers = {"facts": json.dumps([FACT]), "preferences": json.dumps([PREFERENCE]),
                   "summary": json.dumps(SUMMARY)}
        self.messages = iter([AIMessage(content=answers[strategy])])
        return super()._generate(messages, *args, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _Model:
    fake = _Model(messages=iter([]), extraction_calls=[])
    monkeypatch.setattr(
        _compile, "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: fake,
    )
    monkeypatch.setattr(_memory_strategies, "_RETRY_DELAY_S", 0.0)
    return fake


def _drain() -> None:
    """The extractor has one worker, so a no-op queued behind it finishes last."""
    _memory_strategies._EXTRACTOR.submit(lambda: None).result(timeout=20)


def _agent(every: int = 2) -> LlmAgent:
    agent = LlmAgent(name="billing", tools=[], instructions="Be helpful.")
    attach_declared_memory(agent, AgentConfig(name="billing", memory={
        "type": "inmemory", "strategies": ["facts", "preferences", "summary"],
        "extract_every_n_turns": every,
    }), ws=None)
    return agent


def _turn(agent: LlmAgent, saver: InMemorySaver, text: str, *, user: str = "user:alice") -> None:
    graph = compile_to_langgraph(
        agent, ws=MagicMock(name="ws"), model="agent-endpoint",
        headers=get_databricks_headers(user_id=user), checkpointer=saver,
    )
    graph.invoke({"messages": [HumanMessage(content=text)]}, {"configurable": {"thread_id": "session-1"}})


def _rows(agent: LlmAgent, namespace: str, user: str = "user:alice") -> list[str]:
    store = agent._apx_memory_store
    return [m.content for m in store.list(MemoryFilter(principal_id=user, namespace=namespace))]


@pytest.mark.unit
def test_memories_appear_in_strategy_namespaces_after_n_turns(model: _Model) -> None:
    agent, saver = _agent(every=2), InMemorySaver()
    _turn(agent, saver, "I run the billing service.")
    _drain()
    assert model.extraction_calls == []  # one turn: not due yet
    assert _rows(agent, "facts") == []

    _turn(agent, saver, "Always report status as a table.")
    _drain()
    for namespace, content in (("facts", FACT), ("preferences", PREFERENCE), ("summary", SUMMARY)):
        expect(_rows(agent, namespace), label=f"{namespace} read-back").nonempty().equals([content]).verify()
    # The transcript was the whole session from graph state, not just the last turn.
    expect(model.extraction_calls[0], label="extraction transcript") \
        .contains("user: I run the billing service.").contains("user: Always report status as a table.") \
        .contains("assistant: ok").verify()
    # Written under the session's user only.
    assert agent._apx_memory_store.list(MemoryFilter(principal_id="app-sp")) == []


@pytest.mark.unit
def test_extraction_runs_off_the_request_path(model: _Model) -> None:
    agent, saver = _agent(every=1), InMemorySaver()
    model.gate = threading.Event()
    _turn(agent, saver, "I run the billing service.")  # returns while extraction is blocked
    assert _rows(agent, "summary") == []
    model.gate.set()
    _drain()
    assert _rows(agent, "summary") == [SUMMARY]


@pytest.mark.unit
def test_failing_extractor_does_not_fail_the_turn_and_is_logged(
    model: _Model, caplog: pytest.LogCaptureFixture,
) -> None:
    agent, saver = _agent(every=1), InMemorySaver()
    model.fail_extractions = 100
    with caplog.at_level(logging.WARNING, logger="apx_agent._memory_strategies"):
        _turn(agent, saver, "I run the billing service.")  # no raise
        _drain()
    text = caplog.text
    expect(text, label="failure log").contains("model endpoint down").contains("attempt 3/3") \
        .contains("gave up after 3 attempts").verify()
    assert _rows(agent, "facts") == [] and _rows(agent, "summary") == []


@pytest.mark.unit
def test_redelivery_and_retriggering_do_not_duplicate(model: _Model) -> None:
    agent, saver = _agent(every=1), InMemorySaver()
    model.fail_extractions = 1  # first delivery fails, the retry lands
    _turn(agent, saver, "I run the billing service.")
    _turn(agent, saver, "Always report status as a table.")  # re-extracts the same session
    _drain()
    assert len(model.extraction_calls) >= 6
    assert _rows(agent, "summary") == [SUMMARY]
    assert _rows(agent, "facts") == [FACT]
    assert _rows(agent, "preferences") == [PREFERENCE]


@pytest.mark.unit
def test_served_default_path_extracts_under_the_caller(model: _Model) -> None:
    """``chat_agent_for`` with nothing wired: the auto-injected InMemorySaver holds the session."""
    agent = _agent(every=2)
    chat = chat_agent_for(agent, model="agent-endpoint")
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    ci = {"session_id": "T", "user_id": "user:bob"}
    with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
        for i, text in enumerate(("I run the billing service.", "Report status as a table.")):
            chat.predict([ChatAgentMessage(role="user", content=text, id=f"u{i}")], custom_inputs=ci)
    _drain()
    expect(_rows(agent, "summary", user="user:bob"), label="served summary read-back").equals([SUMMARY]).verify()
    assert _rows(agent, "summary") == []  # nothing under another user


@pytest.mark.unit
def test_config_bounds() -> None:
    with pytest.raises(ValidationError, match="needs memory.strategies"):
        MemoryBackendConfig(extract_every_n_turns=2)
    with pytest.raises(ValidationError, match="1 or more"):
        MemoryBackendConfig(strategies=["facts"], extract_every_n_turns=0)
    with pytest.raises(ValidationError, match="apx-agent memory extract"):
        MemoryBackendConfig(type="managed", store_name="m", strategies=["facts"], extract_every_n_turns=2)
    assert MemoryBackendConfig(strategies=["facts"]).extract_every_n_turns is None

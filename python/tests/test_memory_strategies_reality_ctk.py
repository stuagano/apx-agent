"""Reality check (ctk): typed memory strategies end to end (#1088).

Declares ``memory=`` with ``strategies`` on an agent, attaches it the way the
runtime does, extracts one fixture session with the agent's own model (the
same ``_build_chat_databricks`` the compiled graph uses, faked), reads each
namespace back from the store, then runs a compiled turn and checks the
recalled prompt block the model actually saw. Reverting the strategy wiring
(extractor namespaces, ``recalled_namespaces`` in the recall middleware) makes
these tests fail.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._defaults import get_databricks_headers  # noqa: E402
from apx_agent._memory import MemoryFilter  # noqa: E402
from apx_agent._memory_strategies import BUILTIN_PROMPTS, extract_agent_session  # noqa: E402
from apx_agent._memory_wiring import attach_declared_memory  # noqa: E402
from apx_agent._models import AgentConfig  # noqa: E402
from ctk import expect  # noqa: E402

FIXTURE_SESSION = """\
user: I run the billing service and we deploy it to Databricks Apps.
assistant: Got it. How should I report deploy status?
user: Always as a markdown table, never prose.
assistant: Understood.
"""
FACT = "The user runs the billing service, deployed to Databricks Apps."
PREFERENCE = "Report deploy status as a markdown table."
SUMMARY = "The user described the billing deploy and asked for tabular status."


class _Model(GenericFakeChatModel):
    """One fake stands in for the agent's model: extraction answers, then chat."""

    prompts: list[list[BaseMessage]] = []
    built: list[str] = []

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        system = "\n".join(str(m.content) for m in messages if m.type == "system")
        answers = {"facts": json.dumps([FACT]), "preferences": json.dumps([PREFERENCE]),
                   "summary": json.dumps(SUMMARY)}
        reply = next((a for s, a in answers.items() if BUILTIN_PROMPTS[s] in system), "ok")
        self.messages = iter([AIMessage(content=reply)])
        return super()._generate(messages, *args, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _Model:
    fake = _Model(messages=iter([]), prompts=[], built=[])

    def build(endpoint: str, *, temperature: float | None = None, max_tokens: int | None = None) -> _Model:
        fake.built.append(endpoint)
        return fake

    monkeypatch.setattr(_compile, "_build_chat_databricks", build)
    return fake


def _agent(**memory: Any) -> LlmAgent:
    agent = LlmAgent(name="billing", tools=[], instructions="Be helpful.")
    config = AgentConfig(name="billing", memory={
        "type": "inmemory", "strategies": ["facts", "preferences", "summary"], **memory,
    })
    attach_declared_memory(agent, config, ws=None)
    return agent


def _system_of_turn(agent: LlmAgent, model: _Model, user: str, text: str) -> str:
    graph = compile_to_langgraph(
        agent, ws=MagicMock(name="ws"), model="agent-endpoint", headers=get_databricks_headers(user_id=user),
    )
    graph.invoke({"messages": [HumanMessage(content=text)]})
    return "\n".join(m.text for m in model.prompts[-1] if m.type == "system")


@pytest.mark.unit
def test_fixture_session_lands_in_each_namespace_and_is_recalled(model: _Model) -> None:
    agent = _agent()
    extract_agent_session(
        agent, model="agent-endpoint", transcript=FIXTURE_SESSION,
        principal_id="user:alice", session_id="session-1",
    )
    # The agent's own model was used, built the way the compiled graph builds it.
    assert model.built == ["agent-endpoint"]

    store = agent._apx_memory_store
    for namespace, content in (("facts", FACT), ("preferences", PREFERENCE), ("summary", SUMMARY)):
        rows = store.list(MemoryFilter(principal_id="user:alice", namespace=namespace))
        expect([r.content for r in rows], label=f"{namespace} read-back").nonempty().equals([content]).verify()
    # Written under the session's user, never anyone else.
    assert store.list(MemoryFilter(principal_id="app-sp")) == []

    system = _system_of_turn(agent, model, "user:alice", "how is billing deployed and reported?")
    expect(system, label="recalled prompt block").contains("### Relevant memories") \
        .contains(f"- {FACT}").contains(f"- {PREFERENCE}").verify()
    assert SUMMARY not in system  # summary is opt-in


@pytest.mark.unit
def test_summary_is_recalled_only_when_listed(model: _Model) -> None:
    agent = _agent(recall_namespaces=["facts", "summary"])
    extract_agent_session(
        agent, model="agent-endpoint", transcript=FIXTURE_SESSION,
        principal_id="user:alice", session_id="session-1",
    )
    system = _system_of_turn(agent, model, "user:alice", "recap?")
    expect(system, label="recalled prompt block").contains(f"- {SUMMARY}").contains(f"- {FACT}").verify()
    assert PREFERENCE not in system


@pytest.mark.unit
def test_rerun_keeps_one_summary_and_other_users_see_nothing(model: _Model) -> None:
    agent = _agent()
    for _ in range(2):
        extract_agent_session(
            agent, model="agent-endpoint", transcript=FIXTURE_SESSION,
            principal_id="user:alice", session_id="session-1",
        )
    store = agent._apx_memory_store
    assert len(store.list(MemoryFilter(principal_id="user:alice", namespace="summary"))) == 1
    system = _system_of_turn(agent, model, "user:bob", "how is billing deployed?")
    assert FACT not in system and PREFERENCE not in system

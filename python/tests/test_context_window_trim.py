"""``LlmAgent(context_window_tokens=...)`` trims what each model call sees (#1086).

Drives the compiled ``create_agent`` graph with a recording fake model, so the
assertions are on the exact message list handed to the model and on the
checkpointed graph state — not on the helper in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import _trim_to_window, compile_to_langgraph  # noqa: E402

INSTRUCTIONS = "You are a terse assistant."
SENT: list[list[Any]] = []


class _RecordingFake(GenericFakeChatModel):
    """Scripted model that records every prompt (module list: Pydantic rejects attrs)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        SENT.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def lookup(q: str) -> str:
    """Look something up."""
    return f"result for {q} " + "r" * 200


def _install(monkeypatch: pytest.MonkeyPatch, replies: list[AIMessage]) -> None:
    SENT.clear()
    model = _RecordingFake(messages=iter(replies))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )


def _history(turns: int) -> list[Any]:
    """``turns`` completed turns, each with a tool call/result pair (~150 tokens)."""
    out: list[Any] = []
    for t in range(turns):
        out += [
            HumanMessage(content=f"question {t} " + "q" * 200),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": str(t)}, "id": f"c{t}"}]),
            ToolMessage(content="tool " + "t" * 200, tool_call_id=f"c{t}"),
            AIMessage(content=f"answer {t} " + "a" * 200),
        ]
    return out


def _assert_pairs_intact(prompt: list[Any]) -> None:
    calls = {c["id"] for m in prompt if isinstance(m, AIMessage) for c in m.tool_calls}
    results = {m.tool_call_id for m in prompt if isinstance(m, ToolMessage)}
    assert calls == results, f"split tool pair: calls={calls} results={results}"


def _run(agent: LlmAgent, messages: list[Any]) -> tuple[Any, dict[str, Any]]:
    graph = compile_to_langgraph(agent, ws=None, model="m", checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({"messages": messages}, config)
    return graph, config


def test_over_budget_session_sends_prompt_under_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = 600
    _install(monkeypatch, [AIMessage(content="done")])
    history = [*_history(20), HumanMessage(content="latest question")]
    assert count_tokens_approximately(history) > 5 * budget

    graph, config = _run(LlmAgent(tools=[lookup], instructions=INSTRUCTIONS, context_window_tokens=budget), history)

    (prompt,) = SENT
    assert isinstance(prompt[0], SystemMessage) and prompt[0].content == INSTRUCTIONS
    assert count_tokens_approximately(prompt) <= budget
    assert prompt[-1].content == "latest question"
    assert isinstance(prompt[1], HumanMessage), "window must start at a turn boundary"
    assert 1 < len(prompt) < len(history)
    _assert_pairs_intact(prompt)

    # Persisted history is untouched: every original message plus the reply.
    stored = graph.get_state(config).values["messages"]
    assert [m.content for m in stored] == [*(m.content for m in history), "done"]


def test_tool_loop_inside_a_trimmed_window_keeps_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = 700
    _install(monkeypatch, [
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "now"}, "id": "live"}]),
        AIMessage(content="final"),
    ])
    history = [*_history(12), HumanMessage(content="latest question")]

    graph, config = _run(LlmAgent(tools=[lookup], instructions=INSTRUCTIONS, context_window_tokens=budget), history)

    assert len(SENT) == 2
    for prompt in SENT:
        assert len(prompt) < len(history)
        assert count_tokens_approximately(prompt) <= budget
        _assert_pairs_intact(prompt)
    # The second hop carries the live call and its result, after the latest question.
    assert [type(m) for m in SENT[1][-3:]] == [HumanMessage, AIMessage, ToolMessage]
    assert len(graph.get_state(config).values["messages"]) == len(history) + 3


def test_unset_budget_sends_full_history(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [AIMessage(content="done")])
    history = [*_history(20), HumanMessage(content="latest question")]

    _run(LlmAgent(tools=[lookup], instructions=INSTRUCTIONS), history)

    (prompt,) = SENT
    assert len(prompt) == len(history) + 1


@pytest.mark.parametrize("budget", range(0, 1400, 50))
def test_trim_never_splits_pairs_and_keeps_system(budget: int) -> None:
    system = SystemMessage(content="state notes")
    kept = _trim_to_window([system, *_history(6), HumanMessage(content="now")], [], budget)

    assert kept[0] is system
    assert isinstance(kept[1], HumanMessage)
    assert kept[-1].content == "now", "the latest turn is kept even over budget"
    _assert_pairs_intact(kept)

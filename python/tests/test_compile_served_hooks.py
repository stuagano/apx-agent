"""Served-path agent-node wrapper: G1 guardrails/callbacks +
G3 keyed state (output_key, {key} templating).

Exercises _wrap_agent_node against a fake inner runnable (no real LLM).
See docs/design/served-path-guards-and-identity.md (G1) and
docs/design/keyed-shared-state.md (G3).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from apx_agent import LlmAgent
from apx_agent._compile import (
    _agent_needs_node_wrap,
    _has_template,
    _render_template,
    _wrap_agent_node,
)


class _FakeRunnable:
    """Echoes input + one AI reply; records the messages it was called with."""

    def __init__(self, reply: str = "hello") -> None:
        self.reply = reply
        self.called = False
        self.seen_messages: list = []

    async def ainvoke(self, state: dict) -> dict:
        self.called = True
        self.seen_messages = list(state["messages"])
        return {"messages": list(state["messages"]) + [AIMessage(content=self.reply)]}


def _agent(**kw) -> LlmAgent:
    kw.setdefault("instructions", "Help.")
    return LlmAgent(tools=[], name="x", **kw)


# -- wrap decision -------------------------------------------------------------


def test_plain_agent_needs_no_wrap() -> None:
    assert _agent_needs_node_wrap(_agent()) is False


@pytest.mark.parametrize(
    "kw",
    [
        {"input_guardrails": [lambda m: None]},
        {"output_guardrails": [lambda t: None]},
        {"before_agent_callback": lambda m: None},
        {"output_key": "facts"},
        {"instructions": "Use {facts}."},
    ],
)
def test_agent_needs_wrap_when_any_hook_or_key(kw) -> None:
    assert _agent_needs_node_wrap(_agent(**kw)) is True


def test_has_template_detects_brace_key() -> None:
    assert _has_template(_agent(instructions="Summarize {facts} briefly.")) is True
    assert _has_template(_agent(instructions="No placeholders here.")) is False


def test_render_template_substitutes_known_leaves_unknown() -> None:
    assert _render_template("A={a} B={b}", {"a": "1"}) == "A=1 B={b}"


# -- G1: guardrails + callbacks ------------------------------------------------


@pytest.mark.asyncio
async def test_input_guardrail_rejects_and_skips_agent() -> None:
    fake = _FakeRunnable()
    graph = _wrap_agent_node(
        _agent(input_guardrails=[lambda m: "BLOCKED"]), fake, templated=False
    )
    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert fake.called is False
    assert any(isinstance(m, AIMessage) and m.content == "BLOCKED" for m in out["messages"])


@pytest.mark.asyncio
async def test_output_guardrail_replaces_final_text() -> None:
    fake = _FakeRunnable(reply="raw")
    graph = _wrap_agent_node(
        _agent(output_guardrails=[lambda t: "REDACTED"]), fake, templated=False
    )
    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    ai = [m for m in out["messages"] if isinstance(m, AIMessage)]
    assert ai and ai[-1].content == "REDACTED"


@pytest.mark.asyncio
async def test_before_and_after_callbacks_fire() -> None:
    seen: list[str] = []
    fake = _FakeRunnable(reply="answer")
    graph = _wrap_agent_node(
        _agent(
            before_agent_callback=lambda m: seen.append("before"),
            after_agent_callback=lambda t: seen.append(f"after:{t}"),
        ),
        fake,
        templated=False,
    )
    await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert seen == ["before", "after:answer"]


# -- G3: keyed state -----------------------------------------------------------


@pytest.mark.asyncio
async def test_output_key_writes_final_text_to_state() -> None:
    fake = _FakeRunnable(reply="42 customers")
    graph = _wrap_agent_node(_agent(output_key="facts"), fake, templated=False)
    out = await graph.ainvoke({"messages": [HumanMessage(content="how many?")]})
    assert out["state"]["facts"] == "42 customers"


@pytest.mark.asyncio
async def test_template_injects_rendered_system_prompt_from_state() -> None:
    fake = _FakeRunnable(reply="ok")
    graph = _wrap_agent_node(
        _agent(instructions="Summarize: {facts}"), fake, templated=True
    )
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")], "state": {"facts": "Q3 up"}}
    )
    sys = [m for m in fake.seen_messages if isinstance(m, SystemMessage)]
    assert sys and sys[0].content == "Summarize: Q3 up"
    # injected system prompt is internal — not added to the conversation
    assert not any(isinstance(m, SystemMessage) for m in out["messages"])


@pytest.mark.asyncio
async def test_output_key_threads_into_template() -> None:
    """step 1 writes output_key; step 2 reads it via {key} (what a sequential
    graph does — the shared state channel carries the value)."""
    extractor = _FakeRunnable(reply="ACME-123")
    summarizer = _FakeRunnable(reply="done")
    step1 = _wrap_agent_node(_agent(output_key="account"), extractor, templated=False)
    step2 = _wrap_agent_node(
        _agent(instructions="Look up {account}."), summarizer, templated=True
    )
    s1 = await step1.ainvoke({"messages": [HumanMessage(content="find it")]})
    await step2.ainvoke(
        {"messages": [HumanMessage(content="next")], "state": s1["state"]}
    )
    sys = [m for m in summarizer.seen_messages if isinstance(m, SystemMessage)]
    assert sys and sys[0].content == "Look up ACME-123."


@pytest.mark.asyncio
async def test_sequential_inserts_continuation_user_turn(monkeypatch) -> None:
    """A compiled SequentialAgent must not hand the next step a conversation that
    ends on an assistant message — prefill-strict endpoints (Claude) reject it.
    The continuation node appends a user turn between steps, mirroring run().
    """
    from unittest.mock import MagicMock

    from apx_agent import SequentialAgent, compile_to_langgraph
    from apx_agent._agents import SEQUENTIAL_CONTINUATION
    from apx_agent import _compile

    seen: dict[str, list] = {}  # agent name -> messages it was invoked with

    def _fake_compile_llm(agent, ctx, *, bake_prompt=True):  # type: ignore[no-untyped-def]
        name = agent._name

        async def _node(state: dict) -> dict:
            seen[name] = list(state["messages"])
            return {"messages": [AIMessage(content=name)]}  # delta only

        return _node

    monkeypatch.setattr(_compile, "_compile_llm_agent", _fake_compile_llm)

    pipeline = SequentialAgent(
        agents=[
            LlmAgent(tools=[], name="a", instructions="Help."),
            LlmAgent(tools=[], name="b", instructions="Help."),
        ],
        name="pipe",
    )
    graph = compile_to_langgraph(
        pipeline, ws=MagicMock(), model="databricks-claude-sonnet-4-6"
    )
    await graph.ainvoke({"messages": [HumanMessage(content="start")]})

    # step "b" must see a user turn on the tail, not step "a"'s assistant message.
    assert isinstance(seen["b"][-1], HumanMessage)
    assert seen["b"][-1].content == SEQUENTIAL_CONTINUATION
    # accumulation preserved: step a's output still precedes the continuation.
    assert any(isinstance(m, AIMessage) and m.content == "a" for m in seen["b"])

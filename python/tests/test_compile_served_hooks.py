"""G1 enforcement: guardrails + before/after_agent callbacks on the served
(compiled-graph) path. See docs/design/served-path-guards-and-identity.md.

Exercises _wrap_served_hooks against a fake inner runnable (no real LLM).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from apx_agent import LlmAgent
from apx_agent._compile import _wrap_served_hooks


class _FakeRunnable:
    """Stands in for the create_agent runnable: echoes input + one AI reply."""

    def __init__(self, reply: str = "hello") -> None:
        self.reply = reply
        self.called = False

    async def ainvoke(self, state: dict) -> dict:
        self.called = True
        return {"messages": list(state["messages"]) + [AIMessage(content=self.reply)]}


def _agent(**kw) -> LlmAgent:
    return LlmAgent(tools=[], name="x", instructions="Help.", **kw)


def test_no_hooks_returns_runnable_unchanged() -> None:
    fake = _FakeRunnable()
    assert _wrap_served_hooks(_agent(), fake) is fake


@pytest.mark.asyncio
async def test_input_guardrail_rejects_and_skips_agent() -> None:
    fake = _FakeRunnable()
    agent = _agent(input_guardrails=[lambda messages: "BLOCKED"])
    graph = _wrap_served_hooks(agent, fake)

    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    assert fake.called is False  # the agent never ran
    assert any(isinstance(m, AIMessage) and m.content == "BLOCKED" for m in out["messages"])


@pytest.mark.asyncio
async def test_output_guardrail_replaces_final_text() -> None:
    fake = _FakeRunnable(reply="raw secret answer")
    agent = _agent(output_guardrails=[lambda text: "REDACTED"])
    graph = _wrap_served_hooks(agent, fake)

    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    assert fake.called is True
    ai = [m for m in out["messages"] if isinstance(m, AIMessage)]
    assert ai and ai[-1].content == "REDACTED"


@pytest.mark.asyncio
async def test_before_and_after_callbacks_fire() -> None:
    seen: list[str] = []
    fake = _FakeRunnable(reply="answer")
    agent = _agent(
        before_agent_callback=lambda messages: seen.append("before"),
        after_agent_callback=lambda text: seen.append(f"after:{text}"),
    )
    graph = _wrap_served_hooks(agent, fake)

    await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    assert seen == ["before", "after:answer"]


@pytest.mark.asyncio
async def test_passthrough_when_no_guard_fires() -> None:
    """A configured-but-passing guard leaves the agent's output intact."""
    fake = _FakeRunnable(reply="the real answer")
    agent = _agent(
        input_guardrails=[lambda messages: None],
        output_guardrails=[lambda text: None],
    )
    graph = _wrap_served_hooks(agent, fake)

    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    ai = [m for m in out["messages"] if isinstance(m, AIMessage)]
    assert ai and ai[-1].content == "the real answer"


@pytest.mark.asyncio
async def test_streams_as_one_node_update() -> None:
    """The served paths use stream_mode='updates'; a guarded agent surfaces as
    one 'agent' node update carrying the (possibly replaced) output."""
    fake = _FakeRunnable(reply="streamed answer")
    agent = _agent(output_guardrails=[lambda text: None])
    graph = _wrap_served_hooks(agent, fake)

    chunks = [
        c async for c in graph.astream(
            {"messages": [HumanMessage(content="hi")]}, stream_mode="updates"
        )
    ]
    msgs = [m for c in chunks for m in c.get("agent", {}).get("messages", [])]
    assert any(isinstance(m, AIMessage) and m.content == "streamed answer" for m in msgs)

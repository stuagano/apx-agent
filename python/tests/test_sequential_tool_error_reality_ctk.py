"""Claim-vs-reality (ctk): a tool ToolError is contained, not pipeline-fatal (#562).

The claim: in a multi-step ``SequentialAgent`` a single tool that raises
``ToolError`` becomes a legible tool result the agent reasons about, and the
pipeline continues to its final step — instead of one raise aborting every
downstream step as an opaque HTTP 500.

The counter-claim this also pins: a *bare* ``RuntimeError`` (a genuine bug, not
a declared finding) must still propagate and fail loud, so containment never
degrades into a blanket catch that hides defects. ``ToolError`` is deliberately
not a ``RuntimeError``, so the two live on opposite sides of the line.

Drives the compiled LangGraph directly with a canned fake chat model (the same
harness ``test_policy_interrupt_reality_ctk`` uses), so no real model/network is
touched — the assertion is about control flow through the real compiled graph.
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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from apx_agent import Agent, SequentialAgent, ToolError  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402


class _ToolFake(GenericFakeChatModel):
    """Canned chat model that also accepts bind_tools (create_agent needs it)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _install_model(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    # One shared model instance for every step: all LLM calls across the
    # pipeline draw from this single ordered message queue.
    model = _ToolFake(messages=iter(messages))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )


def failing_query(q: str) -> str:
    """Run a SQL query."""
    raise ToolError(f"Query failed: catalog 'main' is not accessible ({q})")


def buggy_query(q: str) -> str:
    """Run a SQL query."""
    raise RuntimeError(f"unexpected None in row parser ({q})")


def _query_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "step0_tool", "args": {"q": "orders"}, "id": "t1"}],
    )


def _pipeline(step0_tool: Any) -> SequentialAgent:
    step0_tool.__name__ = "step0_tool"
    step0 = Agent(name="step_0", tools=[step0_tool], instructions="Investigate.")
    step1 = Agent(name="step_1", instructions="Summarize.")
    return SequentialAgent([step0, step1])


def _tool_msgs(result: dict[str, Any]) -> list[ToolMessage]:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def test_tool_error_is_contained_pipeline_reaches_final_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # step_0 calls the failing tool → ToolError → contained → step_0 answers →
    # step_1 answers. Three LLM turns across the two steps.
    _install_model(
        monkeypatch,
        [
            _query_call(),
            AIMessage(content="step 0: the query was denied, noting as a finding"),
            AIMessage(content="step 1 summary complete"),
        ],
    )
    graph = compile_to_langgraph(_pipeline(failing_query), ws=_ws(), model="m")

    # Does NOT raise — the ToolError is a tool result, not a pipeline abort.
    result = graph.invoke(
        {"messages": [HumanMessage(content="investigate main.default.orders")]}
    )

    # The failure is legible in the transcript, as an error ToolMessage.
    errors = [m for m in _tool_msgs(result) if m.status == "error"]
    assert errors, "ToolError was not contained as an error ToolMessage"
    assert "is not accessible" in str(errors[0].content)

    # The pipeline reached its final step — step_1's answer is present.
    texts = [str(m.content) for m in result["messages"] if isinstance(m, AIMessage)]
    assert any("step 1 summary complete" in t for t in texts), (
        "pipeline did not reach step_1 after the step_0 tool failure"
    )


def test_bare_runtime_error_still_aborts_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuine bug (bare RuntimeError, not a declared ToolError) must fail
    # loud — the pipeline raises rather than silently swallowing the defect.
    _install_model(monkeypatch, [_query_call()])
    graph = compile_to_langgraph(_pipeline(buggy_query), ws=_ws(), model="m")

    with pytest.raises(RuntimeError, match="row parser"):
        graph.invoke(
            {"messages": [HumanMessage(content="investigate main.default.orders")]}
        )

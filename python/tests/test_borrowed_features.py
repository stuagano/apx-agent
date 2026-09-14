"""Gate tests for the borrowed-features WIRING PRD (fixes Isaac Review on #766).

Every budget/deferred-loading AC here drives the **compiled/served path**, not a
seam in isolation:

* AC-1/AC-2 — deferred tool loading is honored at ``compile_to_langgraph`` time
  (assert on the bound tools of the compiled artifact, not ``assemble_llm_tools``).
* AC-3/AC-4/AC-5 — ``session_budget`` is enforced on every served entrypoint
  (chat ``predict``/``predict_stream``, responses ``invoke``/``stream``).
* AC-6/AC-7 — ``ParallelAgent`` edge cases (empty input, system-message forward).
* AC-8 — ``SessionBudgetExceeded`` docstring matches per-served-turn behavior.

Harness-version tracing + CLI surfacing and the exception shape (the other two
#766 features) keep their coverage at the bottom of the file.
"""

from __future__ import annotations

import importlib.metadata
import json
from typing import Any
from unittest.mock import MagicMock, patch

import mlflow.types.responses as mlflow_responses
import pytest
from click.testing import CliRunner
from langchain_core.messages import AIMessage

from apx_agent import LlmAgent, ParallelAgent, SessionBudgetExceeded, compile_to_langgraph
from apx_agent._agents import TOOL_SEARCH_TOOL_NAME, BaseAgent
from apx_agent._audit import AuditAttrs, stamp_harness_version
from apx_agent._chat_agent import chat_agent_for
from apx_agent._models import Message
from apx_agent._responses_agent import compile_to_responses_agent
from apx_agent.cli import main

INSTALLED_VERSION = importlib.metadata.version("apx-agent")
ResponsesAgentRequest = mlflow_responses.ResponsesAgentRequest


def _tool_a(x: str) -> str:
    """Tool A."""
    return x


def _tool_b(x: str) -> str:
    """Tool B."""
    return x


def _tool_c(x: str) -> str:
    """Tool C."""
    return x


# ==========================================================================
# AC-1 / AC-2 — deferred tool loading honored at COMPILE time
# ==========================================================================


def _compile_capture_tools(agent: LlmAgent, create_agent_mock: Any) -> list[str]:
    """Compile ``agent`` and return the names of the tools bound to create_agent."""
    with patch("langchain.agents.create_agent", create_agent_mock), patch(
        "apx_agent._compile._build_chat_databricks", return_value=MagicMock(name="llm")
    ):
        compile_to_langgraph(agent, ws=MagicMock(name="ws"), model="m")
    return [t.name for t in create_agent_mock.call_args.kwargs["tools"]]


def test_ac1_compile_deferred_binds_only_tool_search() -> None:
    deferred = LlmAgent(tools=[_tool_a, _tool_b, _tool_c], tool_loading="deferred")
    names = _compile_capture_tools(deferred, MagicMock(name="create_agent"))
    assert TOOL_SEARCH_TOOL_NAME in names
    assert not ({"_tool_a", "_tool_b", "_tool_c"} & set(names)), names

    eager = LlmAgent(tools=[_tool_a, _tool_b, _tool_c])
    eager_names = _compile_capture_tools(eager, MagicMock(name="create_agent"))
    assert {"_tool_a", "_tool_b", "_tool_c"} <= set(eager_names), eager_names
    assert TOOL_SEARCH_TOOL_NAME not in eager_names


def test_ac2_deferred_falls_back_to_eager_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    deferred = LlmAgent(tools=[_tool_a, _tool_b, _tool_c], tool_loading="deferred")
    calls: list[list[str]] = []

    def _create_agent(**kwargs: Any) -> Any:
        names = [t.name for t in kwargs["tools"]]
        calls.append(names)
        if len(calls) == 1:  # endpoint rejects the tool_search tool the first time
            raise RuntimeError("endpoint cannot accept deferred tool_search")
        return MagicMock(name="runnable")

    with patch("langchain.agents.create_agent", side_effect=_create_agent), patch(
        "apx_agent._compile._build_chat_databricks", return_value=MagicMock(name="llm")
    ), caplog.at_level("WARNING"):
        compile_to_langgraph(deferred, ws=MagicMock(name="ws"), model="m")

    assert len(calls) == 2, calls
    assert {"_tool_a", "_tool_b", "_tool_c"} <= set(calls[1]), calls[1]
    assert any("eager" in r.message for r in caplog.records), caplog.text


# ==========================================================================
# AC-3 / AC-4 / AC-5 — session_budget enforced on every SERVED entrypoint
# ==========================================================================


def _budget_graph(input_tokens: int, output_tokens: int) -> MagicMock:
    """Fake compiled graph whose one AIMessage carries usage_metadata.

    Supports the three shapes the served paths drive: sync ``invoke``, chat
    ``stream(stream_mode="updates")`` (yields ``{node: output}`` dicts), and
    responses ``stream(stream_mode=["updates","messages"])`` (yields
    ``(mode, data)`` tuples).
    """
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    graph = MagicMock(name="budget_graph")

    def _msg() -> AIMessage:
        return AIMessage(content="done", id="ai-1", usage_metadata=usage)

    def _invoke(state: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {"messages": [*state["messages"], _msg()]}

    def _stream(state: dict[str, Any], stream_mode: Any = "updates", **_kw: Any):
        update = {"agent": {"messages": [_msg()]}}
        if isinstance(stream_mode, list):  # responses combined-mode contract
            yield ("updates", update)
        else:  # chat "updates" contract
            yield update

    graph.invoke.side_effect = _invoke
    graph.stream.side_effect = _stream
    return graph


def _chat(agent: LlmAgent) -> Any:
    return chat_agent_for(agent, model="m", conversation_store=None)


def _chat_msg(text: str) -> Any:
    from mlflow.types.agent import ChatAgentMessage

    return ChatAgentMessage(role="user", content=text, id="u1")


def test_ac3_served_predict_over_budget_raises() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 100})
    with patch(
        "apx_agent._chat_agent.compile_to_langgraph",
        return_value=_budget_graph(60, 60),
    ), patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        with pytest.raises(SessionBudgetExceeded) as exc:
            _chat(agent).predict([_chat_msg("hi")])
    assert exc.value.spent >= 100
    assert exc.value.cap == 100


def test_ac4_served_predict_stream_over_budget_raises() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 100})
    with patch(
        "apx_agent._chat_agent.compile_to_langgraph",
        return_value=_budget_graph(60, 60),
    ), patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        with pytest.raises(SessionBudgetExceeded):
            list(_chat(agent).predict_stream([_chat_msg("hi")]))


def test_ac4_served_responses_invoke_over_budget_raises() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 100})
    non_streaming, _ = compile_to_responses_agent(agent, model="m")
    with patch(
        "apx_agent._responses_agent.compile_to_langgraph",
        return_value=_budget_graph(60, 60),
    ), patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        with pytest.raises(SessionBudgetExceeded):
            non_streaming(ResponsesAgentRequest(input=[{"role": "user", "content": "hi"}]))


def test_ac4_served_responses_stream_over_budget_raises() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 100})
    _, streaming = compile_to_responses_agent(agent, model="m")
    with patch(
        "apx_agent._responses_agent.compile_to_langgraph",
        return_value=_budget_graph(60, 60),
    ), patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        with pytest.raises(SessionBudgetExceeded):
            list(streaming(ResponsesAgentRequest(input=[{"role": "user", "content": "hi"}])))


def test_ac5_served_under_budget_returns_normally() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 1000})
    with patch(
        "apx_agent._chat_agent.compile_to_langgraph",
        return_value=_budget_graph(25, 25),
    ), patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        resp = _chat(agent).predict([_chat_msg("hi")])
    assert any(m.content == "done" for m in resp.messages)


# ==========================================================================
# AC-6 / AC-7 — ParallelAgent edge cases
# ==========================================================================


class _RecordingAgent(BaseAgent):
    def __init__(self) -> None:
        self.received: list[Message] = []

    async def run(self, messages: list[Message], request: Any) -> str:
        self.received = list(messages)
        return "ok"


@pytest.mark.asyncio
async def test_ac6_parallel_run_empty_no_indexerror() -> None:
    agent = ParallelAgent([_RecordingAgent()])
    assert await agent.run([], MagicMock()) == ""


@pytest.mark.asyncio
async def test_ac7_parallel_forwards_system_and_last_user() -> None:
    branches = [_RecordingAgent(), _RecordingAgent()]
    await ParallelAgent(branches).run(
        [
            Message(role="system", content="sys"),
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="last"),
        ],
        MagicMock(),
    )
    for branch in branches:
        assert [(m.role, m.content) for m in branch.received] == [
            ("system", "sys"),
            ("user", "last"),
        ]


# ==========================================================================
# AC-8 — SessionBudgetExceeded docstring matches per-served-turn behavior
# ==========================================================================


def test_ac8_docstring_no_same_iteration_claim() -> None:
    doc = SessionBudgetExceeded.__doc__
    assert doc is not None
    assert "same iteration" not in doc.lower()
    assert "served-turn" in doc.lower()


# ==========================================================================
# #766 harness-version tracing + CLI surfacing + exception shape (unchanged)
# ==========================================================================


def test_harness_version_span_attribute() -> None:
    span = MagicMock()
    stamp_harness_version(span)
    span.set_attribute.assert_any_call(AuditAttrs.HARNESS_VERSION, INSTALLED_VERSION)


def test_status_harness_version_human() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert f"harness: {INSTALLED_VERSION}" in result.output


def test_status_harness_version_json() -> None:
    result = CliRunner().invoke(main, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["harness_version"] == INSTALLED_VERSION


def test_session_budget_exceeded_exception_shape() -> None:
    exc = SessionBudgetExceeded(spent=150, cap=100)
    assert isinstance(exc, Exception)
    assert exc.spent == 150
    assert exc.cap == 100

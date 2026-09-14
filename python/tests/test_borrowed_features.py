"""Gate tests for the borrowed-features PRD (OpenAI Agents API parity).

Covers the four capabilities: deferred tool loading (AC-1), session token
budget (AC-2/AC-3/AC-8), ParallelAgent context isolation (AC-4), and harness
version tracing + CLI surfacing (AC-5/AC-6/AC-7).
"""

from __future__ import annotations

import importlib.metadata
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from langchain_core.messages import AIMessage

from apx_agent import LlmAgent, ParallelAgent, SessionBudgetExceeded
from apx_agent._agents import TOOL_SEARCH_TOOL_NAME, BaseAgent
from apx_agent._audit import AuditAttrs, stamp_harness_version
from apx_agent._executor import ExecutorConfig, TurnComplete
from apx_agent._langgraph_executor import LangGraphExecutor
from apx_agent._models import Message
from apx_agent.cli import main

INSTALLED_VERSION = importlib.metadata.version("apx-agent")


def _tool_a(x: str) -> str:
    """Tool A."""
    return x


def _tool_b(x: str) -> str:
    """Tool B."""
    return x


def _tool_c(x: str) -> str:
    """Tool C."""
    return x


class _UsageGraph:
    """Fake compiled graph whose single AIMessage carries usage_metadata."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self._usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    async def ainvoke(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        msg = AIMessage(content="done", usage_metadata=self._usage)
        return {"messages": [*state.get("messages", []), msg]}


async def _drain(executor: LangGraphExecutor) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="",
        config=ExecutorConfig(model="m"),
    ):
        events.append(event)
    return events


# --------------------------------------------------------------------------
# AC-1 — deferred tool loading
# --------------------------------------------------------------------------


def test_ac1_deferred_tool_loading_first_call() -> None:
    deferred = LlmAgent(tools=[_tool_a, _tool_b, _tool_c], tool_loading="deferred")
    tools = deferred.assemble_llm_tools()
    assert len(tools) == 1
    assert tools[0].name == TOOL_SEARCH_TOOL_NAME
    # eager (default) sends every registered schema — the contrast that proves
    # deferred actually withheld them.
    eager = LlmAgent(tools=[_tool_a, _tool_b, _tool_c])
    assert len(eager.assemble_llm_tools()) == 3


# --------------------------------------------------------------------------
# AC-2 / AC-3 — session token budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_session_budget_exceeded() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 100})
    graph = _UsageGraph(input_tokens=60, output_tokens=60)
    with patch("apx_agent._compile.compile_to_langgraph", return_value=graph):
        executor = LangGraphExecutor(agent, model="m")
        with pytest.raises(SessionBudgetExceeded) as exc_info:
            await _drain(executor)
    assert exc_info.value.spent >= 100
    assert exc_info.value.cap == 100


@pytest.mark.asyncio
async def test_ac3_session_budget_not_exceeded() -> None:
    agent = LlmAgent(tools=[], session_budget={"tokens": 1000})
    graph = _UsageGraph(input_tokens=25, output_tokens=25)
    with patch("apx_agent._compile.compile_to_langgraph", return_value=graph):
        executor = LangGraphExecutor(agent, model="m")
        events = await _drain(executor)
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes and completes[-1].response == "done"


# --------------------------------------------------------------------------
# AC-4 — ParallelAgent context isolation
# --------------------------------------------------------------------------


class _RecordingAgent(BaseAgent):
    def __init__(self) -> None:
        self.received: list[Message] = []

    async def run(self, messages: list[Message], request: Any) -> str:
        self.received = list(messages)
        return "ok"


@pytest.mark.asyncio
async def test_ac4_parallel_agent_context_isolation() -> None:
    branches = [_RecordingAgent(), _RecordingAgent()]
    agent = ParallelAgent(branches)
    convo = [
        Message(role="system", content="sys"),
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="last"),
    ]
    await agent.run(convo, MagicMock())
    for branch in branches:
        users = [m for m in branch.received if m.role == "user"]
        assert len(users) == 1
        assert users[0].content == "last"


# --------------------------------------------------------------------------
# AC-5 — harness version span attribute
# --------------------------------------------------------------------------


def test_ac5_harness_version_span_attribute() -> None:
    span = MagicMock()
    stamp_harness_version(span)
    span.set_attribute.assert_any_call(AuditAttrs.HARNESS_VERSION, INSTALLED_VERSION)


# --------------------------------------------------------------------------
# AC-6 / AC-7 — apx-agent status surfaces harness version
# --------------------------------------------------------------------------


def test_ac6_status_harness_version_human() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert f"harness: {INSTALLED_VERSION}" in result.output


def test_ac7_status_harness_version_json() -> None:
    result = CliRunner().invoke(main, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["harness_version"] == INSTALLED_VERSION


# --------------------------------------------------------------------------
# AC-8 — SessionBudgetExceeded shape
# --------------------------------------------------------------------------


def test_ac8_session_budget_exceeded_exception_shape() -> None:
    exc = SessionBudgetExceeded(spent=150, cap=100)
    assert isinstance(exc, Exception)
    assert exc.spent == 150
    assert exc.cap == 100

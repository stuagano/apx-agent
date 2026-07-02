"""Claim-vs-reality: LoopAgent/HandoffAgent inner agents must be governed (#370).

`_compile_loop_agent` and the HandoffAgent node builder compiled their wrapped
inner `LlmAgent` via a raw `create_agent(...)` instead of routing through
`_compile_llm_agent`. That silently dropped the inner agent's governance
middleware (`_governance_exception_middleware`), callback handler, `state_schema`,
and generation config — so a PolicyGate/Watchdog guard declared on an agent
stopped firing the moment it was wrapped in a loop or handoff, and an
`ApprovalRequired`/`ToolCancelled` raised inside re-propagated unhandled.

`_compile_llm_agent` is the only path that attaches that middleware (proven by
test_governance_middleware). This reconciles the *claim* ("the wrapped agent is
governed the same as standalone") against reality: compiling a LoopAgent /
HandoffAgent must route each inner agent through `_compile_llm_agent` (carrying
the synthetic finish/transfer tools as extra_tools), not a bare create_agent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import HandoffAgent, LlmAgent, LoopAgent, compile_to_langgraph
from apx_agent import _compile


@pytest.fixture
def fake_ws() -> MagicMock:
    return MagicMock(name="ws")


@pytest.fixture(autouse=True)
def _stub_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_chat(endpoint: str, *, temperature: Any = None, max_tokens: Any = None) -> Any:
        m = MagicMock(name=f"chat:{endpoint}")
        m.bind_tools = MagicMock(return_value=m)
        return m

    monkeypatch.setattr(_compile, "_build_chat_databricks", _fake_chat)


@pytest.fixture
def spy_llm_compile(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every _compile_llm_agent call while still running the real one."""
    calls: list[dict[str, Any]] = []
    real = _compile._compile_llm_agent

    def _spy(agent: Any, ctx: Any, *, bake_prompt: bool = True, extra_tools: Any = None) -> Any:
        calls.append({"agent": agent, "extra_tools": extra_tools})
        return real(agent, ctx, bake_prompt=bake_prompt, extra_tools=extra_tools)

    monkeypatch.setattr(_compile, "_compile_llm_agent", _spy)
    return calls


def _noop_tool(query: str) -> str:
    """A dependency-free tool."""
    return query


@pytest.mark.unit
def test_loop_inner_agent_is_compiled_through_governed_path(fake_ws, spy_llm_compile) -> None:
    inner = LlmAgent(tools=[_noop_tool], instructions="Iterate.")
    compile_to_langgraph(LoopAgent(agent=inner, max_iterations=3), ws=fake_ws, model="any")

    inner_calls = [c for c in spy_llm_compile if c["agent"] is inner]
    assert inner_calls, "LoopAgent inner agent bypassed _compile_llm_agent — governance dropped"
    # The synthetic finish-loop tool must ride along via the governed path.
    assert inner_calls[0]["extra_tools"], "finish-loop tool not passed as extra_tools"


@pytest.mark.unit
def test_handoff_inner_agents_are_compiled_through_governed_path(fake_ws, spy_llm_compile) -> None:
    a = LlmAgent(name="alpha", tools=[_noop_tool], instructions="A.")
    b = LlmAgent(name="beta", tools=[_noop_tool], instructions="B.")
    compile_to_langgraph(
        HandoffAgent(agents={"alpha": a, "beta": b}, start="alpha"), ws=fake_ws, model="any"
    )

    compiled_agents = {id(c["agent"]) for c in spy_llm_compile}
    assert id(a) in compiled_agents and id(b) in compiled_agents, (
        "HandoffAgent sub-agents bypassed _compile_llm_agent — governance dropped"
    )
    # Each sub-agent gets its transfer tool(s) via extra_tools.
    for c in spy_llm_compile:
        if c["agent"] in (a, b):
            assert c["extra_tools"], "transfer tools not passed as extra_tools"

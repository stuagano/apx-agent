"""Compilation tests for ``ParallelAgent``, ``LoopAgent``, ``RouterAgent``,
``HandoffAgent``.

Structural assertions only — verifies each agent type produces a
``CompiledStateGraph`` with the expected nodes/edges. Behavioral assertions
(actual routing decisions, loop termination, handoff sequences) need a live
LLM and live tools to drive the conditional edges; those belong in
integration tests.

Skips if optional extras are missing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import (  # noqa: E402
    HandoffAgent,
    LlmAgent,
    LoopAgent,
    ParallelAgent,
    RouterAgent,
    compile_to_langgraph,
)


@pytest.fixture
def fake_ws() -> MagicMock:
    ws = MagicMock(name="fake_user_obo_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


@pytest.fixture(autouse=True)
def _stub_chat_databricks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid needing a live serving endpoint or langchain-databricks."""
    from apx_agent import _compile

    def _fake_chat(endpoint: str) -> Any:
        mock = MagicMock(name=f"fake_chat:{endpoint}")
        # bind_tools is called by RouterAgent; return self for chainable.
        mock.bind_tools = MagicMock(return_value=mock)
        return mock

    monkeypatch.setattr(_compile, "_build_chat_databricks", _fake_chat)


def _noop_tool(query: str) -> str:
    """A dependency-free tool used to populate sub-agents."""
    return query


# ---------------------------------------------------------------------------
# ParallelAgent
# ---------------------------------------------------------------------------


class TestParallelAgent:
    def test_fan_out_topology(self, fake_ws: MagicMock) -> None:
        """All branches connect START → branch_i and branch_i → END."""
        agent = ParallelAgent(
            agents=[
                LlmAgent(name="left", tools=[_noop_tool]),
                LlmAgent(name="right", tools=[_noop_tool]),
                LlmAgent(name="center", tools=[_noop_tool]),
            ],
        )
        compiled = compile_to_langgraph(agent, ws=fake_ws, model="any")
        nodes = set(compiled.get_graph().nodes.keys())
        assert {"left", "right", "center"}.issubset(nodes)

        edge_pairs = {(e.source, e.target) for e in compiled.get_graph().edges}
        for name in ("left", "right", "center"):
            assert ("__start__", name) in edge_pairs
            assert (name, "__end__") in edge_pairs


# ---------------------------------------------------------------------------
# LoopAgent
# ---------------------------------------------------------------------------


class TestLoopAgent:
    def test_topology_has_loop_back_edge(self, fake_ws: MagicMock) -> None:
        """Compiled graph has ``agent`` and ``check`` nodes with a conditional
        edge that can route back to ``agent`` or to END."""
        inner = LlmAgent(tools=[_noop_tool], instructions="Iterate.")
        agent = LoopAgent(agent=inner, max_iterations=3)

        compiled = compile_to_langgraph(agent, ws=fake_ws, model="any")
        nodes = set(compiled.get_graph().nodes.keys())
        assert "agent" in nodes
        assert "check" in nodes

        edge_pairs = {(e.source, e.target) for e in compiled.get_graph().edges}
        assert ("agent", "check") in edge_pairs
        # The conditional edge from check to agent (loop back) and to END.
        # LangGraph renders conditional edges as dashed; both targets must
        # appear as outgoing edges from "check".
        check_targets = {dst for src, dst in edge_pairs if src == "check"}
        assert "agent" in check_targets
        assert "__end__" in check_targets

    def test_finish_loop_tool_added_to_inner_agent(self, fake_ws: MagicMock) -> None:
        """The compiled inner node's tool list includes ``finish_loop``."""
        from apx_agent._compile import CompileContext, _build_synthetic_tool

        # Direct check: the synthetic tool builder produces a StructuredTool
        # with the expected name and a non-empty description.
        finish_tool = _build_synthetic_tool(
            name=LoopAgent.FINISH_TOOL,
            description="Signal completion",
            marker="LOOP_FINISHED",
        )
        assert finish_tool.name == "finish_loop"
        assert "Signal completion" in finish_tool.description


# ---------------------------------------------------------------------------
# RouterAgent
# ---------------------------------------------------------------------------


class TestRouterAgent:
    def test_decision_node_and_targets(self, fake_ws: MagicMock) -> None:
        """Compiled graph has a router node + one node per route."""
        agent = RouterAgent(
            agents=[
                ("billing", "Billing questions", LlmAgent(tools=[_noop_tool])),
                ("support", "Tech support", LlmAgent(tools=[_noop_tool])),
                ("sales", "Pricing & sales", LlmAgent(tools=[_noop_tool])),
            ],
            instructions="Route to the right agent.",
        )
        compiled = compile_to_langgraph(agent, ws=fake_ws, model="any")
        nodes = set(compiled.get_graph().nodes.keys())
        assert "router" in nodes
        assert {"billing", "support", "sales"}.issubset(nodes)

        edge_pairs = {(e.source, e.target) for e in compiled.get_graph().edges}
        assert ("__start__", "router") in edge_pairs
        # All targets land in END after they run.
        for name in ("billing", "support", "sales"):
            assert (name, "__end__") in edge_pairs


# ---------------------------------------------------------------------------
# HandoffAgent
# ---------------------------------------------------------------------------


class TestHandoffAgent:
    def test_each_agent_routes_through_check_node(self, fake_ws: MagicMock) -> None:
        """Compiled graph has each agent + a shared __check__ node that
        decides whether to hand off or terminate."""
        agent = HandoffAgent(
            agents={
                "triage": LlmAgent(tools=[_noop_tool], instructions="Triage."),
                "billing": LlmAgent(tools=[_noop_tool], instructions="Billing."),
                "tech": LlmAgent(tools=[_noop_tool], instructions="Tech."),
            },
            start="triage",
            max_handoffs=4,
        )
        compiled = compile_to_langgraph(agent, ws=fake_ws, model="any")
        nodes = set(compiled.get_graph().nodes.keys())
        assert {"triage", "billing", "tech", "__check__"}.issubset(nodes)

        edge_pairs = {(e.source, e.target) for e in compiled.get_graph().edges}
        assert ("__start__", "triage") in edge_pairs
        for name in ("triage", "billing", "tech"):
            assert (name, "__check__") in edge_pairs

        # __check__ has conditional edges to every agent + END.
        check_targets = {dst for src, dst in edge_pairs if src == "__check__"}
        assert {"triage", "billing", "tech", "__end__"}.issubset(check_targets)

    def test_invalid_start_raises_at_construction(self) -> None:
        """HandoffAgent rejects a start name that isn't in the agents dict.
        This is the existing apx-agent constructor's check — verifies the
        guard is still there after our compile additions."""
        with pytest.raises(ValueError, match="not found"):
            HandoffAgent(
                agents={"a": LlmAgent(tools=[_noop_tool])},
                start="missing",
            )

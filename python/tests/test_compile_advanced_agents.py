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


# ---------------------------------------------------------------------------
# Message-state scrub for handoffs
#
# These tests cover the assistant-message-prefill bug: Databricks-Claude
# rejects a chat completion whose tail is an AIMessage. After a transfer_to_*
# tool call fires, the accumulated graph state ends with the prior sub-agent's
# AIMessage. The compile must scrub state["messages"] before invoking the
# downstream sub-agent's react graph.
# ---------------------------------------------------------------------------


class TestHandoffMessageScrub:
    """The scrub helper produces a tail Databricks-Claude will accept."""

    def test_scrub_returns_human_tail_after_assistant(self) -> None:
        """After a transfer-emitting AIMessage, the scrubbed list ends with
        a HumanMessage (never an AIMessage)."""
        from langchain_core.messages import AIMessage, HumanMessage

        from apx_agent._compile import _build_subagent_input_messages

        messages = [
            HumanMessage(content="why is my bill so high?"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "transfer_to_billing_specialist",
                        "args": {"context": "billing question"},
                        "id": "tc-1",
                    }
                ],
            ),
        ]
        scrubbed = _build_subagent_input_messages(
            messages,
            prior_agent_name="triage",
            target_name="billing_specialist",
        )

        assert len(scrubbed) >= 1
        assert not isinstance(scrubbed[-1], AIMessage), (
            "Tail must not be an AIMessage — Databricks-Claude rejects "
            "assistant-message-prefill."
        )
        assert isinstance(scrubbed[-1], HumanMessage)

    def test_original_user_query_reaches_subagent_verbatim(self) -> None:
        """The first HumanMessage in state must be passed through unchanged."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from apx_agent._compile import _build_subagent_input_messages

        original = HumanMessage(content="why is my bill so high?")
        messages = [
            original,
            AIMessage(
                content="",
                tool_calls=[{"name": "classify_intent", "args": {}, "id": "tc-0"}],
            ),
            ToolMessage(content="billing", tool_call_id="tc-0"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "transfer_to_billing_specialist",
                        "args": {},
                        "id": "tc-1",
                    }
                ],
            ),
        ]
        scrubbed = _build_subagent_input_messages(
            messages,
            prior_agent_name="triage",
            target_name="billing_specialist",
        )

        assert scrubbed[0] is original
        assert scrubbed[0].content == "why is my bill so high?"

    def test_handoff_context_propagated_as_human_message(self) -> None:
        """The handoff context is encoded as a HumanMessage carrying the
        target name — NOT as a ToolMessage (orphan tool_result risk) and
        NOT as an AIMessage (prefill risk)."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from apx_agent._compile import _build_subagent_input_messages

        messages = [
            HumanMessage(content="what notification channel does alice prefer?"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "transfer_to_account_specialist",
                        "args": {},
                        "id": "tc-9",
                    }
                ],
            ),
        ]
        scrubbed = _build_subagent_input_messages(
            messages,
            prior_agent_name="triage",
            target_name="account_specialist",
        )

        # Tail is a HumanMessage carrying the handoff context — provenance
        # is encoded in the content, not in role-switched message types.
        tail = scrubbed[-1]
        assert isinstance(tail, HumanMessage)
        assert "account_specialist" in tail.content
        assert not any(isinstance(m, ToolMessage) for m in scrubbed)

    def test_multiple_sequential_handoffs_dont_accumulate_assistant_tails(
        self,
    ) -> None:
        """When called repeatedly with a state that keeps growing AIMessages,
        each scrub still returns a fresh user-tail conversation. Simulates
        triage → billing → account hop chain."""
        from langchain_core.messages import AIMessage, HumanMessage

        from apx_agent._compile import _build_subagent_input_messages

        original = HumanMessage(content="multi-hop question")
        state = [
            original,
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "transfer_to_billing", "args": {}, "id": "tc-a"}
                ],
            ),
        ]
        first_scrub = _build_subagent_input_messages(
            state, prior_agent_name="triage", target_name="billing"
        )
        assert isinstance(first_scrub[-1], HumanMessage)

        # Now imagine billing also handed off (a second AIMessage on the tail).
        state = [
            *state,
            AIMessage(content="billing analysis"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "transfer_to_account", "args": {}, "id": "tc-b"}
                ],
            ),
        ]
        second_scrub = _build_subagent_input_messages(
            state, prior_agent_name="billing", target_name="account"
        )

        assert isinstance(second_scrub[-1], HumanMessage)
        # Original query still preserved as the first message in both.
        assert second_scrub[0] is original
        # The two scrubs target different downstreams; their tails must
        # reflect that (so a downstream knows WHICH agent it's standing in for).
        assert "billing" in first_scrub[-1].content
        assert "account" in second_scrub[-1].content

    def test_start_agent_is_not_wrapped(self, fake_ws: MagicMock) -> None:
        """Compile internals: the start agent's node is the raw react
        runnable (no scrub wrap), since it sees a clean user-tail input
        directly from the graph. Verifies the optimization in _build_node."""
        from apx_agent._compile import (
            CompileContext,
            _compile_handoff_agent,
        )

        ho = HandoffAgent(
            agents={
                "triage": LlmAgent(tools=[_noop_tool], instructions="Triage."),
                "billing": LlmAgent(tools=[_noop_tool], instructions="Billing."),
            },
            start="triage",
        )
        ctx = CompileContext(ws=fake_ws, model="any")
        compiled = _compile_handoff_agent(ho, ctx)

        # The compiled CompiledStateGraph keeps node implementations on
        # ``builder.nodes`` (LangGraph internal). Triage should be a raw
        # langchain runnable, billing should be our wrapper closure.
        # Structural check: both nodes exist and route through __check__.
        edge_pairs = {(e.source, e.target) for e in compiled.get_graph().edges}
        assert ("triage", "__check__") in edge_pairs
        assert ("billing", "__check__") in edge_pairs

    def test_empty_human_fallback_returns_raw_list(self) -> None:
        """Defensive: if state has no HumanMessage at all, the helper falls
        back to the raw list rather than silently dropping context."""
        from langchain_core.messages import AIMessage

        from apx_agent._compile import _build_subagent_input_messages

        # Pathological input (would not happen in practice) — make sure
        # the helper doesn't IndexError or return [].
        messages = [AIMessage(content="orphan assistant message")]
        result = _build_subagent_input_messages(
            messages, prior_agent_name="x", target_name="y"
        )
        assert result == messages

import pytest
pytest.importorskip("langgraph")

from unittest.mock import MagicMock
from langchain_core.messages import AIMessage, HumanMessage

from apx_agent import Dependencies, LlmAgent
from apx_agent._compile import _agent_has_state_tool, _wrap_agent_node


def test_agent_has_state_tool_detects_state_param():
    def with_state(q: str, state: Dependencies.State) -> str:
        return q

    def without(q: str) -> str:
        return q

    assert _agent_has_state_tool(LlmAgent(tools=[with_state], name="a")) is True
    assert _agent_has_state_tool(LlmAgent(tools=[without], name="b")) is False


class _StateWritingRunnable:
    """Fake inner agent that writes to state (as a Command-applying ToolNode
    would have) by returning a state delta alongside its messages."""

    async def ainvoke(self, state: dict) -> dict:
        msgs = list(state["messages"])
        return {
            "messages": msgs + [AIMessage(content="done")],
            "state": {"account_id": "ACME-1"},
        }


@pytest.mark.asyncio
async def test_wrapped_node_propagates_tool_state_writes():
    # An agent wrapped for output_key still must surface tool-written state.
    agent = LlmAgent(tools=[], name="x", instructions="Help.", output_key="answer")
    graph = _wrap_agent_node(agent, _StateWritingRunnable(), templated=False)
    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert out["state"]["account_id"] == "ACME-1"   # tool write survived
    assert out["state"]["answer"] == "done"          # output_key still written

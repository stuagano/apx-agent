import pytest
pytest.importorskip("langgraph")

from typing import Any
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


# ---------------------------------------------------------------------------
# _compile_llm_agent: state_schema wiring
#
# These tests guard that _compile_llm_agent passes state_schema=state_schema()
# to create_agent when the agent has a Dependencies.State tool, and omits it
# when there is none. We intercept at langchain.agents.create_agent (the
# module-level attribute that the function's local `from langchain.agents import
# create_agent` resolves at call time) so no LLM or live Databricks call is made.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_build_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _build_chat_databricks so no live serving endpoint is needed."""
    from apx_agent import _compile

    def _fake_chat(endpoint: str, *, temperature: Any = None, max_tokens: Any = None) -> Any:
        return MagicMock(name=f"fake_llm:{endpoint}")

    monkeypatch.setattr(_compile, "_build_chat_databricks", _fake_chat)


def test_compile_llm_agent_passes_state_schema_when_state_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compile_llm_agent must pass state_schema= to create_agent for state tools.

    Monkeypatching langchain.agents.create_agent intercepts the local import
    inside _compile_llm_agent (``from langchain.agents import create_agent``
    re-resolves from the module each call), so the patch is reliable.
    """
    import langchain.agents as _la

    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock()  # must support .with_config() for post-create wiring

    monkeypatch.setattr(_la, "create_agent", fake_create_agent)

    def with_state(q: str, state: Dependencies.State) -> str:
        """Tool that reads agent state."""
        return q

    from apx_agent._compile import CompileContext, _compile_llm_agent, state_schema

    ctx = CompileContext(
        service_ws=None,
        user_ws=None,
        model="databricks-claude-sonnet-4-6",
        headers=None,
    )
    _compile_llm_agent(LlmAgent(tools=[with_state], name="a", instructions="Help."), ctx)

    assert "state_schema" in captured, (
        "_compile_llm_agent did not pass state_schema= to create_agent for a state-tool agent"
    )
    # state_schema() caches; verify it's the same cached ApxState class.
    expected = state_schema()
    assert captured["state_schema"] is expected, (
        f"state_schema kwarg was {captured['state_schema']!r}, expected {expected!r}"
    )


def test_compile_llm_agent_omits_state_schema_without_state_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compile_llm_agent must NOT pass state_schema= for plain (non-state) agents."""
    import langchain.agents as _la

    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(_la, "create_agent", fake_create_agent)

    def plain(q: str) -> str:
        """A plain tool with no state dependency."""
        return q

    from apx_agent._compile import CompileContext, _compile_llm_agent

    ctx = CompileContext(
        service_ws=None,
        user_ws=None,
        model="databricks-claude-sonnet-4-6",
        headers=None,
    )
    _compile_llm_agent(LlmAgent(tools=[plain], name="b", instructions="Help."), ctx)

    assert "state_schema" not in captured, (
        "_compile_llm_agent unexpectedly passed state_schema= for a plain (non-state) agent"
    )

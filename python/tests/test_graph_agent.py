"""GraphAgent (#1037): host a hand-written LangGraph graph with apx-governed tools.

The graphs here are real LangGraph graphs with no LLM: a ``plan`` node emits a
fixed tool call and a stock ``ToolNode`` runs it, the shape most hand-written
agents use.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

from apx_agent import Dependencies, GraphAgent, compile_to_langgraph  # noqa: E402
from apx_agent._resources import _iter_tool_fns  # noqa: E402


@pytest.fixture
def fake_ws() -> MagicMock:
    ws = MagicMock(name="fake_user_scoped_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _calling(tool_name: str, args: dict[str, Any]):
    """A build() for a two-node graph: plan emits one tool call, ToolNode runs it."""
    seen: dict[str, Any] = {}

    def build(tools: list[Any], checkpointer: Any) -> Any:
        seen["tools"], seen["checkpointer"] = tools, checkpointer

        def plan(_state: MessagesState) -> dict[str, Any]:
            call = {"name": tool_name, "args": args, "id": "call-1"}
            return {"messages": [AIMessage(content="", tool_calls=[call])]}

        graph = StateGraph(MessagesState)
        graph.add_node("plan", plan)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "tools")
        graph.add_edge("tools", END)
        return graph.compile(checkpointer=checkpointer)

    return build, seen


def _lookup_tool(calls: list[Any]):
    def lookup(key: str, ws: Dependencies.Workspace) -> str:
        """Look up a value for a key."""
        calls.append((key, ws))
        return f"value-for-{key}"

    return lookup


def test_build_receives_governed_tools_and_the_graph_runs_them(fake_ws: MagicMock) -> None:
    calls: list[Any] = []
    build, seen = _calling("lookup", {"key": "acme"})
    graph = compile_to_langgraph(GraphAgent(build=build, tools=[_lookup_tool(calls)]), ws=fake_ws, model="m")

    out = graph.invoke({"messages": [HumanMessage(content="go")]})

    [tool] = seen["tools"]
    assert tool.name == "lookup"
    assert set(tool.args) == {"key"}  # the injected dependency is hidden from the model
    assert calls == [("acme", fake_ws)]  # resolved against this request's workspace client
    assert isinstance(out["messages"][-1], ToolMessage)
    assert out["messages"][-1].content == "value-for-acme"


def test_before_tool_fires_for_tool_calls_inside_the_users_graph(fake_ws: MagicMock) -> None:
    hooks: list[Any] = []
    build, _ = _calling("lookup", {"key": "acme"})
    agent = GraphAgent(build=build, tools=[_lookup_tool([])],
                       before_tool=lambda name, args: hooks.append((name, args)))

    compile_to_langgraph(agent, ws=fake_ws, model="m").invoke({"messages": [HumanMessage(content="go")]})

    assert hooks == [("lookup", {"key": "acme"})]


def test_a_raising_before_tool_blocks_the_tool(fake_ws: MagicMock) -> None:
    calls: list[Any] = []

    def deny(name: str, args: dict[str, Any]) -> None:
        raise PermissionError(f"{name} is not allowed")

    build, _ = _calling("lookup", {"key": "acme"})
    graph = compile_to_langgraph(GraphAgent(build=build, tools=[_lookup_tool(calls)], before_tool=deny),
                                 ws=fake_ws, model="m")

    with pytest.raises(PermissionError, match="lookup is not allowed"):
        graph.invoke({"messages": [HumanMessage(content="go")]})
    assert calls == []


def test_build_receives_the_compile_checkpointer(fake_ws: MagicMock) -> None:
    saver = InMemorySaver()
    build, seen = _calling("lookup", {"key": "acme"})

    compile_to_langgraph(GraphAgent(build=build, tools=[_lookup_tool([])]), ws=fake_ws, model="m",
                         checkpointer=saver)

    assert seen["checkpointer"] is saver


def test_build_must_be_callable() -> None:
    with pytest.raises(TypeError, match="build must be callable"):
        GraphAgent(build="not a function")  # type: ignore[arg-type]


def test_build_must_return_a_compiled_graph(fake_ws: MagicMock) -> None:
    agent = GraphAgent(build=lambda tools, checkpointer: 42, tools=[])

    with pytest.raises(TypeError, match="must return a compiled LangGraph graph"):
        compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_graph_agent_tools_are_visible_to_tool_discovery() -> None:
    lookup = _lookup_tool([])
    build, _ = _calling("lookup", {"key": "acme"})

    assert list(_iter_tool_fns(GraphAgent(build=build, tools=[lookup]))) == [lookup]

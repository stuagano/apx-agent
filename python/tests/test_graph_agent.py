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


def test_a_blocked_tool_becomes_an_error_result_inside_the_users_tool_node(fake_ws: MagicMock) -> None:
    """Same containment an LlmAgent gets: the turn continues and the model can read why."""
    calls: list[Any] = []

    def deny(name: str, args: dict[str, Any]) -> None:
        raise PermissionError(f"{name} is not allowed")

    build, _ = _calling("lookup", {"key": "acme"})
    graph = compile_to_langgraph(GraphAgent(build=build, tools=[_lookup_tool(calls)], before_tool=deny),
                                 ws=fake_ws, model="m")

    result = graph.invoke({"messages": [HumanMessage(content="go")]})["messages"][-1]

    assert calls == []
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.content == "Error: lookup is not allowed"


def test_a_tool_error_from_the_tool_body_is_contained(fake_ws: MagicMock) -> None:
    from apx_agent import ToolError

    def lookup(key: str) -> str:
        """Look up a value for a key."""
        raise ToolError(f"no table for {key}")

    build, _ = _calling("lookup", {"key": "acme"})
    graph = compile_to_langgraph(GraphAgent(build=build, tools=[lookup]), ws=fake_ws, model="m")

    result = graph.invoke({"messages": [HumanMessage(content="go")]})["messages"][-1]

    assert (result.status, result.content) == ("error", "Error: no table for acme")


def test_a_genuine_bug_in_a_tool_still_fails_loud(fake_ws: MagicMock) -> None:
    def lookup(key: str) -> str:
        """Look up a value for a key."""
        raise TypeError("bug")

    build, _ = _calling("lookup", {"key": "acme"})
    graph = compile_to_langgraph(GraphAgent(build=build, tools=[lookup]), ws=fake_ws, model="m")

    with pytest.raises(TypeError, match="bug"):
        graph.invoke({"messages": [HumanMessage(content="go")]})


def test_after_tool_sees_the_tools_output(fake_ws: MagicMock) -> None:
    seen: list[Any] = []
    build, _ = _calling("lookup", {"key": "acme"})
    agent = GraphAgent(build=build, tools=[_lookup_tool([])],
                       after_tool=lambda name, args, output: seen.append((name, args, output)))

    compile_to_langgraph(agent, ws=fake_ws, model="m").invoke({"messages": [HumanMessage(content="go")]})

    assert seen == [("lookup", {"key": "acme"}, "value-for-acme")]


def test_an_async_tool_is_hooked_and_contained(fake_ws: MagicMock) -> None:
    import asyncio

    hooks: list[str] = []

    async def lookup(key: str) -> str:
        """Look up a value for a key."""
        raise PermissionError("blocked")

    build, _ = _calling("lookup", {"key": "acme"})
    agent = GraphAgent(build=build, tools=[lookup], before_tool=lambda name, args: hooks.append(name))
    graph = compile_to_langgraph(agent, ws=fake_ws, model="m")

    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}))["messages"][-1]

    assert hooks == ["lookup"]
    assert (result.status, result.content) == ("error", "Error: blocked")


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


# --- Fail closed on tools that bypass apx (#1037, part C) -----------------------------------


def _raw_tool(name: str) -> Any:
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(lambda key: key, name=name, description="raw, not governed")


def _graph_with(tool_node_tools: list[Any], *, nested: bool = False) -> Any:
    def build(tools: list[Any], checkpointer: Any) -> Any:
        inner = StateGraph(MessagesState)
        inner.add_node("tools", ToolNode([*tools, *tool_node_tools]))
        inner.add_edge(START, "tools")
        inner.add_edge("tools", END)
        if not nested:
            return inner.compile(checkpointer=checkpointer)
        outer = StateGraph(MessagesState)
        outer.add_node("sub", inner.compile())
        outer.add_edge(START, "sub")
        outer.add_edge("sub", END)
        return outer.compile(checkpointer=checkpointer)

    return build


def test_a_tool_bound_outside_tools_is_refused(fake_ws: MagicMock) -> None:
    agent = GraphAgent(build=_graph_with([_raw_tool("delete_everything")]), tools=[_lookup_tool([])])

    with pytest.raises(ValueError, match="'delete_everything'.*not passed through tools="):
        compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_a_tool_hidden_in_a_nested_subgraph_is_refused(fake_ws: MagicMock) -> None:
    agent = GraphAgent(build=_graph_with([_raw_tool("sneaky")], nested=True), tools=[])

    with pytest.raises(ValueError, match="'sneaky'"):
        compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_a_raw_tool_reusing_a_governed_name_is_still_refused(fake_ws: MagicMock) -> None:
    """Identity, not name: a raw `lookup` next to the governed one bypasses apx."""

    def build(tools: list[Any], checkpointer: Any) -> Any:
        graph = StateGraph(MessagesState)
        graph.add_node("tools", ToolNode([_raw_tool("lookup")]))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        return graph.compile()

    with pytest.raises(ValueError, match="'lookup'"):
        compile_to_langgraph(GraphAgent(build=build, tools=[_lookup_tool([])]), ws=fake_ws, model="m")


def test_an_explicit_ungoverned_opt_out_is_allowed(fake_ws: MagicMock) -> None:
    agent = GraphAgent(build=_graph_with([_raw_tool("local_math")]), tools=[_lookup_tool([])],
                       ungoverned=["local_math"])

    compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_governed_tools_in_a_create_agent_graph_are_accepted(fake_ws: MagicMock) -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    agent = GraphAgent(build=lambda tools, checkpointer: create_agent(model=Model(responses=[]), tools=tools),
                       tools=[_lookup_tool([])])

    compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_an_opaque_runnable_is_refused_because_it_cannot_be_inspected(fake_ws: MagicMock) -> None:
    from langchain_core.runnables import RunnableLambda

    def build(tools: list[Any], checkpointer: Any) -> Any:
        graph = _calling("lookup", {})[0](tools, checkpointer)
        return RunnableLambda(graph.invoke)  # hides the graph's nodes

    agent = GraphAgent(build=build, tools=[_lookup_tool([])])

    with pytest.raises(TypeError, match="must return a compiled LangGraph graph"):
        compile_to_langgraph(agent, ws=fake_ws, model="m")


def test_ungoverned_must_be_a_list_of_tool_names() -> None:
    with pytest.raises(TypeError, match="ungoverned= must be a list of tool names"):
        GraphAgent(build=_graph_with([]), ungoverned="local_math")  # type: ignore[arg-type]


def test_a_graph_agent_with_no_declared_memory_logs_no_memory_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The 'memory=/example= declared' warning is only for a root that declared them."""
    import logging

    from apx_agent._memory_wiring import attach_declared_memory
    from apx_agent._models import AgentConfig

    agent = GraphAgent(build=_calling("lookup", {})[0], tools=[])
    with caplog.at_level(logging.WARNING):
        attach_declared_memory(agent, AgentConfig(name="rca"), None)

    assert "memory=/example=" not in caplog.text


def _async_graph(checkpointer: Any = None) -> Any:
    # The common LangGraph template style: an `async def` node that reads the runtime config.
    from langchain_core.runnables import RunnableConfig

    async def reply(state: MessagesState, config: RunnableConfig) -> dict[str, Any]:
        return {"messages": [AIMessage(content=f"async ok {config['configurable'].get('who', '?')}")]}

    inner = StateGraph(MessagesState)
    inner.add_node("reply", reply)
    inner.add_edge(START, "reply")
    inner.add_edge("reply", END)
    outer = StateGraph(MessagesState)
    outer.add_node("sub", inner.compile())  # nested: the bridge must reach subgraph nodes too
    outer.add_edge(START, "sub")
    outer.add_edge("sub", END)
    return outer.compile(checkpointer=checkpointer)


def test_async_nodes_run_on_the_sync_turn_path(fake_ws: MagicMock) -> None:
    """#1162: apx drives turns with sync graph.invoke / graph.stream, so async-only nodes get a bridge."""
    graph = compile_to_langgraph(GraphAgent(build=lambda tools, checkpointer: _async_graph(checkpointer)),
                                 ws=fake_ws, model="m")
    config = {"configurable": {"who": "alice"}}

    result = graph.invoke({"messages": [HumanMessage(content="hi")]}, config)
    streamed = [v for mode, v in graph.stream({"messages": [HumanMessage(content="hi")]}, config,
                                               stream_mode=["values"]) if mode == "values"]

    assert result["messages"][-1].content == "async ok alice"
    assert streamed[-1]["messages"][-1].content == "async ok alice"


async def test_async_nodes_still_run_natively_on_the_async_path(fake_ws: MagicMock) -> None:
    graph = compile_to_langgraph(GraphAgent(build=lambda tools, checkpointer: _async_graph(checkpointer)),
                                 ws=fake_ws, model="m")

    result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {"who": "bob"}})

    assert result["messages"][-1].content == "async ok bob"


async def test_context_factory_reaches_the_graph_on_the_executor_path(fake_ws: MagicMock) -> None:
    """#1163: GraphAgent.run()/dev turns go through LangGraphExecutor's astream/ainvoke, not run_graph_turn."""
    from dataclasses import dataclass

    from langgraph.runtime import Runtime

    from apx_agent._executor import TurnComplete
    from apx_agent._langgraph_executor import LangGraphExecutor

    @dataclass
    class Ctx:
        greeting: str = "unset"

    def reply(state: MessagesState, runtime: Runtime[Ctx]) -> dict[str, Any]:
        return {"messages": [AIMessage(content="no context" if runtime.context is None else runtime.context.greeting)]}

    def build(tools: list[Any], checkpointer: Any) -> Any:
        graph = StateGraph(MessagesState, context_schema=Ctx)
        graph.add_node("reply", reply)
        graph.add_edge(START, "reply")
        graph.add_edge("reply", END)
        return graph.compile(checkpointer=checkpointer)

    agent = GraphAgent(build=build, context=lambda: Ctx(greeting="from-factory"))
    events = [e async for e in LangGraphExecutor(agent, ws=fake_ws, model="m").run_turn(
        [{"role": "user", "content": "hi"}], [], "", None)]

    assert [e.response for e in events if isinstance(e, TurnComplete)] == ["from-factory"]

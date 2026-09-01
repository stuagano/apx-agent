# python/tests/test_state_tool_injection.py
import pytest
pytest.importorskip("langgraph")

from typing import Any
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from apx_agent import Dependencies
from apx_agent._compile import CompileContext, _make_langchain_tool, state_schema


def _ctx() -> CompileContext:
    # Tools here use neither client nor request headers.
    return CompileContext(
        service_ws=None,
        user_ws=None,
        model="databricks-claude-sonnet-4-6",
        headers=None,
    )


def _tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _make_graph(tool: BaseTool) -> Any:
    """Compile a minimal StateGraph that drives the tool through a real ToolNode.

    Uses state_schema() so the 'state' channel exists with _merge_state reducer,
    which applies Command(update={"state": delta}) correctly.
    """
    g = StateGraph(state_schema())
    g.add_node("tools", ToolNode([tool]))
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    return g.compile()


@pytest.mark.asyncio
async def test_state_write_becomes_command_update():
    def resolve(name: str, state: Dependencies.State) -> str:
        state["account_id"] = f"ACME-{name}"
        return f"resolved {name}"

    tool = _make_langchain_tool(resolve, _ctx())
    graph = _make_graph(tool)
    out = await graph.ainvoke(
        {"messages": [_tool_call("resolve", {"name": "x"})], "state": {}}
    )
    # state delta applied
    assert out["state"]["account_id"] == "ACME-x"
    # tool message still emitted with the plain return text
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and "resolved x" in tms[0].content


@pytest.mark.asyncio
async def test_state_read_only_returns_plain_value_no_state_update():
    def lookup(q: str, state: Dependencies.State) -> str:
        acct = state.get("account_id")
        return f"{q}:{acct}"

    tool = _make_langchain_tool(lookup, _ctx())
    graph = _make_graph(tool)
    out = await graph.ainvoke(
        {"messages": [_tool_call("lookup", {"q": "hi"})], "state": {"account_id": "A1"}}
    )
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and tms[0].content == "hi:A1"
    # no writes → state is unchanged from input (no new keys were written)
    assert out.get("state") == {"account_id": "A1"}


def test_state_param_excluded_from_tool_schema():
    def resolve(name: str, state: Dependencies.State) -> str:
        return name

    tool = _make_langchain_tool(resolve, _ctx())
    assert set(tool.args.keys()) == {"name"}  # state + injected params hidden


def test_async_stateful_tool_via_sync_graph_invoke():
    """Async stateful tools must work on the sync graph.invoke() path.

    This exercises the _sync_bridge added to the async stateful branch, which
    bridges the coroutine so Apps /invocations and ChatAgent (sync) paths work.
    """
    import asyncio

    async def async_resolve(name: str, state: Dependencies.State) -> str:
        # Simulate async work, then write to state.
        await asyncio.sleep(0)
        state["resolved"] = f"ASYNC-{name}"
        return f"done:{name}"

    tool = _make_langchain_tool(async_resolve, _ctx())
    graph = _make_graph(tool)
    # Invoke SYNCHRONOUSLY — this is the path that previously had no sync bridge.
    out = graph.invoke(
        {"messages": [_tool_call("async_resolve", {"name": "z"})], "state": {}}
    )
    # State write landed.
    assert out["state"]["resolved"] == "ASYNC-z"
    # Tool message still emitted.
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and "done:z" in tms[0].content

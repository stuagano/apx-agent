"""ParallelAgent branch context isolation on the compiled StateGraph (#769).

Each ``ParallelAgent`` branch must receive only the triggering user message
plus a single merged system message — never the full shared history. The fix
lives in ``_compile_parallel_agent`` (the served/compiled path), so these tests
drive the *compiled* StateGraph, never ``ParallelAgent.run`` (AC-6).

The branch *leaf* compiler (``_compile_any``) is stubbed with a recording
compiled subgraph so we can capture exactly what each branch received; the
``ParallelAgent`` fan-out graph itself is the real thing under test.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
)

from apx_agent import ParallelAgent, compile_to_langgraph  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._agents import LlmAgent  # noqa: E402
from apx_agent._compile import (  # noqa: E402
    _isolate_parallel_branch_input,
    state_schema,
)


_NO_INSTRUCTIONS = ""


def _recording_compile_any(sink: dict[str, list[Any]]):
    """Return a fake ``_compile_any`` producing a real compiled recording graph."""
    from langgraph.graph import END, START, StateGraph

    def _factory(sub: Any, ctx: Any) -> Any:
        if isinstance(sub, ParallelAgent):
            # Real fan-out graph under test; only leaf branches are recorded.
            return _compile._compile_parallel_agent(sub, ctx)
        name = sub._name

        def _node(state: dict) -> dict[str, Any]:
            sink[name] = list(state["messages"])
            return {"messages": [AIMessage(content=f"ran-{name}")]}

        graph = StateGraph(state_schema())
        graph.add_node("rec", _node)
        graph.add_edge(START, "rec")
        graph.add_edge("rec", END)
        return graph.compile()

    return _factory


def _compiled_parallel(
    monkeypatch: pytest.MonkeyPatch,
    sink: dict[str, list[Any]],
    *,
    instructions: str = _NO_INSTRUCTIONS,
    branches: tuple[str, ...] = ("left", "right"),
) -> Any:
    monkeypatch.setattr(_compile, "_compile_any", _recording_compile_any(sink))
    agent = ParallelAgent(
        agents=[LlmAgent(name=n) for n in branches],
        instructions=instructions,
    )
    return compile_to_langgraph(agent, ws=None, model="any")


def _roles(messages: list[Any]) -> list[str]:
    out = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append("system")
        elif isinstance(m, HumanMessage):
            out.append("human")
        else:
            out.append(type(m).__name__)
    return out


def test_ac1_branch_gets_system_and_last_user_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink)
    compiled.invoke(
        {
            "messages": [
                SystemMessage(content="S"),
                HumanMessage(content="first"),
                AIMessage(content="a"),
                HumanMessage(content="last"),
            ]
        }
    )
    for name in ("left", "right"):
        got = sink[name]
        assert _roles(got) == ["system", "human"]
        assert got[0].content == "S"
        assert got[1].content == "last"


def test_ac2_instructions_and_system_merge_single(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink, instructions="X")
    compiled.invoke(
        {"messages": [SystemMessage(content="S"), HumanMessage(content="q")]}
    )
    for name in ("left", "right"):
        got = sink[name]
        assert _roles(got) == ["system", "human"]  # exactly one system message
        assert got[0].content == "X\nS"
        assert got[1].content == "q"


def test_ac3_instructions_only(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink, instructions="X")
    compiled.invoke({"messages": [HumanMessage(content="q")]})
    for name in ("left", "right"):
        got = sink[name]
        assert _roles(got) == ["system", "human"]
        assert got[0].content == "X"
        assert got[1].content == "q"


def test_ac4_instruction_seeded_no_user(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink, instructions="X")
    result = compiled.invoke({"messages": [SystemMessage(content="S")]})
    for name in ("left", "right"):
        got = sink[name]
        assert _roles(got) == ["system"]
        assert got[0].content == "X\nS"
    # Not short-circuited to empty: both branches produced output.
    contents = {m.content for m in result["messages"] if isinstance(m, AIMessage)}
    assert {"ran-left", "ran-right"} <= contents


def test_ac5_no_system_no_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink)
    compiled.invoke({"messages": [HumanMessage(content="q")]})
    for name in ("left", "right"):
        got = sink[name]
        assert _roles(got) == ["human"]  # no injected empty system message
        assert got[0].content == "q"


def test_ac6_via_compiled_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolation is exercised through compile_to_langgraph + graph.invoke,
    never ParallelAgent.run."""
    sink: dict[str, list[Any]] = {}
    compiled = _compiled_parallel(monkeypatch, sink)
    assert hasattr(compiled, "invoke")  # it's a compiled StateGraph
    result = compiled.invoke(
        {"messages": [HumanMessage(content="only"), AIMessage(content="a")]}
    )
    # Fan-in merged both branch outputs (add_messages preserved).
    ai = [m.content for m in result["messages"] if isinstance(m, AIMessage)]
    assert "ran-left" in ai and "ran-right" in ai
    # Each branch saw only the last user turn.
    for name in ("left", "right"):
        assert _roles(sink[name]) == ["human"]


def test_ac7_isolate_helper_unit() -> None:
    # merge: instructions first, then incoming system(s), one message
    out = _isolate_parallel_branch_input(
        [SystemMessage(content="S1"), SystemMessage(content="S2"),
         HumanMessage(content="u1"), AIMessage(content="a"),
         HumanMessage(content="u2")],
        "X",
    )
    assert _roles(out) == ["system", "human"]
    assert out[0].content == "X\nS1\nS2"
    assert out[1].content == "u2"  # last user selected

    # no instructions, no system, no user → empty
    assert _isolate_parallel_branch_input([AIMessage(content="a")], "") == []

    # instructions only, no user → [system]
    only_sys = _isolate_parallel_branch_input([], "X")
    assert _roles(only_sys) == ["system"] and only_sys[0].content == "X"


def test_ac8_suite_regression_marker() -> None:
    """Sentinel: the isolation change imports cleanly and the module is intact.
    Full-suite regression is validated by running `make check` (NFR-2/AC-8)."""
    assert callable(_isolate_parallel_branch_input)
    assert callable(_compile._isolated_branch_node)

from __future__ import annotations

from apx_agent import Agent, SequentialAgent, platform_tools
from apx_agent._platform_tools import PlatformToolRef, platform_tool_leaves


def _noop(q: str) -> str:
    """Echo."""
    return q


def test_platform_tools_splats_into_tools_and_stays_out_of_callables() -> None:
    agent = Agent(name="a", tools=[_noop, *platform_tools("genie-pricing")])
    assert agent._tool_fns == [_noop]
    assert agent._platform_tool_refs == [PlatformToolRef(frozenset({"genie-pricing"}))]
    assert len(agent._analyzed) == 1


def test_no_ids_means_all() -> None:
    [ref] = platform_tools()
    assert ref.ids == frozenset()


def test_leaves_found_in_composites_including_shared_ids() -> None:
    a = Agent(name="a", tools=[*platform_tools("g")])
    b = Agent(name="b", tools=[*platform_tools("g")])
    leaves = platform_tool_leaves(SequentialAgent([a, b], name="s"))
    assert [(leaf._name, ref.ids) for leaf, ref in leaves] == [
        ("a", frozenset({"g"})), ("b", frozenset({"g"})),
    ]

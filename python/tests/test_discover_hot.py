"""Live-leaf resolution for instruction hot-apply."""

from __future__ import annotations

from apx_agent import Agent, HandoffAgent
from apx_agent._discover_hot import resolve_live_leaf


def test_resolve_live_leaf_root_and_handoff():
    leaf = Agent(tools=[], instructions="billing")
    root = HandoffAgent(agents={"billing": leaf})
    assert resolve_live_leaf(leaf, "agent") is leaf
    assert resolve_live_leaf(root, "billing") is leaf
    assert resolve_live_leaf(root, "missing") is None

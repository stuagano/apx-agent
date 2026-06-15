"""Claim-vs-reality check on the memory write tool (via the vendored ctk kit).

The agent-facing ``remember`` tool returns a success string the LLM trusts —
``"Saved memory <id>."``. The existing ``test_memory_tools.py`` recall tests seed
the store *directly* with ``store.add(...)``, so they never exercise the tool's
own claim. This asserts the claim against reality: after ``remember`` reports it
saved, ``recall`` must actually return the content through the same tool
interface. A ``remember`` that returned the success string without persisting
would slip past a string-only assertion but fails here.

Uses ``ctk`` (python/.ctk, on the path via ``pythonpath`` in pyproject).
"""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent import InMemoryMemoryStore, make_memory_tools
from ctk import expect


def _find_tool(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.__name__ == name:
            return t
    raise AssertionError(f"tool not found: {name} (have: {[t.__name__ for t in tools]})")


@pytest.mark.unit
def test_remember_tool_claim_matches_reality() -> None:
    store = InMemoryMemoryStore()
    tools = make_memory_tools(store=store, default_principal_id="alice")
    remember = _find_tool(tools, "remember")
    recall = _find_tool(tools, "recall")

    # The write tool CLAIMS success...
    claim = remember(content="the deploy target is apps")
    expect(claim).contains("Saved memory")

    # ...claim-vs-reality: the memory must actually be recallable through the
    # tool, not just reported as saved.
    out = recall(query="deploy target")
    expect(out).contains("apps")

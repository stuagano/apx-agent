"""Reality check (Ctk): ``[tool.apx.agent] vector_search_index`` is wired, not dead.

Before this change the config field was declared, pydantic-validated, and read by
nothing — a "declared, not wired" gap. ``finalize_agent`` now mints a
``vector_search_tool`` for the declared index. These tests prove the field is
*real and functional* (read-after-write), not merely present:

  1. wired — a leaf agent that declares an index gains a ``vector_search`` tool
     in ``collect_tools()`` (the A2A card / compiled-graph surface);
  2. functional — the attached tool actually queries the index and returns rows
     (it's the real factory, not a stub);
  3. resourced — the tool carries a ``vector_search_index`` ResourceSpec so
     deploy-time grants / DAB pick it up;
  4. collision — a code-wired ``vector_search`` tool wins; the declared index is
     skipped (also makes a second finalize a no-op);
  5. composite root — a root with no ``_register_tool`` warns and does not crash.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import Agent, AgentConfig
from apx_agent._resources import get_resources, ResourceSpec
from apx_agent._wiring import finalize_agent


_INDEX = "main.search.docs_index"


def _tool_fn(agent: Any, name: str) -> Any:
    """Return the registered tool closure with the given __name__."""
    return next(fn for fn in agent._tool_fns if fn.__name__ == name)


def _fake_vs_response(rows: list[list[Any]], column_names: list[str]) -> Any:
    """Shape matching ws.vector_search_indexes.query_index (see test_platform_tools)."""
    return SimpleNamespace(
        manifest=SimpleNamespace(columns=[SimpleNamespace(name=n) for n in column_names]),
        result=SimpleNamespace(data_array=rows),
    )


def test_declared_index_becomes_a_tool() -> None:
    """1. A declared index shows up as a `vector_search` tool on the card surface."""
    agent = Agent(tools=[])
    finalize_agent(agent, AgentConfig(name="t", vector_search_index=_INDEX))
    tool_names = {t.name for t in agent.collect_tools()}
    assert "vector_search" in tool_names


def test_declared_index_carries_resource_spec() -> None:
    """3. The minted tool carries the vector_search_index ResourceSpec for grants/DAB."""
    agent = Agent(tools=[])
    finalize_agent(agent, AgentConfig(name="t", vector_search_index=_INDEX))
    assert ResourceSpec("vector_search_index", _INDEX) in get_resources(_tool_fn(agent, "vector_search"))


@pytest.mark.asyncio
async def test_declared_tool_actually_queries_the_index() -> None:
    """2. Read-after-write: the attached tool queries the declared index and returns rows."""
    agent = Agent(tools=[])
    finalize_agent(agent, AgentConfig(name="t", vector_search_index=_INDEX))

    fake_ws = MagicMock()
    fake_ws.vector_search_indexes.query_index.return_value = _fake_vs_response(
        rows=[["doc-1", "Title 1"]],
        column_names=["doc_id", "title"],
    )
    result = await _tool_fn(agent, "vector_search")(query="how do I deploy", ws=fake_ws)

    assert result == [{"doc_id": "doc-1", "title": "Title 1"}]
    assert fake_ws.vector_search_indexes.query_index.call_args.kwargs["index_name"] == _INDEX


def test_no_index_declared_wires_nothing() -> None:
    """Control: absence of the field leaves the agent tool-less (no accidental attach)."""
    agent = Agent(tools=[])
    finalize_agent(agent, AgentConfig(name="t"))
    assert "vector_search" not in {t.name for t in agent.collect_tools()}


def test_code_wired_tool_wins_on_collision(caplog: pytest.LogCaptureFixture) -> None:
    """4. A code-wired `vector_search` is kept; the declared index is skipped."""

    def vector_search(query: str) -> str:
        """Code-wired search — must survive."""
        return "code-wired"

    agent = Agent(tools=[vector_search])
    with caplog.at_level(logging.WARNING):
        finalize_agent(agent, AgentConfig(name="t", vector_search_index=_INDEX))

    vs_tools = [t for t in agent.collect_tools() if t.name == "vector_search"]
    assert len(vs_tools) == 1
    # The surviving tool is the code-wired one (plain fn, no ResourceSpec), not the
    # declared factory (which would attach a vector_search_index ResourceSpec).
    assert get_resources(_tool_fn(agent, "vector_search")) == []
    assert "already wires" in caplog.text


def test_second_finalize_is_idempotent() -> None:
    """A second finalize_agent must not double-register the tool."""
    agent = Agent(tools=[])
    cfg = AgentConfig(name="t", vector_search_index=_INDEX)
    finalize_agent(agent, cfg)
    finalize_agent(agent, cfg)
    assert [t.name for t in agent.collect_tools()].count("vector_search") == 1


def test_composite_root_warns_and_does_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    """5. A root with no _register_tool (SequentialAgent) is skipped with a warning."""
    from apx_agent._agents import SequentialAgent

    root = SequentialAgent(agents=[Agent(tools=[])])
    assert not hasattr(root, "_register_tool")
    with caplog.at_level(logging.WARNING):
        finalize_agent(root, AgentConfig(name="t", vector_search_index=_INDEX))
    assert "no _register_tool" in caplog.text

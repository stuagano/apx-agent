"""Genie interop parity with the KA path: the Genie tool grounds a structured
answer over XBRL facts, composes into a ``SequentialAgent`` stage, and can be
invoked as the calling user (OBO) against a live space.

Genie is the natural tile for *structured* SEC XBRL data (NL→SQL over fact
tables) where a Knowledge Assistant fits *unstructured* 10-K narrative. The
interop scaffold (OBO, subagent, sequential flow, live gate) is identical to the
KA path — only the tool factory differs — so AC-5 can point at a Genie space
instead of a KA endpoint with no other change.

Tiers:
* ``test_genie_grounded_contract_mocked`` — cheap, always runs. The tool yields a
  non-empty ``genie_response`` AND ``sql_results`` rows (grounded over facts).
* ``test_genie_subagent_in_flow`` — cheap. The Genie tool runs as a
  ``SequentialAgent`` stage and its output feeds the next stage.
* ``test_live_genie_grounded_obo`` — live gate. Skips unless APX_GENIE_SPACE_ID +
  APX_CAPS_PROFILE are set; queries the deployed space as the profile's user.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import genie_query_tool


def _patch_genie(monkeypatch, *, rows: list[dict], text: str, sql: str) -> None:
    """Stub genie.py's SDK helpers so genie_query_tool returns grounded data
    without a live space."""
    import apx_agent.genie as g

    monkeypatch.setattr(g, "_run_genie_query", lambda ws, space_id, q: MagicMock(name="msg"))
    monkeypatch.setattr(g, "_is_completed", lambda msg: True)
    monkeypatch.setattr(g, "_extract_rows", lambda ws, space_id, msg: rows)
    monkeypatch.setattr(g, "_extract_text", lambda msg: text)
    monkeypatch.setattr(g, "_extract_generated_sql", lambda msg: sql)


@pytest.mark.asyncio
async def test_genie_grounded_contract_mocked(monkeypatch):
    """Cheap reality check: the grounded-result contract holds — a non-empty
    narrative answer AND structured rows read back from the fact tables."""
    _patch_genie(
        monkeypatch,
        rows=[{"ticker": "AAPL", "net_revenue_usd": 383285000000}],
        text="Apple reported net revenue of $383.3B in FY2023.",
        sql="SELECT ticker, net_revenue_usd FROM xbrl.facts WHERE ticker='AAPL'",
    )
    tool = genie_query_tool("space-xbrl")
    result = await tool(question="What was Apple's FY2023 net revenue?", ws=MagicMock())

    assert "error" not in result
    assert result["genie_response"].strip(), "genie answer must be non-empty"
    assert result["sql_results"], "grounded answer must carry structured rows"
    assert result["result_count"] >= 1


@pytest.mark.asyncio
async def test_genie_error_degrades_not_raises(monkeypatch):
    """A Genie SDK failure degrades to {'error': …}, never a raise."""
    import apx_agent.genie as g

    def _boom(ws, space_id, q):
        raise RuntimeError("space not found")

    monkeypatch.setattr(g, "_run_genie_query", _boom)
    tool = genie_query_tool("space-xbrl")
    result = await tool(question="anything", ws=MagicMock())
    assert "error" in result and "space not found" in result["error"]
    assert result["question"] == "anything"


# ---------------------------------------------------------------------------
# Genie tool as a SequentialAgent stage; output feeds the next stage
# ---------------------------------------------------------------------------

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from apx_agent import Agent, SequentialAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_genie_subagent_in_flow(monkeypatch):
    """The Genie tool runs as stage 0; its grounded output is a ToolMessage the
    pipeline reasons over, and stage 1 still runs."""
    _patch_genie(
        monkeypatch,
        rows=[{"ticker": "AAPL", "net_revenue_usd": 383285000000}],
        text="Apple net revenue FY2023: $383.3B.",
        sql="SELECT net_revenue_usd FROM xbrl.facts WHERE ticker='AAPL'",
    )
    model = _ToolFake(messages=iter([
        AIMessage(content="", tool_calls=[
            {"name": "query_genie", "args": {"question": "Apple FY2023 revenue?"}, "id": "t1"},
        ]),
        AIMessage(content="stage 0: grounded answer captured"),
        AIMessage(content="stage 1 summary complete"),
    ]))
    monkeypatch.setattr(
        _compile, "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )

    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"

    stage0 = Agent(name="research", tools=[genie_query_tool("space-xbrl")],
                   instructions="Answer from the Genie space.")
    stage1 = Agent(name="summarize", instructions="Summarize.")
    graph = compile_to_langgraph(SequentialAgent([stage0, stage1]), ws=ws, model="m")

    result = graph.invoke({"messages": [HumanMessage(content="Apple FY2023 revenue?")]})

    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs, "Genie tool did not run in the flow"
    assert "383" in str(tool_msgs[0].content), "grounded rows/answer not in tool output"

    texts = [str(m.content) for m in result["messages"] if isinstance(m, AIMessage)]
    assert any("stage 1 summary complete" in t for t in texts), (
        "flow did not reach stage 1 after the Genie stage"
    )


@pytest.mark.asyncio
async def test_live_genie_grounded_obo():
    """Live gate: a real Genie space over XBRL returns a grounded answer as the
    user. Skips unless APX_GENIE_SPACE_ID + APX_CAPS_PROFILE are set."""
    space_id = os.environ.get("APX_GENIE_SPACE_ID")
    profile = os.environ.get("APX_CAPS_PROFILE")
    if not (space_id and profile):
        pytest.skip("live Genie gate: set APX_GENIE_SPACE_ID + APX_CAPS_PROFILE (space + fe-stable OBO)")

    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient(profile=profile)
    tool = genie_query_tool(space_id)
    result = await tool(question="Which company had the highest net revenue last month?", ws=ws)

    assert "error" not in result, f"live Genie query failed: {result.get('error')}"
    assert result["genie_response"].strip(), "live Genie returned an empty answer"
    assert result["sql_results"], "live Genie answer carried no structured rows"

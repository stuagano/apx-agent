"""AST helpers for Discover wire-back (sub_agents + factory tools)."""

from __future__ import annotations

import keyword

from apx_agent._ui_edit import (
    _append_sub_agent,
    _get_agent_sub_agents,
    _list_sub_agent_targets,
    _peer_env_key,
    _remove_factory_binding,
    _remove_sub_agent,
    _slug_tool_name,
    _splice_factory_tool,
)


SIMPLE = """\
from apx_agent import Agent

agent = Agent(tools=[], instructions="hi")
"""

COMPOSITION = """\
from apx_agent import Agent, HandoffAgent

billing = Agent(tools=[], instructions="billing")
agent = HandoffAgent(agents={"billing": billing})
"""


def test_list_targets_simple_eligible():
    targets = _list_sub_agent_targets(SIMPLE)
    by_name = {t["name"]: t for t in targets}
    assert by_name["agent"]["eligible"] is True
    assert by_name["agent"]["kind"] == "Agent"
    assert by_name["agent"]["sub_agents"] == []


def test_list_targets_data_agent_eligible():
    src = 'from apx_agent import DataAgent\n\nagent = DataAgent("samples", "nyctaxi", name="hw")\n'
    targets = _list_sub_agent_targets(src)
    by_name = {t["name"]: t for t in targets}
    assert by_name["agent"]["eligible"] is True
    assert by_name["agent"]["kind"] == "DataAgent"
    out, already = _append_sub_agent(src, "$APX_PEER_TRIAGE_URL", target="agent")
    assert already is False
    assert "sub_agents=" in out
    assert _get_agent_sub_agents(out, "agent") == ["$APX_PEER_TRIAGE_URL"]


def test_list_targets_composition_root_ineligible():
    targets = _list_sub_agent_targets(COMPOSITION)
    by_name = {t["name"]: t for t in targets}
    assert by_name["billing"]["eligible"] is True
    assert by_name["agent"]["eligible"] is False
    assert "HandoffAgent" in (by_name["agent"]["reason"] or "")


def test_append_and_remove_sub_agent_idempotent():
    src, already = _append_sub_agent(SIMPLE, "$APX_PEER_TRIAGE_URL", target="agent")
    assert already is False
    assert _get_agent_sub_agents(src, "agent") == ["$APX_PEER_TRIAGE_URL"]
    src2, already2 = _append_sub_agent(src, "$APX_PEER_TRIAGE_URL", target="agent")
    assert already2 is True
    assert src2 == src
    cleared = _remove_sub_agent(src, "$APX_PEER_TRIAGE_URL", target="agent")
    assert _get_agent_sub_agents(cleared, "agent") == []


def test_peer_env_key():
    assert _peer_env_key("triage-app") == "APX_PEER_TRIAGE_APP_URL"
    assert _peer_env_key("Sales Genie!") == "APX_PEER_SALES_GENIE_URL"


def test_slug_tool_name_collision():
    assert _slug_tool_name("main.ml.score_lead") == "score_lead"
    assert _slug_tool_name("main.ml.score_lead", {"score_lead"}) == "ml_score_lead"


def test_slug_tool_name_avoids_keywords():
    """#630: UC function short name that is a Python keyword must not be emitted raw."""
    name = _slug_tool_name("main.ml.class")
    assert name == "t_class"
    assert not keyword.iskeyword(name)


def test_splice_uc_function_tool():
    out = _splice_factory_tool(
        SIMPLE,
        import_names=["uc_function_tool"],
        binding_name="score_lead",
        call_expr='uc_function_tool("main.ml.score_lead")',
        target="agent",
    )
    assert "from apx_agent import Agent, uc_function_tool" in out or (
        "uc_function_tool" in out and "from apx_agent import" in out
    )
    assert 'score_lead = uc_function_tool("main.ml.score_lead")' in out
    assert "tools=[score_lead]" in out
    cleared = _remove_factory_binding(out, "score_lead")
    assert "score_lead = " not in cleared
    assert "tools=[score_lead]" not in cleared


def test_splice_genie_and_vs():
    out = _splice_factory_tool(
        SIMPLE,
        import_names=["genie_tool"],
        binding_name="ask_sales",
        call_expr='genie_tool("space-1", name="ask_sales")',
        target="agent",
    )
    out = _splice_factory_tool(
        out,
        import_names=["vector_search_tool"],
        binding_name="vs_docs",
        call_expr='vector_search_tool("main.rag.docs", columns=["content"], name="vs_docs")',
        target="agent",
    )
    assert "ask_sales" in out and "vs_docs" in out
    assert "tools=[ask_sales, vs_docs]" in out or (
        "ask_sales" in out and "vs_docs" in out and "tools=[" in out
    )

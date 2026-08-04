"""Hot-apply helpers for Discover wire."""

from __future__ import annotations

import pytest

from apx_agent import Agent, HandoffAgent
from apx_agent._discover_hot import (
    hot_apply_factory_tool,
    hot_apply_sub_agent,
    hot_remove_factory_tool,
    hot_remove_sub_agent,
    resolve_live_leaf,
)
from apx_agent._models import AgentCard, AgentConfig, AgentContext


def _ctx(agent) -> AgentContext:
    config = AgentConfig(name="t", model="claude-fake")
    card = AgentCard(name="t", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=agent)


def test_resolve_live_leaf_root_and_handoff():
    leaf = Agent(tools=[], instructions="billing")
    root = HandoffAgent(agents={"billing": leaf})
    assert resolve_live_leaf(leaf, "agent") is leaf
    assert resolve_live_leaf(root, "billing") is leaf
    assert resolve_live_leaf(root, "missing") is None


@pytest.mark.asyncio
async def test_hot_apply_and_remove_sub_agent(monkeypatch: pytest.MonkeyPatch):
    live = Agent(tools=[], instructions="hi")
    ctx = _ctx(live)

    async def _fake_fetch(self):
        return []

    monkeypatch.setattr("apx_agent._agents.LlmAgent.fetch_remote_tools", _fake_fetch)
    # Apps suffix is allowlisted; skip live DNS for the unit test host.
    monkeypatch.setattr("apx_agent._ui_probe._validate_probe_url", lambda _url: None)

    ok = await hot_apply_sub_agent(
        ctx,
        target="agent",
        ref="$APX_PEER_X_URL",
        url="https://x.aws.databricksapps.com",
        env_key="APX_PEER_X_URL",
    )
    assert ok is True
    assert "$APX_PEER_X_URL" in live._sub_agent_urls

    ok2 = await hot_remove_sub_agent(ctx, target="agent", ref="$APX_PEER_X_URL")
    assert ok2 is True
    assert "$APX_PEER_X_URL" not in live._sub_agent_urls


@pytest.mark.asyncio
async def test_hot_apply_sub_agent_rejects_off_allowlist(monkeypatch: pytest.MonkeyPatch):
    live = Agent(tools=[], instructions="hi")
    ctx = _ctx(live)

    async def _fake_fetch(self):
        raise AssertionError("must not fetch off-allowlist peer")

    monkeypatch.setattr("apx_agent._agents.LlmAgent.fetch_remote_tools", _fake_fetch)

    ok = await hot_apply_sub_agent(
        ctx,
        target="agent",
        ref="https://evil.example",
        url="https://evil.example",
    )
    assert ok is False
    assert live._sub_agent_urls == []


@pytest.mark.asyncio
async def test_hot_apply_uc_tool():
    live = Agent(tools=[], instructions="hi")
    ctx = _ctx(live)
    ok = await hot_apply_factory_tool(
        ctx,
        target="agent",
        kind="uc_function",
        binding_name="score_lead",
        full_name="main.ml.score_lead",
    )
    assert ok is True
    assert any(fn.__name__ == "score_lead" for fn in live._tool_fns)
    assert any(t.name == "score_lead" for t in ctx.tools)

    removed = await hot_remove_factory_tool(ctx, target="agent", binding_name="score_lead")
    assert removed is True
    assert all(fn.__name__ != "score_lead" for fn in live._tool_fns)

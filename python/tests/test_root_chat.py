"""End-user chat at ``/`` — the public face of a deployed agent.

Guards two things the dev UI does NOT do:
  1. The page is self-contained (markdown libs inlined, talks to /responses) —
     no /_apx/* assets, which aren't mounted in production.
  2. It ships in the Apps runtime. The dev UI is gated off when
     DATABRICKS_APP_PORT is set; the root chat must survive that gate.

It targets /responses (ResponsesAgent {input}->{output}) deliberately: that
contract is identical on both serve paths, whereas /invocations is ChatAgent
{messages} under create_app but ResponsesAgent {input} under Apps.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, LlmAgent, setup_agent
from apx_agent._ui_root_chat import build_root_chat_router, render_root_chat

from .conftest import get_weather


def test_page_is_self_contained():
    html = render_root_chat()
    # Talks to /responses (ResponsesAgent, works on both serve paths), not the
    # ambiguous /invocations, and never reaches for /_apx/* dev assets.
    assert "/responses" in html
    assert "/invocations" not in html
    assert "/_apx/" not in html
    # Markdown libs inlined (not <script src=...>) so the page needs no assets.
    assert "marked" in html and "DOMPurify" in html
    assert "/_apx/vendor" not in html


@pytest.mark.asyncio
async def test_root_serves_chat_html():
    app = FastAPI()
    app.include_router(build_root_chat_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/responses" in r.text


@pytest.mark.asyncio
async def test_setup_agent_mounts_root_chat():
    app = FastAPI()
    agent = LlmAgent(tools=[get_weather])
    await setup_agent(app, agent, AgentConfig(name="t"))
    assert "/" in [r.path for r in app.routes]


@pytest.mark.asyncio
async def test_root_chat_ships_in_apps_runtime_but_dev_ui_does_not(monkeypatch):
    """The regression this feature fixes: in the Apps runtime the dev UI is
    gated off, so a deployed agent had nothing at /. Root chat must mount; the
    dev console must not."""
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    from apx_agent._wiring import mount_mcp_endpoints

    app = FastAPI()
    mount_mcp_endpoints(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    paths = [r.path for r in app.routes]
    assert "/" in paths, "root chat must ship in the Apps runtime"
    assert "/_apx/agent" not in paths, "dev console must stay off in Apps"

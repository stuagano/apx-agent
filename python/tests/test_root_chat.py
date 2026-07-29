"""End-user chat at ``/`` — the public face of a deployed agent.

Guards two things:
  1. The page is self-contained (markdown libs inlined, talks to /responses) —
     it does not depend on /_apx/* assets.
  2. It ships in the Apps runtime alongside the Dev UI (``/_apx/*``), so a
     deployed agent always has a simple chat at ``/`` even if someone deep-links
     past the Dev shell.

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


def test_page_is_titled_for_the_agent():
    html = render_root_chat("Payroll Coworker", "Answers payroll questions")
    assert "<title>Payroll Coworker</title>" in html
    assert "Answers payroll questions" in html


def test_name_and_description_are_html_escaped():
    # config strings are attacker-influenced in the general case — must not break
    # out of the title/header markup.
    html = render_root_chat("<script>x</script>", "a & b <b>")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "a &amp; b &lt;b&gt;" in html


@pytest.mark.asyncio
async def test_root_serves_chat_html():
    app = FastAPI()
    app.include_router(build_root_chat_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/responses" in r.text
    # No agent_context on a bare app → falls back to the generic title.
    assert "<title>Agent</title>" in r.text


@pytest.mark.asyncio
async def test_root_title_reflects_served_agent():
    app = FastAPI()
    await setup_agent(app, LlmAgent(tools=[get_weather]), AgentConfig(name="weather-bot"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/")
    assert "<title>weather-bot</title>" in r.text


@pytest.mark.asyncio
async def test_setup_agent_mounts_root_chat():
    app = FastAPI()
    agent = LlmAgent(tools=[get_weather])
    await setup_agent(app, agent, AgentConfig(name="t"))
    assert "/" in [r.path for r in app.routes]


@pytest.mark.asyncio
async def test_root_chat_and_dev_ui_both_ship_in_apps_runtime(monkeypatch):
    """Deployed Apps get root chat at ``/`` and the Dev UI at ``/_apx/*``
    (Discover, Chat shell, Probe, …). Write endpoints stay token-gated."""
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    from apx_agent._wiring import mount_mcp_endpoints

    app = FastAPI()
    mount_mcp_endpoints(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    paths = [r.path for r in app.routes]
    assert "/" in paths, "root chat must ship in the Apps runtime"
    assert "/_apx/agent" in paths, "Dev UI (incl. Discover) must mount on Apps"
    assert "/_apx/discover" in paths, "Discover must be available on deployed Apps"

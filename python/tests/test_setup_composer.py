"""Tests for the Setup agent composer endpoints (GET/POST /_apx/setup/agents).

The composer now reads/writes the LOCAL agent.py (single source of truth) — it
used to GET deployed workspace apps and POST orphan Agent(...) lines.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


def _app() -> FastAPI:
    app = FastAPI()
    config = AgentConfig(name="composer-test", model="claude-fake")
    app.state.agent_context = AgentContext(
        config=config, tools=[], card=AgentCard(name="t", description="", skills=[]),
        agent=None,  # type: ignore[arg-type]
    )
    app.state.workspace_client = None
    app.include_router(build_dev_ui_router())
    return app


_AGENT_PY = (
    'from apx_agent import Agent, tool\n'
    '@tool\n'
    'def echo(message: str) -> str:\n'
    '    """Echo."""\n'
    '    return message\n'
    '@tool\n'
    'def lookup(id: str) -> str:\n'
    '    """Lookup."""\n'
    '    return id\n'
    'agent = Agent(name="hw", instructions="You are helpful.", tools=[echo])\n'
)


class TestLoadNodes:
    @pytest.mark.asyncio
    async def test_get_returns_local_agent_nodes(self, tmp_path):
        agent_py = tmp_path / "agent.py"
        agent_py.write_text(_AGENT_PY)
        with patch("apx_agent._dev._find_agent_router_path", return_value=agent_py):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as ac:
                r = await ac.get("/_apx/setup/agents")
        assert r.status_code == 200
        nodes = r.json()
        # The root agent is present with its real tools + instructions —
        # not a list of deployed workspace apps.
        root = next(n for n in nodes if n["name"] == "agent")
        assert root["tools"] == ["echo"]
        assert root["instructions"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_get_empty_when_no_agent_file(self):
        with patch("apx_agent._dev._find_agent_router_path", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as ac:
                r = await ac.get("/_apx/setup/agents")
        assert r.json() == []


class TestApplyNodes:
    @pytest.mark.asyncio
    async def test_post_patches_root_surgically(self, tmp_path):
        agent_py = tmp_path / "agent.py"
        agent_py.write_text(_AGENT_PY)
        payload = {"nodes": [
            {"name": "agent", "tools": ["echo", "lookup"], "instructions": "Be a billing expert."}
        ]}
        with patch("apx_agent._dev._find_agent_router_path", return_value=agent_py), \
             patch("apx_agent._dev._ws_upload_agent_file"):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as ac:
                r = await ac.post("/_apx/setup/agents", json=payload)
        assert r.json()["ok"] is True
        written = agent_py.read_text()
        # Root agent updated, other args preserved, still valid.
        assert "Be a billing expert." in written
        assert "tools=[echo, lookup]" in written
        assert 'name="hw"' in written
        compile(written, "agent.py", "exec")

    @pytest.mark.asyncio
    async def test_post_skips_new_agent_names(self, tmp_path):
        agent_py = tmp_path / "agent.py"
        agent_py.write_text(_AGENT_PY)
        payload = {"nodes": [
            {"name": "agent", "tools": ["echo"], "instructions": "root"},
            {"name": "brand_new_agent", "tools": ["lookup"], "instructions": "new"},
        ]}
        with patch("apx_agent._dev._find_agent_router_path", return_value=agent_py), \
             patch("apx_agent._dev._ws_upload_agent_file"):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as ac:
                r = await ac.post("/_apx/setup/agents", json=payload)
        d = r.json()
        assert d["ok"] is True
        assert d["applied"] == ["agent"]
        assert d["skipped"] == ["brand_new_agent"]
        # No orphan brand_new_agent = Agent(...) line was appended.
        assert "brand_new_agent" not in agent_py.read_text()

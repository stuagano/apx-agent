"""Saving from /_apx/edit must land the agent's source on disk.

The Edit page posts ``{content}`` to ``POST /_apx/edit``. A response that says
``ok`` is not the save — this reads the module back and requires the posted
source to be the bytes now on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext, _dev
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard

_ORIGINAL = 'agent = None  # before the save\n'


@pytest.fixture
def agent_router_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agent_router.py"
    path.write_text(_ORIGINAL)
    monkeypatch.setattr(_dev, "_find_agent_router_path", lambda: path)
    return path


@pytest.fixture
def app(agent_router_path: Path) -> FastAPI:
    config = AgentConfig(
        name="dev-ui-test",
        system_prompt="You are helpful.",
        model="databricks-meta-llama-3-3-70b-instruct",
        warehouse_id="wh",
    )
    card = AgentCard(name="dev-ui-test", description="", skills=[])
    ctx = AgentContext(config=config, tools=[], card=card, agent=object())  # type: ignore[arg-type]
    application = FastAPI()
    application.state.agent_context = ctx
    application.include_router(build_dev_ui_router())
    return application


@pytest.fixture
def client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_edit_page_save_button_posts_and_the_source_lands(
    client: AsyncClient, agent_router_path: Path
) -> None:
    page = await client.get("/_apx/edit")
    assert page.status_code == 200
    assert '<button id="btn-save">' in page.text
    assert "document.getElementById('btn-save').addEventListener('click', save);" in page.text
    assert "(window.apxDevFetch||fetch)('/_apx/edit'" in page.text
    assert "JSON.stringify({ content })" in page.text

    edited = "agent = None  # rewritten from the edit page\n"
    saved = await client.post("/_apx/edit", json={"content": edited})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["ok"] is True

    on_disk = agent_router_path.read_text()
    assert on_disk == edited
    assert "rewritten from the edit page" in on_disk
    assert on_disk != _ORIGINAL


@pytest.mark.asyncio
async def test_edit_page_save_of_unchanged_source_writes_nothing_new(
    client: AsyncClient, agent_router_path: Path
) -> None:
    original = agent_router_path.read_text()
    saved = await client.post("/_apx/edit", json={"content": original})
    assert saved.status_code == 200
    body = saved.json()
    assert body["ok"] is True
    assert body["restart_required"] is False
    assert "diff" not in body
    assert agent_router_path.read_text() == original

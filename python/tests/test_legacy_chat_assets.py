"""Read-back contracts for the bounded legacy-chat history asset (#1052)."""

from pathlib import Path
from zipfile import ZipFile

import pytest
from ctk import Artifact, verify
from fastapi import FastAPI
from hatchling.builders.wheel import WheelBuilder
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import build_dev_ui_router
from apx_agent._ui_chat import _render_agent_ui

PYTHON = Path(__file__).resolve().parents[1]
ASSET = PYTHON / "src/apx_agent/_static/vendor/legacy-chat-history.js"
SCRIPT = '<script src="/_apx/vendor/legacy-chat-history.js"></script>'


@pytest.mark.asyncio
async def test_history_asset_is_served_and_wired_in_original_order():
    app = FastAPI()
    app.state.agent_context = None
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/_apx/chat?shell=1")
        response = await client.get("/_apx/vendor/legacy-chat-history.js")
        missing = await client.get("/_apx/vendor/missing-chat.js")
        traversal = await client.get("/_apx/vendor/%2e%2e%2f_ui_chat.py")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    verify(Artifact(ASSET, min_bytes=1000, must_contain="function loadConversationHistory()"))
    for token in ("function newConversation()", "fetch('/_apx/conversations')", "history.length = 0;"):
        assert token in response.text
    assert response.text == ASSET.read_text(encoding="utf-8")
    assert missing.status_code == traversal.status_code == 404
    assert page.status_code == 200
    assert page.text.count(SCRIPT) == 1
    assert "function loadConversationHistory()" not in page.text
    assert page.text.index("function switchTab(") < page.text.index(SCRIPT)
    assert page.text.index(SCRIPT) < page.text.index("async function checkPendingApprovals()")
    # Classic, parser-blocking scripts preserve shared globals and initialization.
    assert SCRIPT + "\n<script>" in page.text


@pytest.mark.parametrize("embed", [False, True])
def test_native_chat_inlines_the_same_history_code_before_transport(embed):
    html = _render_agent_ui(None, native=True, embed=embed)
    code = ASSET.read_text(encoding="utf-8")
    assert SCRIPT not in html
    assert 'src="/_apx/vendor/' not in html
    assert html.count(code) == 1
    assert html.index("function switchTab(") < html.index(code)
    assert html.index(code) < html.index("async function checkPendingApprovals()")
    assert html.index(code) < html.index("apxNativeChat")
    assert "sessionStorage.setItem(_THREAD_KEY, devThreadId)" in code
    assert "esc(label)" in code


def test_built_wheel_contains_history_asset(tmp_path):
    wheel = next(WheelBuilder(str(PYTHON)).build(directory=str(tmp_path), versions=["standard"]))
    with ZipFile(wheel) as archive:
        code = archive.read("apx_agent/_static/vendor/legacy-chat-history.js")
    assert code == ASSET.read_bytes()
    assert b"function _doLoadConversationHistory()" in code
    assert b"setTimeout(_doLoadConversationHistory, 120)" in code

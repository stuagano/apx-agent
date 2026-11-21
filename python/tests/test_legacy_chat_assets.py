"""Read-back contracts for the legacy chat static assets (#1052)."""

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
VENDOR = PYTHON / "src/apx_agent/_static/vendor"
ASSET = VENDOR / "legacy-chat-history.js"
PRELUDE = VENDOR / "legacy-chat-prelude.js"
APP_JS = VENDOR / "legacy-chat-app.js"
CHAT_JS = (
    "legacy-chat-history.js",
    "legacy-chat-prelude.js",
    "legacy-chat-app.js",
    "native-chat.js",
    "root-chat.js",
)
SCRIPT = '<script src="/_apx/vendor/legacy-chat-history.js"></script>'
PRELUDE_TAG = '<script src="/_apx/vendor/legacy-chat-prelude.js"></script>'
APP_TAG = '<script src="/_apx/vendor/legacy-chat-app.js"></script>'
FLAG = '<script>window.__APX_NATIVE__ = false;</script>'
NATIVE_SRC = 'src="/_apx/vendor/native-chat.js"'


@pytest.mark.asyncio
async def test_history_asset_is_served_and_wired_in_original_order():
    app = FastAPI()
    app.state.agent_context = None
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/_apx/chat?shell=1")
        response = await client.get("/_apx/vendor/legacy-chat-history.js")
        prelude = await client.get("/_apx/vendor/legacy-chat-prelude.js")
        app_js = await client.get("/_apx/vendor/legacy-chat-app.js")
        missing = await client.get("/_apx/vendor/missing-chat.js")
        traversal = await client.get("/_apx/vendor/%2e%2e%2f_ui_chat.py")
    for served, path in ((response, ASSET), (prelude, PRELUDE), (app_js, APP_JS)):
        assert served.status_code == 200
        assert "javascript" in served.headers["content-type"]
        assert served.text == path.read_text(encoding="utf-8")
    verify(Artifact(ASSET, min_bytes=1000, must_contain="function loadConversationHistory()"))
    verify(Artifact(PRELUDE, min_bytes=100, must_contain="function switchTab("))
    verify(Artifact(APP_JS, min_bytes=1000, must_contain="async function checkPendingApprovals()"))
    for token in ("function newConversation()", "fetch('/_apx/conversations')", "history.length = 0;"):
        assert token in response.text
    assert "async function checkPendingApprovals()" in app_js.text
    assert missing.status_code == traversal.status_code == 404
    assert page.status_code == 200
    assert page.text.count(PRELUDE_TAG) == page.text.count(SCRIPT) == page.text.count(APP_TAG) == 1
    assert page.text.count(FLAG) == 1
    # Prelude, history, inline table helpers, the native flag, then the app.
    assert page.text.index(PRELUDE_TAG) < page.text.index(SCRIPT)
    assert page.text.index(SCRIPT) < page.text.index("function tableShape")
    assert page.text.index("function tableShape") < page.text.index(FLAG)
    assert page.text.index(FLAG) < page.text.index(APP_TAG)
    assert "function loadConversationHistory()" not in page.text
    assert "function switchTab(" not in page.text
    assert "async function checkPendingApprovals()" not in page.text
    # Classic, parser-blocking scripts preserve shared globals and initialization.
    assert SCRIPT + "\n<script>" in page.text


@pytest.mark.parametrize("embed", [False, True])
def test_native_chat_inlines_the_same_history_code_before_transport(embed):
    html = _render_agent_ui(None, native=True, embed=embed)
    code = ASSET.read_text(encoding="utf-8")
    assert SCRIPT not in html
    assert PRELUDE_TAG not in html
    assert APP_TAG not in html
    # Legacy chat stays inline. The native client is the one vendor request.
    assert html.count(NATIVE_SRC) == 1
    assert html.count('src="/_apx/vendor/') == 1
    assert html.count(code) == 1
    assert html.index("function switchTab(") < html.index(code)
    assert html.index(code) < html.index("async function checkPendingApprovals()")
    assert html.index(code) < html.index("apxNativeChat")
    assert "sessionStorage.setItem(_THREAD_KEY, devThreadId)" in code
    assert "esc(label)" in code


def test_built_wheel_contains_history_asset(tmp_path):
    wheel = next(WheelBuilder(str(PYTHON)).build(directory=str(tmp_path), versions=["standard"]))
    with ZipFile(wheel) as archive:
        shipped = {name: archive.read(f"apx_agent/_static/vendor/{name}") for name in CHAT_JS}
    for name, code in shipped.items():
        assert code == (VENDOR / name).read_bytes()
    history = shipped["legacy-chat-history.js"]
    assert b"function _doLoadConversationHistory()" in history
    assert b"setTimeout(_doLoadConversationHistory, 120)" in history
    assert b"function switchTab(" in shipped["legacy-chat-prelude.js"]
    assert b"async function checkPendingApprovals()" in shipped["legacy-chat-app.js"]
    assert b"function apxPriorTurns(" in shipped["native-chat.js"]
    assert b"fetch('/invocations'" in shipped["root-chat.js"]

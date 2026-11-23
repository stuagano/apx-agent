"""Read-back contracts for the legacy chat static assets (#1052)."""

import json
import shutil
import subprocess
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
SHELL_JS = VENDOR / "dev-shell.js"
CHAT_JS = (
    "legacy-chat-history.js",
    "legacy-chat-prelude.js",
    "legacy-chat-app.js",
    "native-chat.js",
    "root-chat.js",
    "dev-shell.js",
    "eval-landing.js",
    "judge-alignment.js",
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
def test_native_chat_loads_the_same_vendor_scripts_before_transport(embed):
    # Durable hosts serve /_apx/vendor/, so native pages keep the script tags.
    html = _render_agent_ui(None, native=True, embed=embed)
    code = ASSET.read_text(encoding="utf-8")
    assert html.count(PRELUDE_TAG) == html.count(SCRIPT) == html.count(APP_TAG) == 1
    assert html.count('<script src="/_apx/vendor/marked.min.js"></script>') == 1
    assert html.count('<script src="/_apx/vendor/purify.min.js"></script>') == 1
    assert html.count(NATIVE_SRC) == 1
    assert code not in html
    assert "function switchTab(" not in html
    assert "async function checkPendingApprovals()" not in html
    assert html.index(PRELUDE_TAG) < html.index(SCRIPT) < html.index("function tableShape")
    assert html.index("function tableShape") < html.index(APP_TAG) < html.index("apxNativeChat")
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
    assert b'function selectTab(slug)' in shipped["dev-shell.js"]
    assert b'document.getElementById("dash-frame")' in shipped["dev-shell.js"]
    assert b"async function runCase(" in shipped["eval-landing.js"]
    assert b"let rows = window.__APX_EVAL_ROWS__" in shipped["eval-landing.js"]
    assert b"async function labelAlign()" in shipped["judge-alignment.js"]
    assert b"function renderAlignHistory(" in shipped["judge-alignment.js"]


_TAB_DOM = r"""
const listeners = new WeakMap();
function listen(el, type, fn) {
  const map = listeners.get(el) || {};
  (map[type] = map[type] || []).push(fn);
  listeners.set(el, map);
}
function fire(el, type, event) {
  for (const fn of ((listeners.get(el) || {})[type] || [])) fn(event);
}
function el(props) {
  const node = Object.assign({
    src: "",
    dataset: {},
    classList: null,
    addEventListener(type, fn) { listen(node, type, fn); },
    click() { fire(node, "click", {}); },
  }, props);
  node.classList = {
    add(c) { this._c.add(c); },
    remove(c) { this._c.delete(c); },
    toggle(c) { if (this._c.has(c)) this._c.delete(c); else this._c.add(c); },
    contains(c) { return this._c.has(c); },
    _c: new Set(),
  };
  return node;
}
const tabs = [
  el({dataset: {tab: "chat", src: "/_apx/chat?shell=1"}}),
  el({dataset: {tab: "tools", src: "/_apx/tools"}}),
];
const frame = el({src: "http://localhost/_apx/chat?shell=1"});
const ids = {
  "dash-frame": frame,
  "btn-sidebar-toggle": el({}),
  "topo-overlay": el({}),
  "topo-open": el({}),
  "topo-close": el({}),
  "topo-frame": el({}),
  "sb-identity": el({}),
  "sb-links": el({}),
  "cat-tree": el({}),
};
global.document = {
  body: {
    getAttribute(name) { return name === "data-default-tab" ? "chat" : null; },
    classList: el({}).classList,
  },
  getElementById: (id) => ids[id] || null,
  querySelectorAll: (sel) => sel === ".tab" ? tabs : [],
  querySelector: (sel) => {
    const match = /data-tab="([^"]+)"/.exec(sel);
    return match ? tabs.find((tab) => tab.dataset.tab === match[1]) : null;
  },
  addEventListener(type, fn) { listen(this, type, fn); },
};
global.window = global;
global.addEventListener = (type, fn) => listen(global, type, fn);
global.location = {origin: "http://localhost", hash: ""};
global.history = {url: "", replaceState(_state, _title, url) { this.url = url; }};
global.fetch = () => Promise.resolve({json: async () => ({
  host: "https://h", user: "dev", used_catalogs: [], used_schemas: [],
})});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.asyncio
async def test_shell_tab_switcher_is_served_and_changes_the_frame():
    """The shell loads dev-shell.js, and a tab click changes the frame src.

    An HTML substring check is not enough: #1011 shipped a page whose string
    tests passed while the live script threw.
    """
    app = FastAPI()
    app.state.agent_context = None
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/_apx/agent")
        asset = await client.get("/_apx/vendor/dev-shell.js")
    assert page.status_code == asset.status_code == 200
    tag = '<script src="/_apx/vendor/dev-shell.js"></script>'
    assert page.text.count(tag) == 1
    assert 'data-default-tab="chat"' in page.text
    assert 'src="/_apx/chat?shell=1"' in page.text
    assert "function selectTab(" not in page.text
    assert asset.text == SHELL_JS.read_text(encoding="utf-8")
    verify(Artifact(SHELL_JS, min_bytes=1000, must_contain='document.getElementById("dash-frame")'))
    checks = """
tabs[1].click();
const active = tabs.filter((tab) => tab.classList.contains("active")).map((tab) => tab.dataset.tab);
console.log(JSON.stringify({src: frame.src, hash: history.url, active}));
"""
    out = subprocess.run(
        ["node", "-e", _TAB_DOM + SHELL_JS.read_text(encoding="utf-8") + checks],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(out.stdout) == {"src": "/_apx/tools", "hash": "#tools", "active": ["tools"]}

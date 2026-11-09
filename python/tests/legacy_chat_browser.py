"""Explicit DOM gate: uv run --frozen --with playwright pytest tests/legacy_chat_browser.py."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from apx_agent._ui_chat import _render_agent_ui


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("embed", [False, True])
def test_history_asset_keeps_globals_debounce_and_new_conversation(native, embed):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        conversations = []
        reads = []
        assets = []
        html = _render_agent_ui(None, native=native, embed=embed)

        def route(request):
            path = request.request.url.split("localhost", 1)[1]
            if path == "/":
                request.fulfill(content_type="text/html", body=html)
            elif path.startswith("/_apx/vendor/"):
                assets.append(path)
                asset = Path(__file__).parents[1] / "src/apx_agent/_static/vendor" / path.rsplit("/", 1)[-1]
                request.fulfill(content_type="application/javascript", body=asset.read_text())
            elif path == "/_apx/conversations":
                reads.append(path)
                request.fulfill(json=conversations)
            elif path == "/_apx/tools":
                request.fulfill(content_type="text/html", body="<p>Tool inspector</p>")
            else:
                request.fulfill(json=[])

        page.route("**/*", route)
        page.goto("http://localhost/")
        # The later inline script must initialize and call the extracted global.
        expect(page.locator("#conv-list")).to_contain_text("No conversations yet")
        assert page.evaluate("typeof loadConversationHistory + ':' + typeof newConversation") == "function:function"
        asset_path = "/_apx/vendor/legacy-chat-history.js"
        assert assets.count(asset_path) == (0 if native else 1)
        thread = page.evaluate("devThreadId")
        conversations.append({"id": thread, "title": "<b>History proof</b>", "updated_at": "2026-01-01"})
        before = len(reads)
        page.evaluate("loadConversationHistory(); loadConversationHistory(); loadConversationHistory();")
        expect(page.locator(".conv-title")).to_have_text("<b>History proof</b>")
        assert len(reads) == before + 1
        assert page.locator(".conv-title b").count() == 0
        assert page.locator(".conv-item.active").get_attribute("data-id") == thread
        # Exercise the HTML onclick, not just a direct call to the function.
        page.evaluate("history.push({role:'user', content:'old'}); chat.innerHTML = '<p>old transcript</p>';")
        if native:
            # Native deliberately hides History, but its shared handler must still work.
            page.locator('button[onclick="newConversation()"]').dispatch_event("click")
        else:
            page.locator('.panel-tabs button[onclick*="history"]').click()
            page.locator('button[onclick="newConversation()"]').click()
        expect(page.locator("#chat")).to_be_empty()
        assert page.evaluate("history.length") == 0
        assert page.evaluate("devThreadId !== " + repr(thread))
        assert page.evaluate("sessionStorage.getItem(_THREAD_KEY) === devThreadId")
        expect(page.locator(".conv-item.active")).to_have_count(0)
        assert len(reads) == before + 2
        assert errors == []
        browser.close()

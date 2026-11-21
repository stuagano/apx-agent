"""Explicit DOM gate: uv run --frozen --with playwright pytest tests/root_chat_browser.py."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from apx_agent._ui_root_chat import render_root_chat


def test_root_chat_asset_posts_invocations():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        assets = []
        posts = []
        html = render_root_chat(native=False)

        def route(request):
            path = request.request.url.split("localhost", 1)[1]
            if path == "/":
                request.fulfill(content_type="text/html", body=html)
            elif path.startswith("/assets/chat/"):
                assets.append(path)
                asset = Path(__file__).parents[1] / "src/apx_agent/_static/vendor" / path.rsplit("/", 1)[-1]
                request.fulfill(content_type="application/javascript", body=asset.read_text())
            elif path == "/invocations":
                posts.append(request.request.post_data_json)
                request.fulfill(json={
                    "output": [{
                        "type": "message",
                        "content": [{"type": "output_text", "text": "root proof"}],
                    }],
                })
            else:
                request.fulfill(status=404, body="missing")

        page.route("**/*", route)
        page.goto("http://localhost/")
        assert assets.count("/assets/chat/root-chat.js") == 1
        assert page.evaluate("window.__APX_NATIVE__") is False
        page.locator("#in").fill("hello root")
        page.locator("#send").click()
        expect(page.locator(".msg.user")).to_have_text("hello root")
        expect(page.locator(".msg.assistant")).to_contain_text("root proof")
        assert posts == [{"input": [{"role": "user", "content": "hello root"}]}]
        assert errors == []
        browser.close()

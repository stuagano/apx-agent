"""Run explicitly: uv run --frozen --with playwright pytest tests/native_chat_browser.py.

Real Chromium DOM/network assertions against rendered pages and the frozen SDK wire
contract. The separate SDK contract suite covers the real runtime, not these routes.
"""

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from apx_agent._ui_chat import _render_agent_ui
from apx_agent._ui_root_chat import render_root_chat
from tests.test_native_invocation_contract import native, headers  # noqa: F401
from tests.test_runtime_targets import execution  # noqa: F401
from tests.test_dev_ui_routes import _make_ctx


@pytest.fixture(params=["root", "dev"])
def browser_chat(request):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        surface = request.param
        html = render_root_chat(native=True) if surface == "root" else _render_agent_ui(_make_ctx(), native=True)
        posts, reads = [], []
        state = {"status": "completed", "output": {"status": "completed", "messages": [
            {"type": "ai", "content": "Recorded proof"}]}}
        event = {"type": "agent.message.completed", "message": {"type": "tool", "content": "tool proof"}}

        def route(req):
            url = req.request.url
            if "/api/invocations" in url:
                if req.request.method == "POST":
                    posts.append(req.request.post_data_json)
                    req.fulfill(status=202, json={"id": posts[-1]["id"], "status": "active"})
                elif "/events" in url:
                    reads.append(url)
                    req.fulfill(content_type="text/event-stream", body=f"id: 1\nevent: agent.message.completed\ndata: {json.dumps(event)}\n\n")
                else:
                    reads.append(url)
                    req.fulfill(json={"id": posts[-1]["id"], **state})
            elif url.endswith("/"):
                req.fulfill(content_type="text/html", body=html)
            elif "/vendor/" in url:
                vendor = Path(__file__).parents[1] / "src/apx_agent/_static/vendor" / url.rsplit("/", 1)[-1]
                req.fulfill(content_type="application/javascript", body=vendor.read_text())
            else:
                req.fulfill(status=404, json={"detail": "Not found"})

        page.route("**/*", route)
        page.goto("http://localhost/")
        yield page, posts, reads, state, event
        assert errors == []
        browser.close()


def submit(page, text="record"):
    page.locator("form textarea").fill(text)
    page.locator("button[type=submit]").click()


def test_native_question_and_stable_session(browser_chat):
    page, posts, reads, state, event = browser_chat
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert len(posts) == 1
    body = posts[0]
    uuid.UUID(body["id"])
    uuid.UUID(body["session_id"])
    assert body == {"id": body["id"], "session_id": body["session_id"],
                    "input": {"messages": [{"role": "user", "content": "record"}]},
                    "background": True, "stream": True}
    submit(page, "next")
    expect(page.locator(".msg.assistant")).to_have_count(2)
    expect(page.locator(".msg.assistant").last).to_have_text("Recorded proof")
    assert posts[1]["id"] != body["id"]
    assert posts[1]["session_id"] == body["session_id"]
    assert len(reads) >= 4


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_native_terminal_states(browser_chat, status):
    page, posts, reads, state, event = browser_chat
    if status == "failed":
        state.update(status="failed", error="sanitized failure")
    else:
        state["output"].update(status="interrupted", approval_required={"tool_name": "record"})
    submit(page)
    expect(page.locator(".native-status")).to_contain_text(status.capitalize())
    expect(page.locator(".msg.assistant")).to_have_count(0)
    assert len(posts) == 1


def test_untrusted_result_and_event_text(browser_chat):
    page, posts, reads, state, event = browser_chat
    attack = '<img src=x onerror="window.injected=1">'
    event["message"]["content"] = attack
    state["output"]["messages"][0]["content"] = attack
    submit(page)
    expect(page.locator(".native-status")).to_have_text("Completed")
    assert page.evaluate("window.injected") is None
    assert page.locator("[onerror]").count() == 0
    assert json.loads(page.locator(".native-event").text_content())["message"]["content"] == attack


def test_reload_replays_suffix_without_resubmitting(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["status"] = "active"
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Reconnect")
    assert len(posts) == 1
    state["status"] = "completed"
    suffix = {"type": "agent.message.completed", "message": {"type": "tool", "content": "second proof"}}

    def replay(route):
        reads.append(route.request.url)
        # Include a duplicate defensively: the persisted exclusive cursor wins.
        route.fulfill(content_type="text/event-stream", body=
                      f"id: 1\ndata: {json.dumps(event)}\n\nid: 2\ndata: {json.dumps(suffix)}\n\n")

    page.route("**/events?after=1", replay)
    page.reload()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    expect(page.locator(".native-event")).to_have_count(2)
    expect(page.locator(".native-event").last).to_contain_text("second proof")
    assert any(url.endswith("/events?after=1") for url in reads)
    assert len(posts) == 1


def test_ambiguous_post_keeps_exact_retry(browser_chat):
    page, posts, reads, state, event = browser_chat
    def lost(route):
        posts.append(route.request.post_data_json)
        route.abort("failed")
    page.route("**/api/invocations", lost)
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Reconnect")
    assert len(posts) == 1
    page.unroute("**/api/invocations", lost)
    page.get_by_role("button", name="Retry same request", exact=True).click()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert len(posts) == 2 and posts[0] == posts[1]


def test_native_message_content_blocks(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [
        {"type": "tool", "content": "not an assistant answer"},
        {"type": "ai", "content": [{"type": "text", "text": "Block answer"}]},
    ]
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Block answer")


@pytest.mark.parametrize("failure", [401, 403, 404, "network"])
def test_reload_does_not_show_inaccessible_cached_content(browser_chat, failure):
    page, posts, reads, state, event = browser_chat
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    def inaccessible(route):
        if failure == "network":
            route.abort("failed")
        else:
            route.fulfill(status=failure, json={"detail": "Not available"})
    page.route("**/api/invocations/*", inaccessible)
    page.reload()
    expect(page.locator(".native-status")).to_contain_text("Connection uncertain")
    expect(page.locator(".msg.assistant")).to_have_count(0)
    expect(page.locator(".msg.user")).to_have_count(0)
    expect(page.locator(".native-event")).to_have_count(0)
    assert len(posts) == 1
    retry = page.get_by_role("button", name="Retry same request", exact=True)
    retry.dispatch_event("click")
    expect(retry).to_be_disabled()
    expect(page.locator(".msg.user")).to_have_count(0)
    expect(page.locator(".native-event")).to_have_count(0)
    assert len(posts) == 1
    old_id = posts[0]["id"]
    page.unroute("**/api/invocations/*", inaccessible)
    event["message"]["content"] = "new run event"
    state["output"]["messages"][0]["content"] = "New answer"
    submit(page, "new user question")
    expect(page.locator(".msg.assistant")).to_have_text("New answer")
    expect(page.locator(".msg.user")).to_have_text("new user question")
    expect(page.locator(".native-event")).to_have_count(1)
    expect(page.locator(".native-event")).to_contain_text("new run event")
    assert len(posts) == 2 and posts[1]["id"] != old_id
    assert posts[1]["input"] == {"messages": [{"role": "user", "content": "new user question"}]}


@pytest.mark.parametrize("surface", ["/", "/_apx/chat"])
def test_browser_real_native_host(request, monkeypatch, surface):
    monkeypatch.setenv("APX_DEV_UI", "1")
    host = request.getfixturevalue("native")
    posts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        print("Chromium", browser.version)
        page = browser.new_page()

        def forward(route):
            req = route.request
            url = urlsplit(req.url)
            path = url.path + ("?" + url.query if url.query else "")
            if req.method == "POST":
                posts.append({"path": path, "body": req.post_data_json})
            response = host.client.request(req.method, path, content=req.post_data,
                                           headers={**headers(), "Content-Type": "application/json"})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={"content-type": response.headers.get("content-type", "text/plain")})

        page.route("**/*", forward)
        page.goto("http://localhost" + surface)
        submit(page)
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof", timeout=30000)
        assert len(posts) == 1 and posts[0]["path"] == "/api/invocations"
        assert host.effects == ["proof"]
        page.reload()
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
        assert len(posts) == 1 and host.effects == ["proof"]
        assert "contract-credential-sentinel" not in page.evaluate("JSON.stringify(sessionStorage)")
        browser.close()

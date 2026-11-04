"""Explicit built-bundle proof: uv run --frozen --with playwright pytest tests/discover_browser.py."""

import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

STATIC = Path(__file__).parents[1] / "src/apx_agent/_static/topology"


@pytest.fixture
def discover():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        errors, calls = [], []
        state = {"user_id": "user-a", "host": "https://workspace-a.example", "app_url": "https://workspace-a.example/apps/app-a",
                 "project_id": "project-a", "project_catalog": "project_catalog", "used_catalogs": ["other"], "status": 200}
        pending_context = []
        state["pending_context"] = pending_context
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(r):
            path = urlsplit(r.request.url).path
            if path == "/_apx/workspace-context":
                calls.append("context")
                if state.get("hold_context"):
                    pending_context.append(r)
                else:
                    r.fulfill(status=state["status"], json={k: v for k, v in state.items() if k != "pending_context"})
            elif path == "/_apx/discover/graph.json":
                calls.append(r.request.url)
                r.fulfill(json={"nodes": [
                    {"id": "catalog:p", "type": "Catalog", "name": "project_catalog", "fqn": "project_catalog", "props": {}},
                    {"id": "schema:p.s", "type": "Schema", "name": "sales", "fqn": "project_catalog.sales", "props": {}},
                    {"id": "table:p.s.t", "type": "Table", "name": "orders", "fqn": "project_catalog.sales.orders", "props": {}},
                    {"id": "agent:self", "type": "Agent", "name": "test agent", "fqn": "test agent", "props": {}},
                ], "edges": [{"src": "schema:p.s", "dst": "table:p.s.t", "type": "contains"}],
                    "warnings": [], "built_at": "2026-11-04T12:00:00Z",
                    "agent": {"id": "agent:self", "footprint": [], "unplaced": []}})
            elif path == "/_apx/discover":
                r.fulfill(content_type="text/html", body=(STATIC / "discover.html").read_text())
            elif path.startswith("/_apx/topology/assets/"):
                asset = STATIC / "assets" / path.rsplit("/", 1)[-1]
                r.fulfill(content_type=mimetypes.guess_type(asset.name)[0], body=asset.read_bytes())
            else:
                r.fulfill(status=404)

        page.route("**/*", route)
        page.goto("http://discover.test/_apx/discover")
        expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
        yield page, calls, state
        assert errors == []
        browser.close()


def graph_calls(calls):
    return [c for c in calls if "/graph.json" in c]


def test_reload_cache_preferences_expiry_and_refresh(discover):
    page, calls, state = discover
    catalog = page.locator("#apx-dg-catalogs")
    expect(catalog).to_have_value("project_catalog")
    assert "catalogs=project_catalog" in graph_calls(calls)[0]
    expect(page.locator('.react-flow__node').filter(has_text="orders")).to_have_count(0)
    schema = page.locator('.react-flow__node').filter(has_text="sales")
    expect(schema).to_contain_text("+1")
    page.get_by_role("button", name="Expand schema sales").click()
    expect(page.locator('.react-flow__node').filter(has_text="orders")).to_have_count(1)
    page.get_by_label("Search assets").fill("sales")
    page.locator("label.apx-dg-filter").filter(has_text="View").locator("input").uncheck()
    page.reload()
    expect(page.get_by_text("Cached · updated", exact=False)).to_be_visible()
    expect(page.get_by_label("Search assets")).to_have_value("sales")
    expect(page.locator("label.apx-dg-filter").filter(has_text="View").locator("input")).not_to_be_checked()
    assert len(graph_calls(calls)) == 1
    assert calls.count("context") == 2
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 2 and "refresh=1" in graph_calls(calls)[-1]
    page.evaluate("""() => { for (const k of Object.keys(sessionStorage)) {
      if (k.includes(':graph:')) { const v = JSON.parse(sessionStorage[k]); v.updated -= 300001; sessionStorage[k] = JSON.stringify(v); }
    }}""")
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 3
    catalog.fill("other_catalog")
    page.get_by_role("button", name="Load", exact=True).click()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert "catalogs=other_catalog" in graph_calls(calls)[-1]
    page.reload()
    expect(page.get_by_text("Cached · updated", exact=False)).to_be_visible()
    expect(catalog).to_have_value("other_catalog")
    assert len(graph_calls(calls)) == 4
    catalog.fill("second, first")
    page.get_by_role("button", name="Load", exact=True).click()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    catalog.fill("first,second")
    page.get_by_role("button", name="Load", exact=True).click()
    expect(page.get_by_text("Cached · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 5


@pytest.mark.parametrize("change", ["user_id", "host", "app_url", "project_id"])
def test_cache_scope_isolation(discover, change):
    page, calls, state = discover
    page.get_by_label("Search assets").fill("old-user-filter")
    state[change] = "different-scope"
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    expect(page.get_by_label("Search assets")).to_have_value("")
    assert len(graph_calls(calls)) == 2


@pytest.mark.parametrize("failure", [401, 500, "unknown", None])
def test_identity_failure_never_displays_cached_graph(discover, failure):
    page, calls, state = discover
    if isinstance(failure, int):
        state["status"] = failure
    else:
        state["user_id"] = failure
    page.reload()
    expect(page.get_by_text("Could not load the workspace graph", exact=True)).to_be_visible()
    expect(page.locator(".react-flow__node")).to_have_count(0)
    expect(page.get_by_text("Cached · updated", exact=False)).to_have_count(0)
    assert len(graph_calls(calls)) == 1


def test_refresh_rechecks_identity_and_clears_old_graph(discover):
    page, calls, state = discover
    state["status"] = 401
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Could not load the workspace graph", exact=True)).to_be_visible()
    expect(page.locator(".react-flow__node")).to_have_count(0)
    assert len(graph_calls(calls)) == 1


def test_filter_edit_during_refresh_is_preserved(discover):
    page, calls, state = discover
    state["hold_context"] = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    page.get_by_label("Search assets").fill("new filter during verification")
    page.wait_for_function("document.querySelector('.apx-loading') !== null")
    # Playwright processes the held route while driving the input above.
    assert len(state["pending_context"]) == 1
    state["pending_context"].pop().fulfill(json={k: v for k, v in state.items() if k != "pending_context"})
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    expect(page.get_by_label("Search assets")).to_have_value("new filter during verification")


def test_local_unknown_project_disables_cache_reuse(discover):
    page, calls, state = discover
    state.update(app_url=None, project_id=None)
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 3
    assert page.evaluate("Object.keys(sessionStorage).filter(k => k.includes(':graph:')).length") == 1


def test_local_project_switch_is_isolated(discover):
    page, calls, state = discover
    state.update(app_url=None)
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    page.reload()
    expect(page.get_by_text("Cached · updated", exact=False)).to_be_visible()
    state["project_id"] = "project-b"
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 3


def test_corrupt_cache_is_a_miss(discover):
    page, calls, state = discover
    page.evaluate("""() => { for (const k of Object.keys(sessionStorage)) {
      if (k.includes(':graph:')) sessionStorage[k] = '{broken';
    }}""")
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 2


def test_unavailable_storage_does_not_break_discover(discover):
    page, calls, state = discover
    page.add_init_script("""Object.defineProperty(window, 'sessionStorage', {get() {throw new Error('storage disabled')}});""")
    page.reload()
    expect(page.get_by_text("Loaded · updated", exact=False)).to_be_visible()
    assert len(graph_calls(calls)) == 2

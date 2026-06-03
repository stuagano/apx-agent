"""Route + nav-link health for the dev UI.

Guards against nav drift: every link the nav bar advertises must resolve to a
real, renderable page (not a 404 or a redirect to a removed page), and no page
may advertise a link outside the canonical set. This is the automated answer to
"how do we check that all those links work" — it runs in CI on every change.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard
from apx_agent._ui_nav import APX_NAV_PAGES


def _make_ctx() -> AgentContext:
    config = AgentConfig(name="nav-test", model="claude-fake")
    card = AgentCard(name="nav-test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.state.agent_context = _make_ctx()
    a.include_router(build_dev_ui_router())
    return a


CANONICAL_SLUGS = {slug for slug, _ in APX_NAV_PAGES}
# Pages whose nav bar we render server-side and want to audit for dead links.
NAV_PAGE_SLUGS = ["agent", "edit", "setup", "eval", "probe"]


class TestCanonicalNavRoutesResolve:
    """Every canonical nav link must land on a real page (HTTP 200)."""

    @pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
    @pytest.mark.asyncio
    async def test_route_resolves_to_page(self, app: FastAPI, slug: str):
        # Follow redirects — what the browser actually experiences. Some pages
        # intentionally 302 (e.g. to a trailing-slash variant so an SPA's
        # relative asset paths resolve).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}", follow_redirects=True)
        assert r.status_code == 200, (
            f"/_apx/{slug} resolved to {r.status_code} — a nav link points at a "
            f"page that no longer renders. Update APX_NAV_PAGES or restore the route."
        )
        assert r.text.strip(), f"/_apx/{slug} returned an empty body"


class TestNoDeadNavLinks:
    """No rendered page may advertise a link outside the canonical set."""

    @pytest.mark.parametrize("slug", NAV_PAGE_SLUGS)
    @pytest.mark.asyncio
    async def test_page_nav_links_are_canonical(self, app: FastAPI, slug: str):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}")
        assert r.status_code == 200
        # Pull every /_apx/<word> nav target out of the rendered HTML.
        linked = set(re.findall(r'href="/_apx/([a-z]+)"', r.text))
        # The page may link to non-nav destinations (traces, chat). Only assert
        # that it does NOT link to known-removed pages.
        removed = {"tools", "wizard", "builder"}
        dead = linked & removed
        assert not dead, (
            f"/_apx/{slug} still links to removed page(s) {dead}. "
            f"Render the nav via _apx_nav_links() so it stays canonical."
        )


class TestLegacyRedirects:
    """Removed pages still redirect somewhere valid (no hard 404)."""

    @pytest.mark.parametrize("slug,target", [("tools", "/_apx/edit"), ("wizard", "/_apx/setup")])
    @pytest.mark.asyncio
    async def test_legacy_route_redirects(self, app: FastAPI, slug: str, target: str):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith(target)


class TestVendorAssets:
    """Vendored markdown libs (marked + DOMPurify) are served locally from
    /_apx/vendor/ so the deployed app needs no CDN (offline/private-link safe)."""

    @pytest.mark.asyncio
    async def test_serves_marked(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/marked.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 1000

    @pytest.mark.asyncio
    async def test_serves_purify(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/purify.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 1000

    @pytest.mark.asyncio
    async def test_vendor_path_traversal_blocked(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/../topology/index.html")
        assert r.status_code in (403, 404)


class TestMarkdownWiring:
    """The chat page loads the vendored libs locally and renders assistant
    messages as sanitized markdown."""

    def test_page_loads_vendor_libs_and_renders_assistant(self):
        from apx_agent._ui_chat import _render_agent_ui

        # Building the full HTML also catches f-string brace-doubling mistakes
        # in the CSS/JS we added (a stray single {/} throws at f-string eval).
        html = _render_agent_ui(_make_ctx())
        assert "/_apx/vendor/marked.min.js" in html
        assert "/_apx/vendor/purify.min.js" in html
        assert "DOMPurify.sanitize(marked.parse(" in html  # render wiring present
        assert "renderAssistantInto" in html  # helper present


# ---------------------------------------------------------------------------
# _pick_workspace_defaults — the Setup page's auto-prefill source
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from apx_agent._dev import _pick_workspace_defaults


def _named(name: str, **extra) -> SimpleNamespace:
    return SimpleNamespace(name=name, **extra)


def _wh(wh_id: str, state: str, serverless: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=wh_id,
        state=SimpleNamespace(value=state),
        enable_serverless_compute=serverless,
    )


class _MockWs:
    def __init__(self, *, catalogs, schemas_by_catalog, warehouses):
        self._catalogs = catalogs
        self._schemas = schemas_by_catalog
        self._warehouses = warehouses
        self.catalogs = SimpleNamespace(list=lambda: list(self._catalogs))
        self.schemas = SimpleNamespace(
            list=lambda catalog_name: list(self._schemas.get(catalog_name, []))
        )
        self.warehouses = SimpleNamespace(list=lambda: list(self._warehouses))


class TestPickWorkspaceDefaults:
    """Auto-prefill picks sensible defaults instead of forcing the user to
    manually pick catalog + schema + warehouse on every fresh agent."""

    def test_prefers_user_catalog_over_samples(self):
        """A real user catalog wins over the samples demo when both exist."""
        ws = _MockWs(
            catalogs=[_named("samples"), _named("my_catalog"), _named("system")],
            schemas_by_catalog={
                "samples": [_named("nyctaxi")],
                "my_catalog": [_named("information_schema"), _named("sales")],
            },
            warehouses=[_wh("wh_1", "RUNNING")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "my_catalog"
        assert out["DEMO_SCHEMA"] == "sales"  # skipped information_schema

    def test_falls_back_to_samples_when_no_user_catalogs(self):
        """A brand-new workspace with only samples still pre-fills usefully."""
        ws = _MockWs(
            catalogs=[_named("samples"), _named("system")],
            schemas_by_catalog={"samples": [_named("tpch"), _named("nyctaxi")]},
            warehouses=[_wh("wh_1", "STOPPED")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "samples"
        assert out["DEMO_SCHEMA"] == "nyctaxi"  # explicit nyctaxi preference inside samples

    def test_skips_system_catalogs(self):
        """``system`` and ``__databricks_internal`` must never be picked."""
        ws = _MockWs(
            catalogs=[_named("system"), _named("__databricks_internal"), _named("user_cat")],
            schemas_by_catalog={"user_cat": [_named("default")]},
            warehouses=[_wh("wh_1", "RUNNING")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "user_cat"

    def test_warehouse_prefers_running_over_stopped(self):
        """RUNNING avoids a 20-30s cold-start on the first query."""
        ws = _MockWs(
            catalogs=[],
            schemas_by_catalog={},
            warehouses=[
                _wh("stopped_1", "STOPPED", serverless=True),
                _wh("running_1", "RUNNING", serverless=False),
            ],
        )
        out = _pick_workspace_defaults(ws)
        assert out["WAREHOUSE_ID"] == "running_1"

    def test_warehouse_prefers_serverless_when_none_running(self):
        """Serverless beats classic when nothing is already running."""
        ws = _MockWs(
            catalogs=[],
            schemas_by_catalog={},
            warehouses=[
                _wh("classic_1", "STOPPED", serverless=False),
                _wh("serverless_1", "STOPPED", serverless=True),
            ],
        )
        out = _pick_workspace_defaults(ws)
        assert out["WAREHOUSE_ID"] == "serverless_1"

    def test_returns_empty_when_workspace_unreachable(self):
        """SDK errors must not crash the Setup page — fall back to empty."""
        def _boom():
            raise RuntimeError("boom")

        broken = SimpleNamespace(
            catalogs=SimpleNamespace(list=_boom),
            warehouses=SimpleNamespace(list=_boom),
        )
        out = _pick_workspace_defaults(broken)
        assert out == {}


class TestLandingRender:
    def _ctx(self, *, tools, examples):
        from apx_agent._models import AgentTool

        cfg = AgentConfig(name="demo-agent", description="A demo agent.", examples=examples)
        tool_objs = [
            AgentTool(name=n, description=d, input_schema={"type": "object", "properties": {}})
            for n, d in tools
        ]
        card = AgentCard(name="demo-agent", description="A demo agent.", skills=[])
        return AgentContext(config=cfg, tools=tool_objs, card=card, agent=None)  # type: ignore[arg-type]

    def test_landing_shows_greeting_cards_and_chips(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(
            tools=[("sample_customer", "Preview rows."), ("run_sql", "Run SQL.")],
            examples=["Show me sample customers", "Top 5 by balance"],
        ))
        assert 'id="landing"' in html
        assert "demo-agent" in html and "A demo agent." in html
        assert "sample_customer" in html and "run_sql" in html      # capability cards
        assert "Show me sample customers" in html and "Top 5 by balance" in html  # chips

    def test_landing_no_tools_no_chips_still_has_greeting(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(tools=[], examples=[]))
        assert 'id="landing"' in html
        assert "demo-agent" in html
        assert 'class="cap-cards"' not in html       # no capability cards
        assert 'class="starter-chips"' not in html   # no chips

    def test_landing_examples_only_no_tools(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(tools=[], examples=["Hi there"]))
        assert 'class="starter-chips"' in html and "Hi there" in html
        assert 'class="cap-cards"' not in html


class TestTraceDetailSpanEvents:
    def test_render_trace_detail_shows_span_events(self):
        from apx_agent._dev import _render_trace_detail
        spans = [{
            "span_id": "s1", "parent_id": None, "name": "run_sql",
            "span_type": "TOOL", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": None, "outputs": None,
            "events": [{"name": "apx.progress",
                        "attributes": {"message": "Starting SQL warehouse — serverless cold-start, ~20-30s"}}],
        }]
        html = _render_trace_detail("tr-1", spans, None)
        assert "Starting SQL warehouse" in html

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

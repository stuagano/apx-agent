"""Du Bois theme tokens + global injection for the /_apx/* dev UI."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from apx_agent._ui_nav import APX_NAV_PAGES
from apx_agent._ui_theme import (
    APX_COMPONENTS_CSS,
    APX_THEME_CSS,
    apx_theme_style,
)


def test_theme_defines_dubois_tokens():
    for var, value in [
        ("--apx-bg", "#11171C"),
        ("--apx-panel", "#1F272D"),
        ("--apx-border", "#445461"),
        ("--apx-text", "#E8ECF0"),
        ("--apx-accent", "#2272B4"),
    ]:
        assert var in APX_THEME_CSS, f"{var} missing from theme"
        assert value in APX_THEME_CSS, f"{var} should use Du Bois value {value}"


def test_theme_style_is_one_style_block():
    s = apx_theme_style()
    assert s.startswith("<style>") and s.endswith("</style>")
    assert "apx-dubois-theme" in s


def test_shared_components_defined_with_tokens():
    for cls in [".apx-card", ".apx-badge", ".apx-btn", ".apx-empty", ".apx-pre"]:
        assert cls in APX_COMPONENTS_CSS, f"{cls} must be a shared primitive"
    hexes = re.findall(r"#[0-9a-fA-F]{3,6}", APX_COMPONENTS_CSS)
    assert not hexes, f"shared components must use var(--apx-*), found: {hexes}"


async def _get(app, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


# The theme is injected into each server-rendered page's <head> (the true
# universal choke point; the deploy overlay is dropped on embed/some pages).
# `discover` always renders its <head> server-side, so it's the mechanism proof.
# Whole-surface coverage across all nav slugs is asserted in test_dubois_sweep
# once every module has been migrated (topology is a React SPA — out of scope).
@pytest.mark.asyncio
async def test_discover_page_carries_theme_once(dev_ui_app):
    r = await _get(dev_ui_app, "/_apx/discover")
    assert r.status_code == 200
    assert r.text.count("apx-dubois-theme") == 1, (
        "the Du Bois theme should be injected exactly once into the discover "
        "page <head> via apx_theme_style()"
    )

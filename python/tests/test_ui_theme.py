"""Du Bois theme tokens + global injection for the /_apx/* dev UI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from apx_agent._ui_nav import APX_NAV_PAGES
from apx_agent._ui_theme import (
    APX_COMPONENTS_CSS,
    APX_THEME_CSS,
    apx_theme_style,
)


def test_theme_defines_agentkit_tokens():
    # Values from the vendored AgentKit chat (_static/agentkit_chat/styles.css).
    for var, value in [
        ("--apx-bg", "#f1f3ff"),
        ("--apx-panel", "#ffffff"),
        ("--apx-border", "#e7e9f0"),
        ("--apx-text", "#1f2124"),
        ("--apx-accent", "#1a73e8"),
    ]:
        assert var in APX_THEME_CSS, f"{var} missing from theme"
        assert f"{var}:{value}" in APX_THEME_CSS, f"{var} should use AgentKit value {value}"


def test_theme_style_is_one_style_block():
    s = apx_theme_style()
    assert s.startswith("<style>") and s.endswith("</style>")
    assert "apx-agentkit-theme" in s


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
# `knowledge` always renders its <head> server-side, so it's the mechanism proof.
# Discover is now a React SPA, like topology, so it is out of scope here.
# Whole-surface coverage across all nav slugs is asserted in test_dubois_sweep
# once every module has been migrated (topology is a React SPA — out of scope).
@pytest.mark.asyncio
async def test_knowledge_page_carries_theme_once(dev_ui_app):
    r = await _get(dev_ui_app, "/_apx/knowledge")
    assert r.status_code == 200
    assert r.text.count("apx-agentkit-theme") == 1, (
        "the Du Bois theme should be injected exactly once into the knowledge "
        "page <head> via apx_theme_style()"
    )


_SWEPT = ["_dev.py", "_ui_nav.py", "_ui_tools.py", "_ui_hub.py", "_ui_table.py",
          "_ui_grounding.py", "_ui_edit.py", "_ui_knowledge.py"]
_SRC = Path(__file__).resolve().parents[1] / "src" / "apx_agent"
# 3-digit matches must look like CSS: "(#629)" / "#611)" are issue references.
_HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{6}\b|(?<![&\w(])#[0-9a-fA-F]{3}(?=[;\s'\"},])")


@pytest.mark.parametrize("name", _SWEPT)
def test_pages_use_theme_tokens_not_hex(name: str) -> None:
    hexes = _HEX.findall((_SRC / name).read_text(encoding="utf-8"))
    assert hexes == [], f"{name} hard-codes colors {sorted(set(hexes))[:12]}; use var(--apx-*)"

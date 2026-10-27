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


def test_theme_defines_dubois_tokens():
    # Du Bois values (@databricks/design-system dubois-colors.less), dark.
    for var, value in [
        ("--apx-bg", "#11171C"),
        ("--apx-panel", "#1F272D"),
        ("--apx-border", "#445461"),
        ("--apx-text", "#E8ECF0"),
        ("--apx-accent", "#2272B4"),
    ]:
        assert var in APX_THEME_CSS, f"{var} missing from theme"
        assert f"{var}:{value}" in APX_THEME_CSS, f"{var} should use Du Bois value {value}"
    assert "color-scheme:dark" in APX_THEME_CSS


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
# `knowledge` always renders its <head> server-side, so it's the mechanism proof.
# Discover is now a React SPA, like topology, so it is out of scope here.
# Whole-surface coverage across all nav slugs is asserted in test_dubois_sweep
# once every module has been migrated (topology is a React SPA — out of scope).
@pytest.mark.asyncio
async def test_knowledge_page_carries_theme_once(dev_ui_app):
    r = await _get(dev_ui_app, "/_apx/knowledge")
    assert r.status_code == 200
    assert r.text.count("apx-dubois-theme") == 1, (
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


def test_no_page_forces_its_own_color_scheme() -> None:
    # The theme owns color-scheme (dark Du Bois); a page forcing one would fight it.
    for path in sorted(p for p in _SRC.glob("_ui_*.py") if p.name != "_ui_theme.py") + [_SRC / "_dev.py"]:
        assert "color-scheme:dark" not in path.read_text(encoding="utf-8").replace(" ", ""), path.name


def test_feedback_save_hover_keeps_label_readable() -> None:
    css = (_SRC / "_dev.py").read_text(encoding="utf-8")
    hover = re.search(r"\.fb-save-btn:hover\{\{([^}]*)\}\}", css)
    assert hover and "color:var(--apx-on-accent)" in hover.group(1), "hover fills --apx-ok; label must contrast"
